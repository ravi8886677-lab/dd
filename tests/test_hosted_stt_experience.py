"""What a misconfigured hosted recogniser does to the person using it.

Both defects here are invisible in the code and obvious at the microphone.

A hosted call inherited the adapter's 15-second default timeout, and every
failure was reported through `debug_log`, which is off unless the user
turned it on. On a hosted setup with a wrong or rate-limited key, that is
fifteen seconds of silence per sentence with nothing said about why. It
does not look like a failing API call, it looks like a broken assistant.

The second is the cost of the local fallback existing. Local Whisper is
the fallback and has to stay the fallback, so it may not be removed - but
it was being downloaded and held in memory at startup on setups where it
never runs. Loading it when it is first needed keeps the guarantee and
drops roughly a gigabyte from a hosted install.

These assert the experience, not the implementation: how long a caller is
made to wait, whether it is told, and whether the utterance survives.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jarvis.speech.backend import Transcription

pytestmark = pytest.mark.unit


def _cfg(**overrides):
    cfg = MagicMock()
    cfg.whisper_model = "base"
    cfg.whisper_device = "auto"
    cfg.whisper_compute_type = "int8"
    cfg.whisper_backend = "faster-whisper"
    cfg.sample_rate = 16000
    cfg.vad_enabled = False
    cfg.vad_aggressiveness = 2
    cfg.echo_tolerance = 0.3
    cfg.echo_energy_threshold = 2.0
    cfg.hot_window_seconds = 3.0
    cfg.hot_window_enabled = True
    cfg.voice_collect_seconds = 2.0
    cfg.voice_max_collect_seconds = 60.0
    cfg.voice_device = None
    cfg.voice_debug = False
    cfg.voice_min_energy = 0.0045
    cfg.tune_enabled = False
    cfg.wake_word = "jarvis"
    cfg.wake_aliases = []
    cfg.wake_fuzzy_ratio = 0.78
    cfg.stop_commands = ["stop"]
    cfg.transcript_buffer_duration_sec = 120.0
    cfg.whisper_min_confidence = 0.3
    cfg.whisper_no_speech_threshold = 0.5
    cfg.stt_provider = "local"
    cfg.stt_base_url = ""
    cfg.stt_api_key = ""
    cfg.stt_model = ""
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _listener(cfg=None):
    cfg = cfg or _cfg()
    with patch("jarvis.listening.listener.webrtcvad", None), \
         patch("jarvis.listening.listener.sd", None), \
         patch("jarvis.listening.listener.np", None), \
         patch("jarvis.listening.listener.create_intent_judge", return_value=None):
        from jarvis.listening.listener import VoiceListener
        return VoiceListener(MagicMock(), cfg, MagicMock(), MagicMock())


_NOT_PASSED = object()


class _RecordingBackend:
    """Captures the timeout it was handed.

    The default is a sentinel rather than the adapter's real 15.0, so that
    "the listener passed nothing" is distinguishable from "the listener
    passed 15.0". With the real default here, the test asserting that a
    timeout is supplied passes against the code that supplies none.
    """

    def __init__(self, result=None, error: str | None = None):
        self.result = result
        self.last_error = error
        self.timeouts: list = []

    def transcribe_detailed(self, audio, sample_rate=16000, timeout_sec=_NOT_PASSED):
        self.timeouts.append(timeout_sec)
        return self.result


class TestTheCallerIsNotMadeToWaitFifteenSeconds:
    """A voice loop cannot spend the adapter's default on every sentence."""

    def test_a_timeout_is_passed_explicitly(self) -> None:
        listener = _listener()
        backend = _RecordingBackend(result=Transcription(text="hello"))
        listener._hosted_stt = backend
        listener._hosted_stt_resolved = True

        listener._transcribe_hosted(object())

        assert backend.timeouts, "no hosted call was made"
        assert backend.timeouts[0] is not _NOT_PASSED, (
            "the listener passed no timeout, so the adapter's 15-second "
            "default applies to every utterance"
        )

    def test_the_timeout_is_short_enough_for_a_conversation(self) -> None:
        listener = _listener()
        backend = _RecordingBackend(result=Transcription(text="hello"))
        listener._hosted_stt = backend
        listener._hosted_stt_resolved = True

        listener._transcribe_hosted(object())

        assert backend.timeouts[0] <= 10.0, (
            f"a {backend.timeouts[0]}s ceiling is dead air between sentences"
        )

    def test_the_timeout_is_configurable(self) -> None:
        listener = _listener(_cfg(stt_timeout_sec=3.5))
        backend = _RecordingBackend(result=Transcription(text="hello"))
        listener._hosted_stt = backend
        listener._hosted_stt_resolved = True

        listener._transcribe_hosted(object())

        assert backend.timeouts[0] == 3.5


