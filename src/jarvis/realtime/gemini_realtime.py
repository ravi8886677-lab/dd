"""Gemini Live protocol adapter.

Differs from the OpenAI adapter in ways that are easy to get wrong:

- one server message can carry several parts, so translation returns a list
  rather than a single event;
- function arguments arrive decoded, and the tool bridge parses JSON text,
  so they are re-encoded here;
- the server announces an interruption itself instead of leaving it to be
  inferred from the user starting to speak;
- the two audio directions do not share a sample rate;
- the API key travels in the query string rather than an auth header.

Concurrency matches the other adapter and ``tools/external/mcp_runtime.py``:
one background thread owning an asyncio loop, synchronous interface in
front, events crossing back over a thread-safe queue.
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote

from ..debug import debug_log
from .backend import (
    RealtimeConfig,
    RealtimeEvent,
    RealtimeEventType,
    RealtimeVoiceBackend,
)
from .tool_bridge import FunctionCall, FunctionResult

DEFAULT_BASE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

# The directions differ. Sending at the output rate, or playing at the input
# rate, produces speech at the wrong pitch rather than an error.
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
INPUT_MIME_TYPE = f"audio/pcm;rate={INPUT_SAMPLE_RATE}"

_SENTINEL = object()


def gemini_tool_declarations(
    realtime_tools: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Wrap neutral tool definitions as Gemini function declarations.

    An empty declaration block is rejected by the API, so no tools means no
    block at all rather than an empty one.
    """
    declarations: List[Dict[str, Any]] = []

    for tool in realtime_tools or []:
        name = str((tool or {}).get("name") or "").strip()
        if not name:
            continue
        declarations.append(
            {
                "name": name,
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )

    return [{"functionDeclarations": declarations}] if declarations else []


def _decode_audio(data: Any) -> Optional[bytes]:
    try:
        return base64.b64decode(data or "", validate=True)
    except Exception:
        debug_log("⚠️ realtime: dropped an undecodable audio chunk")
        return None


def translate_server_message(message: Dict[str, Any]) -> List[RealtimeEvent]:
    """Turn one server message into zero or more Jarvis events.

    Unknown messages produce nothing rather than raising: the API grows new
    message types, and a session must not fall over because it met one.
    """
    events: List[RealtimeEvent] = []
    message = message or {}

    content = message.get("serverContent")
    if isinstance(content, dict):
        # Interruption first: it invalidates whatever is still playing.
        if content.get("interrupted"):
            events.append(RealtimeEvent(RealtimeEventType.SPEECH_STARTED))

        model_turn = content.get("modelTurn")
        if isinstance(model_turn, dict):
            for part in model_turn.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData")
                if isinstance(inline, dict):
                    audio = _decode_audio(inline.get("data"))
                    if audio:
                        events.append(
                            RealtimeEvent(RealtimeEventType.AUDIO_DELTA, audio=audio)
                        )
                    continue
                text = part.get("text")
                if text:
                    events.append(
                        RealtimeEvent(RealtimeEventType.ASSISTANT_TEXT, text=str(text))
                    )

        output_transcript = content.get("outputTranscription")
        if isinstance(output_transcript, dict) and output_transcript.get("text"):
            events.append(
                RealtimeEvent(
                    RealtimeEventType.ASSISTANT_TEXT,
                    text=str(output_transcript["text"]),
                )
            )

        input_transcript = content.get("inputTranscription")
        if isinstance(input_transcript, dict) and input_transcript.get("text"):
            events.append(
                RealtimeEvent(
                    RealtimeEventType.USER_TRANSCRIPT,
                    text=str(input_transcript["text"]),
                )
            )

        if content.get("turnComplete"):
            events.append(RealtimeEvent(RealtimeEventType.TURN_DONE))

    tool_call = message.get("toolCall")
    if isinstance(tool_call, dict):
        for call in tool_call.get("functionCalls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").strip()
            if not name:
                debug_log("⚠️ realtime: ignored a function call with no name")
                continue
            args = call.get("args")
            if not isinstance(args, dict):
                args = {}
            events.append(
                RealtimeEvent(
                    RealtimeEventType.FUNCTION_CALL,
                    function_call=FunctionCall(
                        call_id=str(call.get("id") or ""),
                        name=name,
                        # The bridge parses JSON text; Gemini already decoded it.
                        arguments_json=json.dumps(args),
                    ),
                )
            )

    error = message.get("error")
    if isinstance(error, dict):
        events.append(
            RealtimeEvent(
                RealtimeEventType.ERROR,
                error=str(error.get("message") or "the provider reported an error"),
            )
        )

    return events


class GeminiRealtimeBackend(RealtimeVoiceBackend):
    """A live connection to the Gemini Live API."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None
        self._events: "queue.Queue[Any]" = queue.Queue()
        self._closed = threading.Event()
        self._names_by_call_id: Dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    def connect(self, config: RealtimeConfig) -> None:
        """Open the socket and wait for the session to be accepted.

        Raises on failure so the session runner falls back to the local
        pipeline rather than surfacing an exception at the user.
        """
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("realtime voice needs the 'websockets' package") from exc

        if not config.api_key:
            raise RuntimeError("realtime voice has no API key configured")

        self._closed.clear()
        ready: "queue.Queue[Any]" = queue.Queue(maxsize=1)

        def _runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._serve(config, ready))
            except Exception as exc:
                debug_log(f"⚠️ realtime: session thread ended: {exc}")
                self._publish(RealtimeEvent(RealtimeEventType.ERROR, error=str(exc)))
            finally:
                self._publish(_SENTINEL)
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=_runner, name="jarvis-realtime-gemini", daemon=True
        )
        self._thread.start()

        outcome = ready.get()
        if isinstance(outcome, Exception):
            raise RuntimeError(f"realtime connection failed: {outcome}") from outcome
        debug_log("🎙️ realtime: connected to Gemini Live")

    async def _serve(self, config: RealtimeConfig, ready: "queue.Queue[Any]") -> None:
        """Own the socket for the lifetime of the session."""
        import websockets

        base = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}?key={quote(config.api_key, safe='')}"

        try:
            async with websockets.connect(url) as ws:
                self._ws = ws
                await ws.send(json.dumps(self._setup_frame(config)))

                # Audio sent before the server accepts the setup is discarded,
                # so the session is not "open" until setupComplete arrives.
                async for raw in ws:
                    message = self._decode(raw)
                    if message is None:
                        continue
                    if "setupComplete" in message:
                        ready.put(True)
                        continue
                    for event in translate_server_message(message):
                        self._remember_call(event)
                        self._publish(event)
        except Exception as exc:
            if ready.empty():
                ready.put(exc)
            raise
        finally:
            self._ws = None

    def _setup_frame(self, config: RealtimeConfig) -> Dict[str, Any]:
        """The opening frame: model, voice, transcription and tools.

        Transcription is requested in both directions because without it a
        realtime conversation never reaches the memory pipeline.
        """
        model = config.model
        if not model.startswith("models/"):
            model = f"models/{model}"

        setup: Dict[str, Any] = {
            "model": model,
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }

        if config.voice:
            setup["generationConfig"]["speechConfig"] = {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": config.voice}}
            }
        if config.instructions:
            setup["systemInstruction"] = {"parts": [{"text": config.instructions}]}

        tools = gemini_tool_declarations(config.tools)
        if tools:
            setup["tools"] = tools

        return {"setup": setup}

    def _decode(self, raw: Any) -> Optional[Dict[str, Any]]:
        """Decode one frame, which may arrive as text or as bytes."""
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
        except (ValueError, TypeError, UnicodeDecodeError):
            debug_log("⚠️ realtime: dropped an unparseable frame")
            return None
        return message if isinstance(message, dict) else None

    def _remember_call(self, event: RealtimeEvent) -> None:
        """Keep each call's name; the response has to quote it back."""
        if event.type is RealtimeEventType.FUNCTION_CALL and event.function_call:
            self._names_by_call_id[event.function_call.call_id] = (
                event.function_call.name
            )

    # ── Sending ───────────────────────────────────────────────────────

    def _submit(self, payload: Dict[str, Any]) -> None:
        loop, ws = self._loop, self._ws
        if loop is None or ws is None or self._closed.is_set():
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps(payload)), loop)
        except Exception as exc:
            debug_log(f"⚠️ realtime: send failed: {exc}")

    def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._submit(
            {
                "realtimeInput": {
                    "mediaChunks": [
                        {
                            "mimeType": INPUT_MIME_TYPE,
                            "data": base64.b64encode(pcm).decode("ascii"),
                        }
                    ]
                }
            }
        )

    def send_function_result(self, result: FunctionResult) -> None:
        """Return a tool's output against the call it answers.

        Gemini expects a structured response and the function's name back,
        not just the id, so the name is carried from the original call.
        """
        self._submit(
            {
                "toolResponse": {
                    "functionResponses": [
                        {
                            "id": result.call_id,
                            "name": self._names_by_call_id.get(result.call_id, ""),
                            "response": {
                                "output": result.output,
                                "success": result.success,
                            },
                        }
                    ]
                }
            }
        )

    def interrupt(self) -> None:
        """Gemini stops generating on its own when the user speaks.

        The server drives barge-in, so there is nothing to send. Local
        playback is stopped by the session runner, which is the half that
        actually needs doing.
        """
        return

    # ── Receiving ─────────────────────────────────────────────────────

    def events(self) -> Iterator[RealtimeEvent]:
        while True:
            item = self._events.get()
            if item is _SENTINEL:
                break
            yield item
        yield RealtimeEvent(RealtimeEventType.CLOSED)

    def _publish(self, item: Any) -> None:
        self._events.put(item)

    def close(self) -> None:
        """Close the socket and stop the thread. Safe to call more than once."""
        if self._closed.is_set():
            return
        self._closed.set()

        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception as exc:
                debug_log(f"⚠️ realtime: close failed: {exc}")

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

        self._publish(_SENTINEL)
        debug_log("🔇 realtime: disconnected")
