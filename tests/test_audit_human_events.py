"""Opening the YOLO window belongs on the same record as what it allows.

A log that shows Jarvis driving the mouse, with nothing to say the user
opened the window that allowed it, cannot explain itself. The grant is
the most consequential thing the user does and until now it existed only
in memory: a restart erased the evidence that it had ever been open.

The recording must not be reachable from the tool layer, for the same
reason granting is not: `tests/test_yolo.py` asserts no tool can enable
YOLO for itself, and a log entry is not a way in.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis import approval
from jarvis.audit import ActionLog, recorder

pytestmark = pytest.mark.unit


@pytest.fixture
def log(tmp_path):
    db_path = str(tmp_path / "jarvis.db")
    recorder.reset_for_tests()
    recorder.configure(db_path=db_path)
    approval.revoke()
    log = ActionLog(db_path)
    try:
        yield log
    finally:
        approval.revoke()
        log.close()
        recorder.reset_for_tests()


class TestTheWindowIsOnTheRecord:
    def test_granting_is_recorded(self, log):
        approval.grant(30)

        entries = [e for e in log.get_entries() if e.tool_source == "human"]
        assert [e.tool_name for e in entries] == ["yolo.granted"]

    def test_the_grant_says_how_long_it_was_for(self, log):
        approval.grant(30)

        entry = [e for e in log.get_entries() if e.tool_source == "human"][0]
        assert "30" in entry.decision_reason

    def test_revoking_is_recorded(self, log):
        approval.grant(30)
        approval.revoke()

        names = [e.tool_name for e in log.get_entries() if e.tool_source == "human"]
        assert names == ["yolo.granted", "yolo.revoked"]

    def test_revoking_a_closed_window_records_nothing(self, log):
        """Nothing happened, so there is nothing to record."""
        approval.revoke()

        assert [e for e in log.get_entries() if e.tool_source == "human"] == []

    def test_a_refused_grant_is_not_recorded_as_one(self, log):
        assert approval.grant("not a duration") is False

        assert [e for e in log.get_entries() if e.tool_source == "human"] == []


class TestRecordingCannotBecomeAWayIn:
    def test_a_broken_log_does_not_stop_a_grant(self, log):
        with patch.object(
            recorder, "record_human_event", side_effect=RuntimeError("disk gone"),
        ):
            assert approval.grant(15) is True

        assert approval.is_active() is True

    def test_the_approval_module_does_not_import_the_tool_layer(self):
        """The grant path must stay unreachable from a tool call."""
        import jarvis.approval as module

        source = __import__("pathlib").Path(module.__file__).read_text()
        assert "from .tools" not in source
        assert "import jarvis.tools" not in source