class TestAFailingProviderSaysSoOutLoud:
    """debug_log is off by default, so a failure there is silence."""

    def test_the_user_is_told_when_the_hosted_provider_fails(self, capsys) -> None:
        listener = _listener()
        listener._hosted_stt = _RecordingBackend(result=None, error="HTTP 401")
        listener._hosted_stt_resolved = True
        capsys.readouterr()  # discard construction noise

        listener._transcribe_hosted(object())

        out = capsys.readouterr().out
        assert out.strip(), (
            "a hosted failure printed nothing. On a hosted setup with a bad "
            "key this is silence with no explanation."
        )
        assert "local" in out.lower(), "the message must say what happens next"

    def test_the_reason_reaches_the_user(self, capsys) -> None:
        listener = _listener()
        listener._hosted_stt = _RecordingBackend(result=None, error="HTTP 401")
        listener._hosted_stt_resolved = True
        capsys.readouterr()  # discard construction noise

        listener._transcribe_hosted(object())

        assert "401" in capsys.readouterr().out, (
            "a bad key and a rate limit must not look identical to the user"
        )

    def test_it_does_not_repeat_on_every_utterance(self, capsys) -> None:
        listener = _listener()
        listener._hosted_stt = _RecordingBackend(result=None, error="HTTP 401")
        listener._hosted_stt_resolved = True
        capsys.readouterr()  # discard construction noise

        for _ in range(5):
            listener._transcribe_hosted(object())

        printed = capsys.readouterr().out.strip().splitlines()
        assert len(printed) == 1, (
            f"the warning printed {len(printed)} times; a per-sentence warning "
            "is its own kind of broken"
        )

    def test_a_recovery_is_announced_again_if_it_fails_later(self, capsys) -> None:
        listener = _listener()
        backend = _RecordingBackend(result=None, error="HTTP 429")
        listener._hosted_stt = backend
        listener._hosted_stt_resolved = True
        capsys.readouterr()  # discard construction noise

        listener._transcribe_hosted(object())
        capsys.readouterr()

        backend.result = Transcription(text="back again")
        backend.last_error = None
        listener._transcribe_hosted(object())
        capsys.readouterr()

        backend.result = None
        backend.last_error = "HTTP 429"
        listener._transcribe_hosted(object())

        assert capsys.readouterr().out.strip(), (
            "a second outage after a recovery must be announced, or a "
            "degraded session stays quiet about being degraded"
        )


class TestLocalWhisperLoadsWhenItIsNeeded:
    """The fallback guarantee is kept; the standing cost is not."""

    def test_a_hosted_setup_does_not_load_whisper_at_startup(self) -> None:
        cfg = _cfg(
            stt_provider="openai_compatible",
            stt_base_url="https://example.invalid/v1",
            stt_api_key="key",
        )
        listener = _listener(cfg)
        assert listener._should_defer_local_whisper() is True, (
            "a configured hosted provider must not pay for a local model "
            "download and ~1GB of RAM it may never use"
        )

    def test_a_local_setup_still_loads_it_at_startup(self) -> None:
        listener = _listener(_cfg(stt_provider="local"))
        assert listener._should_defer_local_whisper() is False

    def test_a_hosted_provider_with_no_endpoint_still_loads_it(self) -> None:
        """Unconfigured means local, so local must be ready."""
        listener = _listener(_cfg(stt_provider="openai_compatible", stt_base_url=""))
        assert listener._should_defer_local_whisper() is False

    def test_the_deferred_model_is_loaded_on_first_local_transcription(self) -> None:
        cfg = _cfg(
            stt_provider="openai_compatible",
            stt_base_url="https://example.invalid/v1",
            stt_api_key="key",
        )
        listener = _listener(cfg)
        listener.model = None
        listener._whisper_backend = "faster-whisper"

        with patch.object(
            listener, "_initialise_local_whisper", return_value=False
        ) as init:
            listener._transcribe_locally(object())

        assert init.called, (
            "the fallback never loaded its model, so a hosted failure would "
            "lose the utterance instead of degrading to local"
        )
