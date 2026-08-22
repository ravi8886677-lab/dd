"""What Jarvis did, and whether it actually happened.

Two of the project's non-negotiables are currently the ones it does not
meet: no fabricated completion claims, and every external action
attributable. Both need the same thing underneath, a record that says
what was asked, what was decided, and what came back.

The record is written in two entries. The decision entry lands *before*
anything executes, so an action that crashed the process still left
evidence that it was attempted. The outcome entry lands after, and it is
the only thing entitled to say an action succeeded: a function returning
is not the same as the world having changed.
"""

from __future__ import annotations

import pytest

from jarvis.audit import ActionLog, Decision, Outcome, Verification

pytestmark = pytest.mark.unit


@pytest.fixture
def log(tmp_path):
    log = ActionLog(str(tmp_path / "jarvis.db"))
    yield log
    log.close()


class TestTheDecisionIsRecordedBeforeAnythingRuns:
    def test_an_attempt_appears_before_its_outcome(self, log):
        log.record_decision(tool_name="localFiles", tool_source="builtin")

        entries = log.get_entries()

        assert [e.entry for e in entries] == ["decision"]

    def test_it_carries_who_and_where(self, log):
        log.record_decision(
            tool_name="localFiles",
            tool_source="builtin",
            user_id="u1",
            workspace_id="w1",
            device_id="d1",
        )

        entry = log.get_entries()[0]

        assert (entry.user_id, entry.workspace_id, entry.device_id) == ("u1", "w1", "d1")

    def test_an_action_that_never_completed_is_visible_as_such(self, log):
        """The process died mid-call. The attempt is still on the record."""
        log.record_decision(tool_name="openApp", tool_source="builtin")

        assert log.get_actions()[0].outcome is None

    def test_a_denial_says_which_rule_denied_it(self, log):
        log.record_decision(
            tool_name="computerUse",
            tool_source="builtin",
            decision=Decision.DENIED,
            decision_reason="YOLO mode is off",
            policy_rule_id="computer_use.confirm",
        )

        entry = log.get_entries()[0]

        assert entry.decision == Decision.DENIED
        assert entry.decision_reason == "YOLO mode is off"
        assert entry.policy_rule_id == "computer_use.confirm"


class TestArgumentsAreRecordedWithoutSecrets:
    def test_a_token_in_an_argument_does_not_reach_the_row(self, log):
        log.record_decision(
            tool_name="fetchWebPage",
            tool_source="builtin",
            arguments={"url": "https://x.test", "token": "ghp_" + "a" * 36},
        )

        stored = log.get_entries()[0].arguments_redacted

        assert "ghp_" not in stored
        assert "REDACTED" in stored

    def test_the_writer_redacts_rather_than_trusting_its_caller(self, log):
        """Callers pass raw arguments; the log is what makes them safe."""
        log.record_decision(
            tool_name="localFiles",
            tool_source="builtin",
            arguments={"body": "my password: hunter2"},
        )

        assert "hunter2" not in log.get_entries()[0].arguments_redacted

    def test_a_secret_is_recorded_by_length_never_by_value(self, log):
        log.record_decision(
            tool_name="someTool",
            tool_source="mcp",
            arguments={"api_key": "s3cr3t-value-here"},
            secrets={"api_key": "s3cr3t-value-here"},
        )

        entry = log.get_entries()[0]

        assert "s3cr3t" not in entry.arguments_redacted
        assert "api_key" in entry.arguments_redacted
        assert "17" in entry.arguments_redacted  # its length, not its value

    def test_arguments_are_capped_so_one_call_cannot_flood_the_log(self, log):
        log.record_decision(
            tool_name="localFiles",
            tool_source="builtin",
            arguments={"body": "x" * 50_000},
        )

        assert len(log.get_entries()[0].arguments_redacted) <= 4096

    def test_a_large_argument_is_bounded_before_it_is_scrubbed(self):
        """The cap has to come before the regexes, not after them.

        `redact` carries a lookahead that rescans to the end of the
        string at every position, so its cost is quadratic in the input.
        Writing a file logs its content, so scrubbing first and
        truncating afterwards means a big write spends minutes inside
        the audit path — on the request path, for a row that gets cut to
        4KB anyway.
        """
        import time

        from jarvis.audit.log import summarise_arguments

        started = time.perf_counter()
        rendered = summarise_arguments({"body": "x" * 400_000})
        took = time.perf_counter() - started

        assert len(rendered) <= 4096
        assert took < 1.0, f"scrubbing a large argument took {took:.1f}s"

    def test_a_secret_near_the_cap_is_still_scrubbed(self):
        """Bounding the input must not open a hole at the boundary."""
        from jarvis.audit.log import summarise_arguments

        token = "ghp_" + "d" * 36
        rendered = summarise_arguments({"a": "y" * 4000, "b": token})

        assert token not in rendered


