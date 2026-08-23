"""The process's one action log, and who it is acting as.

`registry.py` is the boundary every tool call crosses, and it should not
have to know how to open a database or resolve an identity to write a
line. This holds both, resolved once and reused, so recording an action
costs a function call rather than two connections.

Everything here is best-effort. Witnessing an action must never be able
to prevent it: authorisation fails closed, but the record does not,
because a full disk is not a reason to refuse work the user asked for.
A gap in the log is visible on its own.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from ..debug import debug_log
from .log import ActionLog, Decision, Outcome, Verification

_lock = threading.RLock()
_log: Optional[ActionLog] = None
_db_path: Optional[str] = None
_actor: Optional[dict[str, Optional[str]]] = None


def configure(db_path: str) -> None:
    """Point the recorder at a database. Cheap; opens nothing yet."""
    global _db_path
    with _lock:
        if db_path != _db_path:
            _close_locked()
            _db_path = db_path


def reset_for_tests() -> None:
    """Drop the cached log and identity."""
    global _db_path, _actor
    with _lock:
        _close_locked()
        _db_path = None
        _actor = None


def _close_locked() -> None:
    global _log
    if _log is not None:
        try:
            _log.close()
        except Exception:
            pass
        _log = None


def get_log() -> Optional[ActionLog]:
    """The shared log, opened on first use. ``None`` if it cannot open."""
    global _log
    with _lock:
        if _log is None and _db_path:
            try:
                _log = ActionLog(_db_path)
            except Exception as exc:
                debug_log(f"could not open the action log: {exc}", "audit")
                return None
        return _log


def _resolve_actor() -> dict[str, Optional[str]]:
    """Who this machine is acting for, resolved once.

    Slice 1's payoff: an entry that cannot say which machine acted is
    not an audit trail. Resolved lazily and cached, because every tool
    call reads it and none of them should pay for a query.
    """
    global _actor
    with _lock:
        if _actor is not None:
            return _actor
        resolved: dict[str, Optional[str]] = {
            "user_id": None, "workspace_id": None, "device_id": None,
        }
        if _db_path:
            try:
                from ..identity import IdentityStore

                store = IdentityStore(_db_path)
                try:
                    identity = store.ensure_local_identity()
                    resolved = {
                        "user_id": identity.user.id,
                        "workspace_id": identity.workspace.id,
                        "device_id": identity.device.id,
                    }
                finally:
                    store.close()
            except Exception as exc:
                debug_log(f"could not resolve the acting identity: {exc}", "audit")
        _actor = resolved
        return _actor


def record_attempt(
    *,
    tool_name: str,
    tool_source: str,
    arguments: Optional[dict[str, Any]] = None,
    secrets: Optional[dict[str, Any]] = None,
    decision: Decision = Decision.ALLOWED,
    decision_reason: str = "",
    policy_rule_id: str = "",
    mcp_server: Optional[str] = None,
) -> Optional[str]:
    """Record an attempt before it runs. Returns an id, or ``None``."""
    log = get_log()
    if log is None:
        return None
    try:
        return log.record_decision(
            tool_name=tool_name,
            tool_source=tool_source,
            arguments=arguments,
            secrets=secrets,
            decision=decision,
            decision_reason=decision_reason,
            policy_rule_id=policy_rule_id,
            mcp_server=mcp_server,
            **_resolve_actor(),
        )
    except Exception as exc:
        debug_log(f"could not record an attempt: {exc}", "audit")
        return None


def record_result(
    action_id: Optional[str],
    *,
    outcome: Outcome,
    detail: str = "",
    verification: Verification = Verification.NOT_CHECKED,
) -> None:
    """Record how an action ended. A no-op if the attempt was not recorded."""
    if action_id is None:
        return
    log = get_log()
    if log is None:
        return
    try:
        log.record_outcome(
            action_id, outcome=outcome, detail=detail, verification=verification,
        )
    except Exception as exc:
        debug_log(f"could not record an outcome: {exc}", "audit")


def record_human_event(name: str, *, detail: str = "") -> None:
    """Record something the person did, on the same log as the tools."""
    log = get_log()
    if log is None:
        return
    try:
        actor = _resolve_actor()
        log.record_human_event(
            name,
            detail=detail,
            user_id=actor["user_id"],
            device_id=actor["device_id"],
        )
    except Exception as exc:
        debug_log(f"could not record a human event: {exc}", "audit")
