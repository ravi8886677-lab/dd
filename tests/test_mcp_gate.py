"""Behavioural tests for the MCP confirmation gate.

The gate stands between the model and an external tool call. Its whole
value is that the model cannot open it alone: the code is printed to the
user's screen on a channel the model never reads back, and an approval
is bound to the exact call it was issued for.
"""

import pytest

from jarvis.tools.external.mcp_gate import (
    GateOutcome,
    check_confirmation,
    reset_pending,
)


@pytest.fixture(autouse=True)
def _clean_gate():
    reset_pending()
    yield
    reset_pending()


def _capture_announced(monkeypatch):
    shown = []
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_gate._announce", lambda message: shown.append(message)
    )
    return shown


def test_first_call_is_proposed_not_executed(monkeypatch):
    shown = _capture_announced(monkeypatch)
    outcome, message = check_confirmation("files", "deleteFile", {"path": "/a"}, None)

    assert outcome is GateOutcome.PROPOSED
    assert shown, "the user was never shown a code"


def test_the_code_is_not_in_the_text_handed_back_to_the_model(monkeypatch):
    shown = _capture_announced(monkeypatch)
    _, message = check_confirmation("files", "deleteFile", {"path": "/a"}, None)

    code = _extract_code(shown)
    assert code not in message, "the model can read the code it is supposed to ask for"


def _extract_code(shown):
    import re

    for line in shown:
        found = re.search(r"\b(\d{4})\b", line)
        if found:
            return found.group(1)
    raise AssertionError(f"no code found in {shown!r}")


def test_correct_code_approves_the_same_call(monkeypatch):
    shown = _capture_announced(monkeypatch)
    check_confirmation("files", "deleteFile", {"path": "/a"}, None)
    code = _extract_code(shown)

    outcome, _ = check_confirmation("files", "deleteFile", {"path": "/a"}, code)
    assert outcome is GateOutcome.APPROVED


def test_wrong_code_is_refused_and_burns_the_proposal(monkeypatch):
    shown = _capture_announced(monkeypatch)
    check_confirmation("files", "deleteFile", {"path": "/a"}, None)
    real_code = _extract_code(shown)

    outcome, _ = check_confirmation("files", "deleteFile", {"path": "/a"}, "0000")
    assert outcome is GateOutcome.REFUSED

    # The real code must not still work after a wrong guess.
    outcome_after, _ = check_confirmation("files", "deleteFile", {"path": "/a"}, real_code)
    assert outcome_after is not GateOutcome.APPROVED


def test_approval_is_bound_to_the_arguments_it_was_issued_for(monkeypatch):
    """An approval for deleting /a must not delete /b."""
    shown = _capture_announced(monkeypatch)
    check_confirmation("files", "deleteFile", {"path": "/a"}, None)
    code = _extract_code(shown)

    outcome, _ = check_confirmation("files", "deleteFile", {"path": "/b"}, code)
    assert outcome is not GateOutcome.APPROVED


def test_approval_is_bound_to_the_tool_it_was_issued_for(monkeypatch):
    shown = _capture_announced(monkeypatch)
    check_confirmation("files", "readFile", {"path": "/a"}, None)
    code = _extract_code(shown)

    outcome, _ = check_confirmation("files", "deleteFile", {"path": "/a"}, code)
    assert outcome is not GateOutcome.APPROVED


def test_approval_is_bound_to_the_server_it_was_issued_for(monkeypatch):
    shown = _capture_announced(monkeypatch)
    check_confirmation("safe-server", "doThing", {}, None)
    code = _extract_code(shown)

    outcome, _ = check_confirmation("evil-server", "doThing", {}, code)
    assert outcome is not GateOutcome.APPROVED


def test_a_code_cannot_be_spent_twice(monkeypatch):
    shown = _capture_announced(monkeypatch)
    check_confirmation("files", "deleteFile", {"path": "/a"}, None)
    code = _extract_code(shown)

    first, _ = check_confirmation("files", "deleteFile", {"path": "/a"}, code)
    assert first is GateOutcome.APPROVED

    second, _ = check_confirmation("files", "deleteFile", {"path": "/a"}, code)
    assert second is not GateOutcome.APPROVED


