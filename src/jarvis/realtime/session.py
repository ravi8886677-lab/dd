"""Drive a realtime voice session on Jarvis's behalf.

The provider owns the conversation, so this runner does not decide when to
listen or when to speak. What it owns is everything the provider cannot
see: running the tools it asks for, stopping local playback the moment the
user cuts in, keeping the transcript so the conversation reaches memory,
and reporting failure so the caller can fall back to the local pipeline.

Nothing here knows a vendor's wire format. See ``backend.py`` for the
contract and ``realtime.spec.md`` for the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from ..debug import debug_log
from .backend import (
    RealtimeConfig,
    RealtimeEvent,
    RealtimeEventType,
    RealtimeVoiceBackend,
)
from .tool_bridge import dispatch_function_call


@dataclass
class ConversationTurn:
    """One side speaking, assembled from however many fragments arrived."""

    role: str  # "user" | "assistant"
    text: str


class RealtimeSession:
    """A live conversation, from connect to close.

    ``on_audio`` receives the model's speech as PCM. ``on_interrupt`` is
    called when the user cuts in and must stop local playback: audio
    already queued to the speakers keeps sounding otherwise, and the user
    hears the reply they interrupted continue over their own voice.
    """

    def __init__(
        self,
        backend: RealtimeVoiceBackend,
        db: Any,
        cfg: Any,
        *,
        on_audio: Callable[[bytes], None],
        on_interrupt: Optional[Callable[[], None]] = None,
        system_prompt: str = "",
    ) -> None:
        self._backend = backend
        self._db = db
        self._cfg = cfg
        self._on_audio = on_audio
        self._on_interrupt = on_interrupt
        self._system_prompt = system_prompt

        self._running = False
        self._turns: List[ConversationTurn] = []
        self._last_error: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self, config: RealtimeConfig) -> bool:
        """Open the session. ``False`` means the caller should fall back.

        Connection failure is an expected outcome rather than an error:
        no credentials, no network and a provider outage all land here,
        and all three should leave the user with a working local pipeline
        rather than an exception.
        """
        try:
            self._backend.connect(config)
        except Exception as exc:
            self._last_error = str(exc)
            debug_log(f"🔌 realtime: connection refused, falling back: {exc}")
            return False

        self._running = True
        debug_log("🎙️ realtime: session open")
        return True

    def close(self) -> None:
        """End the session and stop audio leaving the machine."""
        self._running = False
        try:
            self._backend.close()
        except Exception as exc:
            debug_log(f"⚠️ realtime: close failed: {exc}")

    # ── Audio in ──────────────────────────────────────────────────────

    def send_audio(self, pcm: bytes) -> None:
        """Stream microphone audio, but only while the session is open.

        The guard is the audio boundary from the spec: outside a running
        session nothing reaches a provider, so a stray buffer arriving
        during setup or teardown cannot leak.
        """
        if not self._running:
            return
        try:
            self._backend.send_audio(pcm)
        except Exception as exc:
            debug_log(f"⚠️ realtime: dropping audio chunk: {exc}")

    # ── The pump ──────────────────────────────────────────────────────

    def run_until_closed(self) -> None:
        """Consume provider events until the session ends.

        Returns on ``CLOSED`` or ``ERROR`` rather than raising, so a
        dropped socket and a clean shutdown both leave the caller holding
        a usable transcript.
        """
        try:
            for event in self._backend.events():
                if self._handle(event) is False:
                    break
        except Exception as exc:
            self._last_error = str(exc)
            debug_log(f"⚠️ realtime: event stream failed: {exc}")
        finally:
            self._running = False

    def _handle(self, event: RealtimeEvent) -> bool:
        """Act on one event. ``False`` ends the session."""
        kind = event.type

        if kind is RealtimeEventType.AUDIO_DELTA:
            if event.audio:
                self._on_audio(event.audio)
            return True

        if kind is RealtimeEventType.SPEECH_STARTED:
            self._barge_in()
            return True

        if kind is RealtimeEventType.FUNCTION_CALL:
            self._run_tool(event)
            return True

        if kind is RealtimeEventType.USER_TRANSCRIPT:
            self._append("user", event.text)
            return True

        if kind is RealtimeEventType.ASSISTANT_TEXT:
            self._append("assistant", event.text)
            return True

        if kind is RealtimeEventType.ERROR:
            self._last_error = event.error
            debug_log(f"⚠️ realtime: provider error: {event.error}")
            return False

        if kind is RealtimeEventType.CLOSED:
            debug_log("🔇 realtime: session closed")
            return False

        return True

    # ── Handlers ──────────────────────────────────────────────────────

    def _barge_in(self) -> None:
        """Stop both ends of the sound the user just talked over."""
        debug_log("✋ realtime: user interrupted")
        if self._on_interrupt is not None:
            try:
                self._on_interrupt()
            except Exception as exc:
                debug_log(f"⚠️ realtime: local playback would not stop: {exc}")
        try:
            self._backend.interrupt()
        except Exception as exc:
            debug_log(f"⚠️ realtime: provider would not stop: {exc}")

    def _run_tool(self, event: RealtimeEvent) -> None:
        """Run a requested tool and answer the model, whatever happens.

        The dispatcher turns every outcome into a result, and the send is
        guarded too: a model left waiting on a call that never comes back
        stops mid-conversation.
        """
        call = event.function_call
        if call is None:
            return

        result = dispatch_function_call(
            self._db,
            self._cfg,
            call,
            system_prompt=self._system_prompt,
            transcript=self._latest_user_text(),
        )
        try:
            self._backend.send_function_result(result)
        except Exception as exc:
            debug_log(f"⚠️ realtime: could not return {call.name} result: {exc}")

    def _latest_user_text(self) -> str:
        """The most recent thing the user said.

        Tools take the user's own words as context, and a realtime session
        has no prompt string to hand over: the transcript is the only
        record of what was asked.
        """
        for turn in reversed(self._turns):
            if turn.role == "user":
                return turn.text
        return ""

    def _append(self, role: str, text: Optional[str]) -> None:
        """Add speech to the transcript, joining fragments into turns.

        Providers stream text in pieces. Memory wants whole sentences, so
        consecutive fragments from the same speaker extend one turn.
        """
        if not text:
            return
        if self._turns and self._turns[-1].role == role:
            self._turns[-1].text += text
        else:
            self._turns.append(ConversationTurn(role=role, text=text))

    # ── Results ───────────────────────────────────────────────────────

    def transcript(self) -> List[ConversationTurn]:
        """The conversation so far, for the memory pipeline."""
        return list(self._turns)

    @property
    def last_error(self) -> Optional[str]:
        """Why the session ended, when it did not end cleanly."""
        return self._last_error
