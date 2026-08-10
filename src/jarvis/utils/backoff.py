"""Backoff helper for waiting out an upstream rate limit.

Model downloads (Whisper weights, Piper voices) come from HuggingFace, which
answers 429 under load. The retry loops that handle it need to pause, and
their tests need to observe the schedule.

Importing this by name into each module gives those tests a seam of their
own: ``patch("jarvis.output.tts.retry_backoff_sleep")`` replaces one binding
in one module. Patching ``time.sleep`` through a module attribute instead
(``patch("jarvis.output.tts.time.sleep")``) resolves to the shared ``time``
module and swaps sleep for the whole process, so every other thread's sleep
is recorded by the mock as well and any assertion on the call list becomes a
race against unrelated background work.
"""

from __future__ import annotations

import time


def retry_backoff_sleep(seconds: float) -> None:
    """Pause ``seconds`` between attempts at a rate-limited download."""
    time.sleep(seconds)
