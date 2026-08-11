"""OpenAI realtime protocol adapter.

The only file in this package that knows a vendor's event names. It speaks
the socket, translates in both directions, and exposes the synchronous
interface in ``backend.py``.

Concurrency follows ``tools/external/mcp_runtime.py``: one background
thread owns an asyncio loop for the connection, and every public method
here is called from Jarvis's ordinary synchronous code, which never sees a
coroutine. Events cross back over a thread-safe queue.
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
from typing import Any, Dict, Iterator, Optional

from ..debug import debug_log
from .backend import (
    RealtimeConfig,
    RealtimeEvent,
    RealtimeEventType,
    RealtimeVoiceBackend,
)
from .tool_bridge import FunctionCall, FunctionResult

DEFAULT_BASE_URL = "wss://api.openai.com/v1/realtime"

# Audio the API speaks: 16-bit PCM, 24kHz, mono.
AUDIO_FORMAT = "pcm16"
SAMPLE_RATE = 24000

# Event names changed between the beta and GA shapes of this API. Both are
# accepted because an adapter that knows only one of them connects fine and
# then stays silent, which is a very expensive thing to debug by ear.
_AUDIO_DELTA_EVENTS = {"response.audio.delta", "response.output_audio.delta"}
_TRANSCRIPT_DELTA_EVENTS = {
    "response.audio_transcript.delta",
    "response.output_audio_transcript.delta",
}
_USER_TRANSCRIPT_EVENTS = {
    "conversation.item.input_audio_transcription.completed",
}

_SENTINEL = object()


def translate_server_event(payload: Dict[str, Any]) -> Optional[RealtimeEvent]:
    """Turn one server event into a Jarvis event, or ``None`` to ignore it.

    Unknown events are ignored rather than raised on: providers add event
    types, and a session must not fall over because it met a new one.
    """
    kind = str((payload or {}).get("type") or "")

    if kind in _AUDIO_DELTA_EVENTS:
        try:
            audio = base64.b64decode(payload.get("delta") or "", validate=True)
        except Exception:
            debug_log("⚠️ realtime: dropped an undecodable audio chunk")
            return None
        return RealtimeEvent(RealtimeEventType.AUDIO_DELTA, audio=audio)

    if kind in _TRANSCRIPT_DELTA_EVENTS:
        return RealtimeEvent(
            RealtimeEventType.ASSISTANT_TEXT, text=str(payload.get("delta") or "")
        )

    if kind in _USER_TRANSCRIPT_EVENTS:
        return RealtimeEvent(
            RealtimeEventType.USER_TRANSCRIPT, text=str(payload.get("transcript") or "")
        )

    if kind == "input_audio_buffer.speech_started":
        return RealtimeEvent(RealtimeEventType.SPEECH_STARTED)

    if kind == "response.function_call_arguments.done":
        name = str(payload.get("name") or "").strip()
        if not name:
            debug_log("⚠️ realtime: ignored a function call with no name")
            return None
        return RealtimeEvent(
            RealtimeEventType.FUNCTION_CALL,
            function_call=FunctionCall(
                call_id=str(payload.get("call_id") or ""),
                name=name,
                arguments_json=str(payload.get("arguments") or ""),
            ),
        )

    if kind == "response.done":
        return RealtimeEvent(RealtimeEventType.TURN_DONE)

    if kind == "error":
        error = payload.get("error")
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        return RealtimeEvent(
            RealtimeEventType.ERROR, error=message or "the provider reported an error"
        )

    return None


class OpenAIRealtimeBackend(RealtimeVoiceBackend):
    """A live connection to the OpenAI realtime API."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Any = None
        self._events: "queue.Queue[Any]" = queue.Queue()
        self._closed = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def connect(self, config: RealtimeConfig) -> None:
        """Open the socket and configure the session.

        Raises on failure. The session runner treats that as "fall back to
        the local pipeline", so no credentials and no network both end up
        with a working assistant rather than an exception at the user.
        """
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "realtime voice needs the 'websockets' package"
            ) from exc

        if not config.api_key:
            raise RuntimeError("realtime voice has no API key configured")

        self._closed.clear()
        ready: "queue.Queue[Any]" = queue.Queue(maxsize=1)

        def _runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._serve(config, ready))
            except Exception as exc:  # surfaced through `ready` or the queue
                debug_log(f"⚠️ realtime: session thread ended: {exc}")
                self._publish(
                    RealtimeEvent(RealtimeEventType.ERROR, error=str(exc))
                )
            finally:
                self._publish(_SENTINEL)
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=_runner, name="jarvis-realtime", daemon=True
        )
        self._thread.start()

        outcome = ready.get()
        if isinstance(outcome, Exception):
            raise RuntimeError(f"realtime connection failed: {outcome}") from outcome
        debug_log("🎙️ realtime: connected")

    async def _serve(self, config: RealtimeConfig, ready: "queue.Queue[Any]") -> None:
        """Own the socket for the lifetime of the session."""
        import websockets

        base = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        url = f"{base}?model={config.model}"
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        try:
            async with websockets.connect(url, additional_headers=headers) as ws:
                self._ws = ws
                await ws.send(json.dumps(self._session_update(config)))
                ready.put(True)
                async for raw in ws:
                    self._on_raw(raw)
        except Exception as exc:
            if ready.empty():
                ready.put(exc)
            raise
        finally:
            self._ws = None

    def _session_update(self, config: RealtimeConfig) -> Dict[str, Any]:
        """The opening frame that configures turn detection and tools.

        Server-side VAD is what buys the latency: the provider decides when
        the user has finished rather than Jarvis waiting out a silence
        timer. Transcription is requested explicitly, because without it
        the conversation never reaches memory.
        """
        session: Dict[str, Any] = {
            "modalities": ["audio", "text"],
            "input_audio_format": AUDIO_FORMAT,
            "output_audio_format": AUDIO_FORMAT,
            "turn_detection": {"type": "server_vad"},
            "input_audio_transcription": {"model": "whisper-1"},
        }
        if config.voice:
            session["voice"] = config.voice
        if config.instructions:
            session["instructions"] = config.instructions
        if config.tools:
            session["tools"] = config.tools
            session["tool_choice"] = "auto"

        return {"type": "session.update", "session": session}

    def _on_raw(self, raw: Any) -> None:
        """Decode one frame and publish whatever it means."""
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            debug_log("⚠️ realtime: dropped an unparseable frame")
            return
        event = translate_server_event(payload)
        if event is not None:
            self._publish(event)

    def _publish(self, item: Any) -> None:
        self._events.put(item)

    # ── Sending ───────────────────────────────────────────────────────

    def _submit(self, payload: Dict[str, Any]) -> None:
        """Send a client event from synchronous code."""
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
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    def send_function_result(self, result: FunctionResult) -> None:
        """Return a tool's output and let the model carry on speaking."""
        self._submit(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": result.call_id,
                    "output": result.output,
                },
            }
        )
        self._submit({"type": "response.create"})

    def interrupt(self) -> None:
        self._submit({"type": "response.cancel"})

    # ── Receiving ─────────────────────────────────────────────────────

    def events(self) -> Iterator[RealtimeEvent]:
        """Yield events until the session ends, then a final CLOSED."""
        while True:
            item = self._events.get()
            if item is _SENTINEL:
                break
            yield item
        yield RealtimeEvent(RealtimeEventType.CLOSED)

    def close(self) -> None:
        """Close the socket and stop the thread. Safe to call twice."""
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
