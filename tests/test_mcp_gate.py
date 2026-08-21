"""Behavioural tests for the MCP gate under YOLO mode.

A consequential external tool call runs when the user has opened the
YOLO window and does not when they have not. What must never happen is
the model opening that window for itself: its instructions and its tool
results both contain text other people wrote.
"""

import pytest

from jarvis import approval
from jarvis.tools.external.mcp_gate import GateOutcome, check_allowed

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean():
    approval.revoke()
    yield
    approval.revoke()


class _Cfg:
    def __init__(self, **kw):
        self.mcps = {"srv": {"transport": "stdio", "command": "/bin/true", "args": []}}
        self.voice_debug = False
        for key, value in kw.items():
            setattr(self, key, value)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

def test_a_call_is_blocked_while_yolo_is_off():
    outcome, message = check_allowed("files", "deleteFile", {"path": "/a"})
    assert outcome is GateOutcome.BLOCKED
    assert "NOT DONE" in message


def test_a_call_is_allowed_while_yolo_is_on():
    approval.grant(15)
    outcome, _ = check_allowed("files", "deleteFile", {"path": "/a"})
    assert outcome is GateOutcome.ALLOWED


def test_the_block_message_names_the_action_and_what_to_do():
    _, message = check_allowed("files", "deleteFile", {"path": "/a"})
    lowered = message.lower()
    assert "deletefile" in lowered
    assert "yolo" in lowered
    assert "cannot turn it on yourself" in lowered


def test_the_window_closing_blocks_again(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(approval.time, "time", lambda: clock["now"])

    approval.grant(15)
    assert check_allowed("files", "deleteFile", {})[0] is GateOutcome.ALLOWED

    clock["now"] += 16 * 60
    assert check_allowed("files", "deleteFile", {})[0] is GateOutcome.BLOCKED


# ---------------------------------------------------------------------------
# Enforcement in the tool execution path
# ---------------------------------------------------------------------------

def _install_fake_mcp(monkeypatch, annotations):
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
                name="srv__doThing", description="Does a thing",
                inputSchema={"type": "object"}, annotations=annotations,
            )
        },
    )
    monkeypatch.setattr(registry, "is_mcp_cache_initialized", lambda: True)
    return calls


def _run(cfg, args=None):
    from jarvis.tools.registry import run_tool_with_retries

    return run_tool_with_retries(
        db=None, cfg=cfg, tool_name="srv__doThing", tool_args=args or {},
        system_prompt="", original_prompt="", redacted_text="",
    )


def test_a_destructive_tool_does_not_reach_the_server_with_yolo_off(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    result = _run(_Cfg())

    assert calls == [], "the server was called with YOLO off"
    assert result.success is False
    assert "YOLO" in (result.error_message or "")


def test_a_destructive_tool_runs_with_yolo_on(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    approval.grant(15)

    result = _run(_Cfg())

    assert len(calls) == 1
    assert result.success is True


def test_a_read_only_tool_runs_either_way(monkeypatch):
    """YOLO is about consequences; reading is not one."""
    calls = _install_fake_mcp(monkeypatch, {"readOnlyHint": True})
    _run(_Cfg())
    assert len(calls) == 1


def test_the_policy_still_decides_what_counts_as_consequential(monkeypatch):
    """`mcp_confirm: off` means never gate, even with YOLO off."""
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    _run(_Cfg(mcp_confirm="off"))
    assert len(calls) == 1


def test_an_unannotated_tool_is_gated_under_the_strict_policy(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, None)
    _run(_Cfg(mcp_confirm="unannotated"))
    assert calls == []


def test_no_argument_can_stand_in_for_the_grant(monkeypatch):
    """There is no longer a per-call escape hatch to forge.

    The old gate took a `confirmation_code` argument, which meant the
    model had something to guess at. Now the only way through is a
    window the user opened, so a made-up argument does nothing.
    """
    calls = _install_fake_mcp(monkeypatch, {"destructiveHint": True})
    result = _run(_Cfg(), {"confirmation_code": "1234", "yolo": True, "approved": True})

    assert calls == []
    assert result.success is False
