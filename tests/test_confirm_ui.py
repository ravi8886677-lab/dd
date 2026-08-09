"""The presenter must add a channel without ever weakening the gate."""

import time

import pytest

from jarvis import confirm_ui


@pytest.fixture(autouse=True)
def _clean():
    confirm_ui.clear_presenter()
    confirm_ui.dismiss("")
    yield
    confirm_ui.clear_presenter()
    confirm_ui.dismiss("")


def test_a_registered_presenter_receives_the_code():
    seen = []
    confirm_ui.set_presenter(lambda code, desc, ttl: seen.append((code, desc, ttl)))

    confirm_ui.present_code("4821", "delete_file on fs", 120.0)

    assert seen == [("4821", "delete_file on fs", 120.0)]


def test_no_presenter_is_not_an_error():
    """CLI runs have no UI. The stderr line is the whole channel there."""
    confirm_ui.present_code("4821", "anything", 120.0)


def test_a_broken_presenter_cannot_break_the_gate():
    def explode(code, desc, ttl):
        raise RuntimeError("Qt is on fire")

    confirm_ui.set_presenter(explode)
    confirm_ui.present_code("4821", "anything", 120.0)  # must not raise


def test_screen_reads_are_blocked_only_while_a_code_is_live():
    assert not confirm_ui.is_showing()

    confirm_ui.present_code("4821", "anything", 60.0)
    assert confirm_ui.is_showing()

    confirm_ui.dismiss("spent")
    assert not confirm_ui.is_showing()


def test_a_gate_that_dies_mid_proposal_unblocks_itself():
    """Expiry is clock-based, so a missing dismiss cannot disable the
    screenshot tool permanently."""
    confirm_ui.present_code("4821", "anything", 0.05)
    assert confirm_ui.is_showing()

    time.sleep(0.1)
    assert not confirm_ui.is_showing()


def test_dismiss_reaches_the_ui():
    reasons = []
    confirm_ui.set_presenter(lambda *a: None, lambda reason: reasons.append(reason))

    confirm_ui.present_code("4821", "anything", 60.0)
    confirm_ui.dismiss("Wrong code — cancelled.")

    assert reasons == ["Wrong code — cancelled."]


# ---------------------------------------------------------------------------
# The reading side has to cover MCP too, not just the builtin screenshot
# ---------------------------------------------------------------------------

class _Cfg:
    def __init__(self, **kw):
        self.mcps = {"srv": {"transport": "stdio", "command": "/bin/true", "args": []}}
        self.voice_debug = False
        for key, value in kw.items():
            setattr(self, key, value)


def _install_mcp(monkeypatch, annotations):
    from jarvis.tools import registry

    calls = []

    class FakeClient:
        def __init__(self, cfgs):
            pass

        def invoke_tool(self, server_name, tool_name, arguments):
            calls.append((server_name, tool_name))
            return {"text": "screen text including 1234", "isError": False}

    monkeypatch.setattr(registry, "MCPClient", FakeClient)
    monkeypatch.setattr(
        registry,
        "get_cached_mcp_tools",
        lambda: {
            "srv__readScreen": registry.ToolSpec(
                name="srv__readScreen", description="Reads the screen",
                inputSchema={"type": "object"}, annotations=annotations,
            ),
            "srv__deleteAll": registry.ToolSpec(
                name="srv__deleteAll", description="Deletes everything",
                inputSchema={"type": "object"},
                annotations={"destructiveHint": True},
            ),
        },
    )
    monkeypatch.setattr(registry, "is_mcp_cache_initialized", lambda: True)
    return calls


def _run(cfg, name, args=None):
    from jarvis.tools.registry import run_tool_with_retries

    return run_tool_with_retries(
        db=None, cfg=cfg, tool_name=name, tool_args=args or {},
        system_prompt="", original_prompt="", redacted_text="",
    )


def test_a_read_only_mcp_tool_cannot_run_while_a_code_is_on_screen(monkeypatch):
    """The builtin screenshot is not the only thing that can read a display.

    `macos-automator` is a wizard-featured catalogue entry that runs
    arbitrary AppleScript, so it can capture the screen and return text.
    A read-only annotation means the gate never stops it, so the block
    has to sit on the reading side for MCP as well.
    """
    from jarvis.tools.external import mcp_gate

    calls = _install_mcp(monkeypatch, {"readOnlyHint": True})
    monkeypatch.setattr(mcp_gate, "_announce", lambda m: None)
    mcp_gate.reset_pending()

    # A destructive call opens a proposal and puts a code on screen.
    _run(_Cfg(), "srv__deleteAll", {})
    assert calls == []

    result = _run(_Cfg(), "srv__readScreen", {})

    assert calls == [], "an MCP tool read the screen while the code was displayed"
    assert result.success is False
    mcp_gate.reset_pending()


def test_the_approval_call_itself_still_gets_through(monkeypatch):
    """Blocking everything would make the gate impossible to open."""
    import re

    from jarvis.tools.external import mcp_gate

    calls = _install_mcp(monkeypatch, {"readOnlyHint": True})
    shown = []
    monkeypatch.setattr(mcp_gate, "_announce", lambda m: shown.append(m))
    mcp_gate.reset_pending()

    _run(_Cfg(), "srv__deleteAll", {})
    code = re.search(r"\b(\d{4})\b", "\n".join(shown)).group(1)

    result = _run(_Cfg(), "srv__deleteAll", {"confirmation_code": code})

    assert calls == [("srv", "deleteAll")]
    assert result.success is True
    mcp_gate.reset_pending()


def test_mcp_tools_run_normally_once_no_code_is_showing(monkeypatch):
    from jarvis.tools.external import mcp_gate

    calls = _install_mcp(monkeypatch, {"readOnlyHint": True})
    mcp_gate.reset_pending()

    result = _run(_Cfg(), "srv__readScreen", {})

    assert calls == [("srv", "readScreen")]
    assert result.success is True


def test_a_computer_use_proposal_also_blocks_mcp_screen_reading(monkeypatch):
    """A computerUse code on screen is just as readable by an MCP tool."""
    from jarvis import confirm_ui
    from jarvis.tools.external import mcp_gate

    calls = _install_mcp(monkeypatch, {"readOnlyHint": True})
    mcp_gate.reset_pending()
    confirm_ui.present_code("4321", "click Delete", 180.0)
    try:
        result = _run(_Cfg(), "srv__readScreen", {})
        assert calls == [], "an MCP tool ran while a computerUse code was on screen"
        assert result.success is False
    finally:
        confirm_ui.dismiss("")