def test_a_code_expires(monkeypatch):
    shown = _capture_announced(monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr("jarvis.tools.external.mcp_gate.time.time", lambda: clock["now"])

    check_confirmation("files", "deleteFile", {"path": "/a"}, None)
    code = _extract_code(shown)

    clock["now"] += 10_000
    outcome, _ = check_confirmation("files", "deleteFile", {"path": "/a"}, code)
    assert outcome is not GateOutcome.APPROVED


def test_argument_key_order_does_not_break_an_approval(monkeypatch):
    """The model may re-serialise its own arguments between turns."""
    shown = _capture_announced(monkeypatch)
    check_confirmation("srv", "doThing", {"a": 1, "b": 2}, None)
    code = _extract_code(shown)

    outcome, _ = check_confirmation("srv", "doThing", {"b": 2, "a": 1}, code)
    assert outcome is GateOutcome.APPROVED


def test_proposal_text_tells_the_model_to_ask_rather_than_guess(monkeypatch):
    _capture_announced(monkeypatch)
    _, message = check_confirmation("files", "deleteFile", {"path": "/a"}, None)
    lowered = message.lower()
    assert "deletefile" in lowered
    assert "not" in lowered  # states plainly that nothing ran


# ---------------------------------------------------------------------------
# Enforcement in the tool execution path
# ---------------------------------------------------------------------------

class _Cfg:
    def __init__(self, **kw):
        self.mcps = {"srv": {"transport": "stdio", "command": "/bin/true", "args": []}}
        self.voice_debug = False
        for key, value in kw.items():
            setattr(self, key, value)


def _install_fake_mcp(monkeypatch, annotations):
    """Register one MCP tool with the given annotations and capture calls."""
    from jarvis.tools import registry

    calls = []

    class FakeClient:
        def __init__(self, cfgs):
            pass

        def invoke_tool(self, server_name, tool_name, arguments):
            calls.append((server_name, tool_name, dict(arguments or {})))
            return {"text": "did it", "isError": False}

    monkeypatch.setattr(registry, "MCPClient", FakeClient)
    monkeypatch.setattr(
        registry,
        "get_cached_mcp_tools",
        lambda: {
            "srv__doThing": registry.ToolSpec(
                name="srv__doThing",
                description="Does a thing",
                inputSchema={"type": "object"},
                annotations=annotations,
            )
        },
    )
    return calls


def _run(cfg, args):
    from jarvis.tools.registry import run_tool_with_retries

    return run_tool_with_retries(
        db=None, cfg=cfg, tool_name="srv__doThing", tool_args=args,
        system_prompt="", original_prompt="", redacted_text="",
    )


def test_destructive_tool_is_not_invoked_without_a_code(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    _capture_announced(monkeypatch)

    result = _run(_Cfg(), {"x": 1})

    assert calls == [], "the server was called before the user approved"
    assert "PROPOSED" in (result.reply_text or "")


def test_read_only_tool_runs_without_a_code(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, {"readOnlyHint": True})

    result = _run(_Cfg(), {"x": 1})

    assert len(calls) == 1
    assert result.success is True


def test_approved_destructive_call_reaches_the_server(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    shown = _capture_announced(monkeypatch)

    _run(_Cfg(), {"x": 1})
    code = _extract_code(shown)
    result = _run(_Cfg(), {"x": 1, "confirmation_code": code})

    assert len(calls) == 1
    assert result.success is True


def test_confirmation_code_is_never_forwarded_to_the_server(monkeypatch):
    """It is Jarvis's argument; a server must not see or record it."""
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    shown = _capture_announced(monkeypatch)

    _run(_Cfg(), {"x": 1})
    code = _extract_code(shown)
    _run(_Cfg(), {"x": 1, "confirmation_code": code})

    assert calls[0][2] == {"x": 1}


def test_policy_off_lets_a_destructive_call_through(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    _run(_Cfg(mcp_confirm="off"), {"x": 1})
    assert len(calls) == 1


def test_unannotated_policy_gates_a_silent_server(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, None)
    _capture_announced(monkeypatch)
    _run(_Cfg(mcp_confirm="unannotated"), {"x": 1})
    assert calls == []


def test_a_server_claiming_read_only_cannot_disable_the_all_policy(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, {"readOnlyHint": True})
    _capture_announced(monkeypatch)
    _run(_Cfg(mcp_confirm="all"), {"x": 1})
    assert calls == [], "a server annotation overrode the user's policy"
