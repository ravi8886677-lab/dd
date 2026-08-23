"""The action log: what was asked, what was decided, what happened.

Two of the project's non-negotiables are the two it does not currently
meet. Nothing verifies that an action landed, so a completion claim is
only as good as the model's optimism; and nothing records what was done,
so "every external action attributable" has nothing to attribute to.
Both need one record underneath them.

## Two entries per action, appended, never rewritten

The decision entry is written **before** anything executes. An action
that took the process down with it still left evidence that it was
attempted: an action with no outcome entry is a call that never came
back, which is more informative than no row at all.

The outcome entry is written after, and it is the only thing entitled to
say an action succeeded. A function returning is not the same as the
world having changed, which is the whole distinction between logging and
verification.

Entries are appended. Nothing updates an entry once written, because a
record that can be edited after the fact is not evidence of anything.

## Secrets

Arguments are redacted **here**, on the way in, rather than by asking
every caller to remember. A secret is recorded by name and character
count and never by value: that is enough to answer "was a credential
used here?" without the log becoming the thing worth stealing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional

from ..debug import debug_log
from ..utils.redact import redact

#: Arguments are capped so one enormous call cannot flood the log. Well
#: above anything readable, far below a file body.
MAX_ARGUMENTS_CHARS = 4096

#: Same reasoning for the failure text a tool hands back.
MAX_DETAIL_CHARS = 2048

BUSY_TIMEOUT_SEC = 15.0


class Decision(str, Enum):
    """What the boundary decided about an attempt."""

    ALLOWED = "allowed"
    DENIED = "denied"
    CONFIRMED = "confirmed"


class Outcome(str, Enum):
    """What came back."""

    OK = "ok"
    ERROR = "error"


class Verification(str, Enum):
    """Whether anyone checked that the world actually changed."""

    CONFIRMED = "confirmed"
    NOT_CHECKED = "not_checked"
    FAILED = "failed"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS actions (
    id                 TEXT PRIMARY KEY,
    action_id          TEXT NOT NULL,
    entry              TEXT NOT NULL,
    ts_utc             TEXT NOT NULL,
    user_id            TEXT,
    workspace_id       TEXT,
    device_id          TEXT,
    tool_name          TEXT NOT NULL,
    tool_source        TEXT NOT NULL,
    mcp_server         TEXT,
    arguments_redacted TEXT NOT NULL DEFAULT '',
    decision           TEXT,
    decision_reason    TEXT NOT NULL DEFAULT '',
    policy_rule_id     TEXT NOT NULL DEFAULT '',
    outcome            TEXT,
    outcome_detail     TEXT NOT NULL DEFAULT '',
    verification       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_actions_action ON actions(action_id);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts_utc DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class ActionEntry:
    """One appended entry: a decision or an outcome."""

    id: str
    action_id: str
    entry: str
    ts_utc: str
    tool_name: str
    tool_source: str
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    device_id: Optional[str] = None
    mcp_server: Optional[str] = None
    arguments_redacted: str = ""
    decision: Optional[Decision] = None
    decision_reason: str = ""
    policy_rule_id: str = ""
    outcome: Optional[Outcome] = None
    outcome_detail: str = ""
    verification: str = ""


@dataclass(frozen=True)
class ActionRecord:
    """An action as a reader wants it: its decision, and how it ended.

    ``outcome`` is ``None`` for an action that never came back.
    """

    action_id: str
    ts_utc: str
    tool_name: str
    tool_source: str
    mcp_server: Optional[str]
    arguments_redacted: str
    decision: Optional[Decision]
    decision_reason: str
    policy_rule_id: str
    outcome: Optional[Outcome]
    outcome_detail: str
    verification: Optional[Verification]
    completed_at: Optional[str]
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    device_id: Optional[str] = None


def _describe_secrets(secrets: Optional[dict[str, Any]]) -> str:
    """Name each secret and its length. Never its value."""
    if not secrets:
        return ""
    parts = [f"{name}=<{len(str(value))} chars>" for name, value in secrets.items()]
    return " ".join(parts)



def summarise_arguments(
    arguments: Optional[dict[str, Any]],
    secrets: Optional[dict[str, Any]] = None,
) -> str:
    """Render arguments for the log: scrubbed, capped, secrets by length.

    Public because the shape of what gets stored is a property worth
    testing on its own, and because a caller occasionally wants to know
    what a row will look like before writing it.
    """
    rendered = ""
    if arguments:
        safe = {
            key: value for key, value in arguments.items()
            if not (secrets and key in secrets)
        }
        if safe:
            try:
                rendered = json.dumps(safe, default=str, sort_keys=True)
            except Exception:
                rendered = str(safe)
    scrubbed = redact(rendered, max_len=MAX_ARGUMENTS_CHARS)

    # Appended after the scrub, not through it. The description holds a
    # name and a character count and no value, so it is safe by
    # construction, and the scrub would otherwise read `api_key=<17` as
    # a credential and redact away the very count that makes it useful.
    described = _describe_secrets(secrets)
    return f"{scrubbed} {described}".strip() if described else scrubbed



def _set_journal_mode(conn: sqlite3.Connection) -> None:
    """Ask for WAL, and carry on if another process is mid-open.

    Converting a database to WAL takes a brief exclusive lock, and
    SQLite does **not** run the busy handler for that operation: the
    timeout on the connection does not cover it. Two processes opening a
    fresh install at the same moment is the ordinary case here - the
    daemon establishes identity while the tray spawns the dashboard - so
    one of them can see `database is locked` before anything has gone
    wrong.

    Failing to set it is not a reason to fail the open. Journal mode is a
    property of the database rather than the connection, so whoever wins
    sets it for everyone; and a database in the default mode is slower,
    not broken.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError as exc:
        debug_log(f"left the journal mode as it was: {exc}", "storage")


