"""Tests for the rate-limit backoff seam.

The retry loops that wait out a HuggingFace 429 are tested by asserting on
the backoff schedule. That assertion is only trustworthy if the mock records
calls made by the code under test and nothing else.
"""

import threading
import time
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestPatchingTheSeamStaysLocal:
    """Patching the seam must not swap sleep for the whole process."""

    def test_patching_tts_seam_leaves_global_sleep_alone(self):
        real_sleep = time.sleep
        with patch("src.jarvis.output.tts.retry_backoff_sleep"):
            assert time.sleep is real_sleep

    def test_patching_listener_seam_leaves_global_sleep_alone(self):
        real_sleep = time.sleep
        with patch("jarvis.listening.listener.retry_backoff_sleep"):
            assert time.sleep is real_sleep

    def test_patching_one_module_does_not_patch_the_other(self):
        """Each module holds its own binding, so the seams are independent."""
        import jarvis.listening.listener as listener
        original = listener.retry_backoff_sleep
        with patch("src.jarvis.output.tts.retry_backoff_sleep"):
            assert listener.retry_backoff_sleep is original

    def test_background_thread_sleeps_are_not_recorded(self):
        """The failure this seam prevents: an unrelated thread sleeping
        during the test landing in the mock's call list and breaking
        assertions on the backoff schedule."""
        with patch("src.jarvis.output.tts.retry_backoff_sleep") as mock_sleep:
            done = threading.Event()

            def noisy_neighbour():
                for _ in range(5):
                    time.sleep(0.001)
                done.set()

            t = threading.Thread(target=noisy_neighbour)
            t.start()
            t.join(timeout=5)
            assert done.is_set(), "background thread did not finish"
            assert mock_sleep.call_args_list == []