class TestTheOutcomeIsASeparateEntry:
    def test_it_correlates_with_the_decision(self, log):
        action_id = log.record_decision(tool_name="localFiles", tool_source="builtin")

        log.record_outcome(action_id, outcome=Outcome.OK)

        assert [e.entry for e in log.get_entries()] == ["decision", "outcome"]
        assert {e.action_id for e in log.get_entries()} == {action_id}

    def test_the_decision_entry_is_not_rewritten(self, log):
        """An audit entry that can be edited after the fact is not evidence."""
        action_id = log.record_decision(tool_name="localFiles", tool_source="builtin")
        before = log.get_entries()[0]

        log.record_outcome(action_id, outcome=Outcome.ERROR, detail="no such file")
        after = log.get_entries()[0]

        assert after == before

    def test_a_failure_is_recorded_with_its_detail(self, log):
        action_id = log.record_decision(tool_name="openApp", tool_source="builtin")

        log.record_outcome(action_id, outcome=Outcome.ERROR, detail="no such app")

        action = log.get_actions()[0]
        assert action.outcome == Outcome.ERROR
        assert action.outcome_detail == "no such app"

    def test_an_outcome_detail_is_scrubbed_too(self, log):
        action_id = log.record_decision(tool_name="fetchWebPage", tool_source="builtin")

        log.record_outcome(
            action_id, outcome=Outcome.ERROR, detail="401 for token=abc123def456",
        )

        assert "abc123def456" not in log.get_actions()[0].outcome_detail


class TestVerificationIsNotAssumed:
    def test_unchecked_is_the_default_not_success(self, log):
        """A tool with nothing to check must not read as confirmed."""
        action_id = log.record_decision(tool_name="openApp", tool_source="builtin")

        log.record_outcome(action_id, outcome=Outcome.OK)

        assert log.get_actions()[0].verification == Verification.NOT_CHECKED

    def test_a_confirmed_action_says_so(self, log):
        action_id = log.record_decision(tool_name="localFiles", tool_source="builtin")

        log.record_outcome(
            action_id, outcome=Outcome.OK, verification=Verification.CONFIRMED,
        )

        assert log.get_actions()[0].verification == Verification.CONFIRMED

    def test_a_check_that_failed_is_not_an_error_and_not_a_success(self, log):
        """The call returned cleanly and the world did not change."""
        action_id = log.record_decision(tool_name="localFiles", tool_source="builtin")

        log.record_outcome(
            action_id, outcome=Outcome.OK, verification=Verification.FAILED,
        )

        action = log.get_actions()[0]
        assert action.outcome == Outcome.OK
        assert action.verification == Verification.FAILED


class TestHumanControlIsOnTheSameLog:
    """A YOLO grant is the most consequential thing the user does."""

    def test_a_grant_is_recorded(self, log):
        log.record_human_event("yolo.granted", detail="30 minutes")

        entry = log.get_entries()[0]

        assert entry.tool_name == "yolo.granted"
        assert entry.tool_source == "human"

    def test_a_lapse_is_recorded(self, log):
        log.record_human_event("yolo.lapsed")

        assert log.get_entries()[0].tool_name == "yolo.lapsed"


class TestReadingTheLog:
    def test_actions_come_back_newest_first(self, log):
        first = log.record_decision(tool_name="a", tool_source="builtin")
        second = log.record_decision(tool_name="b", tool_source="builtin")

        assert [a.action_id for a in log.get_actions()] == [second, first]

    def test_the_schema_arrives_with_the_store(self, tmp_path):
        log = ActionLog(str(tmp_path / "nested" / "jarvis.db"))
        try:
            assert log.get_actions() == []
        finally:
            log.close()

    def test_a_second_process_sees_what_the_first_wrote(self, tmp_path):
        path = str(tmp_path / "jarvis.db")
        daemon, dashboard = ActionLog(path), ActionLog(path)
        try:
            action_id = daemon.record_decision(tool_name="a", tool_source="builtin")

            assert [a.action_id for a in dashboard.get_actions()] == [action_id]
        finally:
            daemon.close()
            dashboard.close()