class ActionLog:
    """The ``actions`` table, in the same SQLite file as everything else.

    Owns its schema and applies it on construction, the same contract as
    ``GraphMemoryStore`` and ``IdentityStore``: the daemon and the
    dashboard both open it and either may be first.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_SEC,
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            # Without WAL every append fsyncs, and the boundary appends
            # twice per tool call on the request path.
            _set_journal_mode(self.conn)
            self.conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def _writing(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # ── Writing ──────────────────────────────────────────────────────

    def record_decision(
        self,
        *,
        tool_name: str,
        tool_source: str,
        arguments: Optional[dict[str, Any]] = None,
        secrets: Optional[dict[str, Any]] = None,
        decision: Decision = Decision.ALLOWED,
        decision_reason: str = "",
        policy_rule_id: str = "",
        mcp_server: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> str:
        """Record an attempt and what was decided about it, before it runs.

        Returns the action id to hand back to ``record_outcome``.
        """
        action_id = _new_id()
        self._append(
            action_id=action_id,
            entry="decision",
            tool_name=tool_name,
            tool_source=tool_source,
            mcp_server=mcp_server,
            arguments_redacted=summarise_arguments(arguments, secrets),
            decision=decision.value,
            decision_reason=redact(decision_reason, max_len=MAX_DETAIL_CHARS),
            policy_rule_id=policy_rule_id,
            user_id=user_id,
            workspace_id=workspace_id,
            device_id=device_id,
        )
        return action_id

    def record_outcome(
        self,
        action_id: str,
        *,
        outcome: Outcome,
        detail: str = "",
        verification: Verification = Verification.NOT_CHECKED,
    ) -> None:
        """Record how an action ended.

        ``verification`` defaults to ``not_checked`` rather than to
        success: a tool with nothing to check must not read as confirmed.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT tool_name, tool_source, mcp_server, user_id, workspace_id,"
                " device_id FROM actions WHERE action_id = ? AND entry = 'decision'",
                (action_id,),
            ).fetchone()
        if row is None:
            debug_log(f"outcome for an unknown action {action_id}", "audit")
            return

        self._append(
            action_id=action_id,
            entry="outcome",
            tool_name=row["tool_name"],
            tool_source=row["tool_source"],
            mcp_server=row["mcp_server"],
            outcome=outcome.value,
            outcome_detail=redact(detail, max_len=MAX_DETAIL_CHARS),
            verification=verification.value,
            user_id=row["user_id"],
            workspace_id=row["workspace_id"],
            device_id=row["device_id"],
        )

    def record_human_event(
        self,
        name: str,
        *,
        detail: str = "",
        user_id: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> str:
        """Record something the person did, on the same log as the tools.

        Granting YOLO is the most consequential thing a user does, and
        until now it existed only in memory. A log that shows an action
        running without showing that the window was open cannot explain
        itself.
        """
        action_id = _new_id()
        self._append(
            action_id=action_id,
            entry="decision",
            tool_name=name,
            tool_source="human",
            decision=Decision.CONFIRMED.value,
            decision_reason=redact(detail, max_len=MAX_DETAIL_CHARS),
            user_id=user_id,
            device_id=device_id,
        )
        return action_id

    def _append(self, **fields: Any) -> None:
        fields.setdefault("ts_utc", _now())
        fields["id"] = _new_id()
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        try:
            with self._writing():
                self.conn.execute(
                    f"INSERT INTO actions({columns}) VALUES ({placeholders})",
                    tuple(fields.values()),
                )
        except Exception as exc:
            # The log must not be the thing that breaks a tool call. A
            # missing entry is visible as a gap; a raised exception here
            # would take down the action it was only supposed to witness.
            debug_log(f"could not write an audit entry: {exc}", "audit")

    # ── Reading ──────────────────────────────────────────────────────

    def get_entries(self, limit: int = 500) -> list[ActionEntry]:
        """Every appended entry, oldest first. The raw record."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM actions ORDER BY ts_utc, rowid LIMIT ?", (limit,),
            ).fetchall()
        return [self._to_entry(row) for row in rows]

    def get_actions(self, limit: int = 100) -> list[ActionRecord]:
        """Actions newest first, each folded from its entries."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM actions ORDER BY ts_utc, rowid",
            ).fetchall()

        decisions: dict[str, sqlite3.Row] = {}
        outcomes: dict[str, sqlite3.Row] = {}
        order: list[str] = []
        for row in rows:
            if row["entry"] == "decision":
                decisions[row["action_id"]] = row
                order.append(row["action_id"])
            else:
                outcomes[row["action_id"]] = row

        records = [
            self._fold(decisions[action_id], outcomes.get(action_id))
            for action_id in reversed(order)
        ]
        return records[:limit]

    @staticmethod
    def _to_entry(row: sqlite3.Row) -> ActionEntry:
        return ActionEntry(
            id=row["id"],
            action_id=row["action_id"],
            entry=row["entry"],
            ts_utc=row["ts_utc"],
            tool_name=row["tool_name"],
            tool_source=row["tool_source"],
            user_id=row["user_id"],
            workspace_id=row["workspace_id"],
            device_id=row["device_id"],
            mcp_server=row["mcp_server"],
            arguments_redacted=row["arguments_redacted"],
            decision=Decision(row["decision"]) if row["decision"] else None,
            decision_reason=row["decision_reason"],
            policy_rule_id=row["policy_rule_id"],
            outcome=Outcome(row["outcome"]) if row["outcome"] else None,
            outcome_detail=row["outcome_detail"],
            verification=row["verification"],
        )

    @staticmethod
    def _fold(decision: sqlite3.Row, outcome: Optional[sqlite3.Row]) -> ActionRecord:
        return ActionRecord(
            action_id=decision["action_id"],
            ts_utc=decision["ts_utc"],
            tool_name=decision["tool_name"],
            tool_source=decision["tool_source"],
            mcp_server=decision["mcp_server"],
            arguments_redacted=decision["arguments_redacted"],
            decision=Decision(decision["decision"]) if decision["decision"] else None,
            decision_reason=decision["decision_reason"],
            policy_rule_id=decision["policy_rule_id"],
            user_id=decision["user_id"],
            workspace_id=decision["workspace_id"],
            device_id=decision["device_id"],
            outcome=Outcome(outcome["outcome"]) if outcome and outcome["outcome"] else None,
            outcome_detail=outcome["outcome_detail"] if outcome else "",
            verification=(
                Verification(outcome["verification"])
                if outcome and outcome["verification"]
                else None
            ),
            completed_at=outcome["ts_utc"] if outcome else None,
        )
