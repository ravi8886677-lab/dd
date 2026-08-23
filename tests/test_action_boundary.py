"""Every tool call crosses one boundary, and the boundary keeps the record.

Before this, two tools gated themselves and everything else — writing
files under `$HOME`, launching a process, fetching a page — ran ungated
and unrecorded. There was no place that knew a tool had been called, so
"every external action attributable" had nothing to attribute to.

The boundary is `run_tool_with_retries`. It decides, records the
decision before anything executes, runs the call, and records what came
back. The tests here are about that ordering and that completeness,
because the ordering is what makes the record evidence rather than a
summary written by the winner.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis.audit import ActionLog, Decision, Outcome, Verification
from jarvis.tools import registry
from jarvis.tools.types import ToolExecutionResult

pytestmark = pytest.mark.unit


@pytest.fixture
def recorded(tmp_path, mock_config):
    """Run tools against a real log, and hand back a reader for it."""
    from jarvis.audit import recorder

    db_path = str(tmp_path / "jarvis.db")
    mock_config.db_path = db_path
    recorder.reset_for_tests()
    recorder.configure(db_path=db_path)

    log = ActionLog(db_path)
    try:
        yield mock_config, log
    finally:
        log.close()
        recorder.reset_for_tests()


def _run(cfg, tool_name, tool_args=None):
    return registry.run_tool_with_retries(
        db=None,
        cfg=cfg,
        tool_name=tool_name,
        tool_args=tool_args or {},
        system_prompt="",
        original_prompt="",
        redacted_text="",
    )


class TestEveryCallIsRecorded:
    def test_a_builtin_call_leaves_a_decision_and_an_outcome(self, recorded):
        cfg, log = recorded

        _run(cfg, "stop")

        assert [e.entry for e in log.get_entries()] == ["decision", "outcome"]

    def test_the_record_names_the_tool_and_where_it_came_from(self, recorded):
        cfg, log = recorded

        _run(cfg, "stop")

        action = log.get_actions()[0]
        assert action.tool_name == "stop"
        assert action.tool_source == "builtin"

    def test_an_unknown_tool_is_still_recorded(self, recorded):
        """A call that names a tool that does not exist is worth seeing."""
        cfg, log = recorded

        _run(cfg, "definitelyNotATool")

        action = log.get_actions()[0]
        assert action.tool_name == "definitelyNotATool"
        assert action.outcome == Outcome.ERROR

    def test_arguments_reach_the_log_scrubbed(self, recorded):
        cfg, log = recorded

        _run(cfg, "definitelyNotATool", {"token": "ghp_" + "b" * 36})

        assert "ghp_" not in log.get_actions()[0].arguments_redacted


class TestTheDecisionLandsBeforeExecution:
    def test_a_call_that_never_returns_still_left_its_attempt(self, recorded):
        """The evidence of an attempt cannot depend on the attempt finishing."""
        cfg, log = recorded

        with patch.dict(registry.BUILTIN_TOOLS, {}, clear=False):
            with patch.object(
                registry.BUILTIN_TOOLS["stop"], "execute",
                side_effect=KeyboardInterrupt("process died"),
            ):
                with pytest.raises(KeyboardInterrupt):
                    _run(cfg, "stop")

        actions = log.get_actions()
        assert len(actions) == 1
        assert actions[0].outcome is None, "an unfinished action reads as unfinished"


class TestTheOutcomeIsWhatHappened:
    def test_a_failing_tool_is_recorded_as_an_error(self, recorded):
        cfg, log = recorded

        with patch.object(
            registry.BUILTIN_TOOLS["stop"], "execute",
            return_value=ToolExecutionResult(
                success=False, reply_text=None, error_message="it did not work",
            ),
        ):
            _run(cfg, "stop")

        action = log.get_actions()[0]
        assert action.outcome == Outcome.ERROR
        assert "it did not work" in action.outcome_detail

    def test_a_tool_that_raises_is_recorded_before_the_error_travels_on(
        self, recorded,
    ):
        """The boundary witnesses; it does not change who handles what."""
        cfg, log = recorded

        with patch.object(
            registry.BUILTIN_TOOLS["stop"], "execute",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError):
                _run(cfg, "stop")

        action = log.get_actions()[0]
        assert action.outcome == Outcome.ERROR
        assert "boom" in action.outcome_detail

    def test_nothing_is_confirmed_unless_a_tool_checked(self, recorded):
        cfg, log = recorded

        _run(cfg, "stop")

        assert log.get_actions()[0].verification == Verification.NOT_CHECKED


class TestARefusalIsRecordedAsARefusal:
    def test_the_boundary_records_who_refused_and_why(self, recorded):
        cfg, log = recorded
        cfg.computer_use_confirm = "risky"

        from jarvis import approval
        approval.revoke()

        _run(cfg, "computerUse", {"action": "click", "x": 10, "y": 10})

        action = log.get_actions()[0]
        assert action.decision == Decision.DENIED
        assert action.policy_rule_id
        assert "yolo" in action.decision_reason.lower()

    def test_a_refused_action_never_executes(self, recorded):
        cfg, log = recorded
        cfg.computer_use_confirm = "risky"

        from jarvis import approval
        approval.revoke()

        result = _run(cfg, "computerUse", {"action": "click", "x": 10, "y": 10})

        action = log.get_actions()[0]
        assert result.success is False
        # Nothing ran, so there is no outcome. `decision` is what tells a
        # reader this was refused rather than left unfinished.
        assert action.outcome is None
        assert action.decision == Decision.DENIED


class TestTheLogNeverBreaksTheCall:
    def test_a_broken_log_does_not_stop_a_tool_running(self, recorded):
        """Witnessing an action must not be able to prevent it.

        Authorisation fails closed; the record does not, because a
        failure to write is not a reason to refuse work the user asked
        for. The gap in the log is itself visible.
        """
        cfg, _ = recorded
        from jarvis.audit import recorder

        with patch.object(
            recorder, "record_attempt", side_effect=RuntimeError("disk gone"),
        ):
            result = _run(cfg, "stop")

        assert result is not None
