"""
Tests for how a query collection decides it is finished.

The collection window exists so a user who says the wake word alone gets
time to think of the query. Once the collection actually holds a query the
VAD has already proved the user stopped talking, so the listener waits only
a short follow-on grace before dispatching. These tests pin that split, and
the guard that keeps a resumed sentence from being cut in half.
"""

import time

import pytest

# CI runs `pytest -m unit`. Without this every test in this file is
# deselected there and the guards below only fire on a local pre-push hook.
pytestmark = pytest.mark.unit

from jarvis.listening.state_manager import StateManager


@pytest.fixture(autouse=True)
def _warm_face_state_import():
    """Take the optional desktop_app import out of the timings under test.

    ``start_collection`` tries to drive the face widget, and the first attempt
    pays the import machinery's search cost. That lands inside the very
    interval these tests measure, so it is paid once up front instead.
    """
    StateManager().start_collection("warm")


class TestFollowOnGrace:
    """A collection holding a query finishes on the grace, not the window."""

    def test_query_in_hand_dispatches_on_the_grace(self):
        """Text already collected means the short grace decides, not the window."""
        sm = StateManager(voice_collect_seconds=5.0, follow_on_seconds=0.05)
        sm.start_collection("what is the weather")

        assert sm.check_collection_timeout() is False

        time.sleep(0.06)
        assert sm.check_collection_timeout() is True

    def test_empty_collection_keeps_the_full_window(self):
        """The wake word alone leaves nothing to answer, so the long wait stands."""
        sm = StateManager(voice_collect_seconds=5.0, follow_on_seconds=0.05)
        sm.start_collection("")

        time.sleep(0.06)
        assert sm.check_collection_timeout() is False

    def test_grace_starts_once_the_query_arrives(self):
        """Speech during an empty collection switches it onto the grace."""
        sm = StateManager(voice_collect_seconds=5.0, follow_on_seconds=0.05)
        sm.start_collection("")
        sm.add_to_collection("what is the weather")

        assert sm.check_collection_timeout() is False
        time.sleep(0.06)
        assert sm.check_collection_timeout() is True

    def test_grace_never_outlasts_the_window(self):
        """A window shorter than the grace still decides the whole wait."""
        sm = StateManager(voice_collect_seconds=0.05, follow_on_seconds=5.0)
        sm.start_collection("test")

        time.sleep(0.06)
        assert sm.check_collection_timeout() is True


class TestSpeechInProgressGuard:
    """A sentence the user is still speaking is not finalised underneath them."""

    def test_active_speech_suppresses_the_silence_timeout(self):
        """Mid-utterance, the collection stays open however long the grace was."""
        sm = StateManager(voice_collect_seconds=5.0, follow_on_seconds=0.05)
        sm.start_collection("remind me to")

        time.sleep(0.06)
        assert sm.check_collection_timeout(speech_active=True) is False

    def test_collection_finalises_once_speech_stops(self):
        """The guard holds the turn open, it does not cancel it."""
        sm = StateManager(voice_collect_seconds=5.0, follow_on_seconds=0.05)
        sm.start_collection("remind me to")

        time.sleep(0.06)
        assert sm.check_collection_timeout(speech_active=True) is False
        assert sm.check_collection_timeout(speech_active=False) is True

    def test_active_speech_does_not_defeat_the_max_duration(self):
        """Continuous noise must not hold a collection open forever."""
        sm = StateManager(
            voice_collect_seconds=5.0,
            follow_on_seconds=5.0,
            max_collect_seconds=0.05,
        )
        sm.start_collection("test")

        time.sleep(0.06)
        assert sm.check_collection_timeout(speech_active=True) is True


class TestListenerWiring:
    """The listener must actually hand the config values to the state manager."""

    def test_state_manager_receives_the_configured_grace(self):
        """A config value that never reaches the state manager changes nothing."""
        import jarvis.listening.listener as listener_module

        cfg = _voice_config(voice_collect_seconds=4.5, voice_follow_on_seconds=0.6)
        listener = listener_module.VoiceListener.__new__(listener_module.VoiceListener)
        listener.cfg = cfg
        state_manager = listener_module.VoiceListener._build_state_manager(listener)

        assert state_manager.voice_collect_seconds == pytest.approx(4.5)
        assert state_manager.follow_on_seconds == pytest.approx(0.6)

    def test_listener_does_not_dispatch_while_speech_is_active(self):
        """The guard is only real if the listener reports speech to it.

        `check_collection_timeout` defaults `speech_active` to False, so a
        dropped keyword leaves every unit test above passing while the
        listener finalises sentences underneath the user again. Assert the
        behaviour at the listener, where the two halves meet.
        """
        listener = _wired_listener(is_speech_active=True)
        listener.state_manager.start_collection("remind me to")
        time.sleep(0.06)

        listener._check_query_timeout()

        assert listener.dispatched == []
        assert listener.state_manager.get_pending_query() == "remind me to"

    def test_listener_dispatches_once_speech_stops(self):
        """The same listener finalises as soon as the endpointer is out of speech."""
        listener = _wired_listener(is_speech_active=False)
        listener.state_manager.start_collection("remind me to")
        time.sleep(0.06)

        listener._check_query_timeout()

        assert listener.dispatched == ["remind me to"]


def _wired_listener(*, is_speech_active: bool):
    """A VoiceListener with only the collaborators `_check_query_timeout` uses."""
    import jarvis.listening.listener as listener_module

    listener = listener_module.VoiceListener.__new__(listener_module.VoiceListener)
    listener.cfg = _voice_config(voice_collect_seconds=5.0)
    listener.state_manager = StateManager(
        voice_collect_seconds=5.0, follow_on_seconds=0.05
    )
    listener.is_speech_active = is_speech_active
    listener.dispatched = []
    listener._dispatch_query = listener.dispatched.append
    return listener


class TestConfigDefaults:
    """The shipped default has to be short enough to matter."""

    def test_follow_on_default_is_a_fraction_of_the_collect_window(self):
        """A grace as long as the window would leave the dead air in place."""
        from jarvis.config import load_settings

        settings = load_settings()
        assert settings.voice_follow_on_seconds < settings.voice_collect_seconds


def _voice_config(**overrides):
    """Minimal stand-in carrying only the attributes under test."""
    from types import SimpleNamespace

    values = {
        "hot_window_seconds": 20.0,
        "echo_tolerance": 0.3,
        "voice_collect_seconds": 4.5,
        "voice_follow_on_seconds": 0.6,
        "voice_max_collect_seconds": 180.0,
        "voice_debug": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)
