"""Behavioural tests for YOLO mode.

YOLO is a time-boxed grant: for the next 15 or 30 minutes Jarvis acts on
consequential things without asking each time. It replaces per-action
confirmation codes, which were unusable in the packaged app and tedious
everywhere else.

One property carries over from the codes and is the reason this file
exists: **the model cannot turn it on**. Jarvis reads web pages, tool
descriptions and tool results, any of which can carry text written by
someone else. If "enable YOLO" were reachable from a tool call, that text
could enable it and then act freely. Granting is a human action in the
UI, and nothing in the tool layer can reach it.
"""

import time

import pytest

from jarvis import approval


@pytest.fixture(autouse=True)
def _clean():
    approval.revoke()
    yield
    approval.revoke()


# ---------------------------------------------------------------------------
# The window itself
# ---------------------------------------------------------------------------

def test_it_is_off_until_someone_turns_it_on():
    assert approval.is_active() is False


def test_granting_turns_it_on():
    approval.grant(15)
    assert approval.is_active() is True


def test_it_expires_on_its_own(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(approval.time, "time", lambda: clock["now"])

    approval.grant(15)
    clock["now"] += 15 * 60 - 1
    assert approval.is_active() is True

    clock["now"] += 2
    assert approval.is_active() is False


def test_revoking_turns_it_off_immediately():
    approval.grant(30)
    approval.revoke()
    assert approval.is_active() is False


def test_remaining_time_counts_down(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(approval.time, "time", lambda: clock["now"])

    approval.grant(30)
    assert approval.remaining_sec() == pytest.approx(30 * 60)
    clock["now"] += 600
    assert approval.remaining_sec() == pytest.approx(30 * 60 - 600)


def test_remaining_time_is_zero_when_off():
    assert approval.remaining_sec() == 0.0


def test_granting_again_extends_rather_than_stacking(monkeypatch):
    """A second grant sets a fresh window, it does not add to the old one."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(approval.time, "time", lambda: clock["now"])

    approval.grant(30)
    clock["now"] += 600
    approval.grant(15)
    assert approval.remaining_sec() == pytest.approx(15 * 60)


@pytest.mark.parametrize("minutes", [0, -5])
def test_a_non_positive_grant_leaves_it_off(minutes):
    approval.grant(minutes)
    assert approval.is_active() is False


def test_an_absurd_duration_is_capped():
    """A grant is meant to lapse; an all-day one is a standing permission."""
    approval.grant(60 * 24 * 7)
    assert approval.remaining_sec() <= approval.MAX_GRANT_MINUTES * 60


# ---------------------------------------------------------------------------
# The property that has to survive
# ---------------------------------------------------------------------------

def test_no_tool_can_turn_it_on():
    """The gate is worthless if the gated party can open it.

    Jarvis acts on text other people wrote — web pages, MCP tool
    descriptions, tool results. If any tool could grant, that text could
    grant, and then do as it liked for half an hour.
    """
    import inspect

    from jarvis.tools import registry

    offenders = []
    for name, tool in registry.BUILTIN_TOOLS.items():
        try:
            source = inspect.getsource(type(tool))
        except (OSError, TypeError):
            continue
        if "approval.grant" in source or "yolo_grant" in source:
            offenders.append(name)

    assert not offenders, f"these tools can enable YOLO themselves: {offenders}"


def test_the_tool_registry_module_never_grants():
    import inspect

    from jarvis.tools import registry

    source = inspect.getsource(registry)
    assert "approval.grant(" not in source, (
        "the tool execution path can enable YOLO, so a tool result could"
    )


def test_granting_needs_a_real_number_not_a_stray_truthy_value():
    """A config or message value must not be coerced into a grant."""
    for bad in ["30", None, True, "yes", object()]:
        approval.revoke()
        approval.grant(bad)  # must not raise
        assert approval.is_active() is False, f"{bad!r} was accepted as a duration"
