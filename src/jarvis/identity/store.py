"""Identity rows: the user, their workspaces, their devices, their accounts.

There is one person on one machine, and these rows still have to exist.
A permission that applies to one tool on one device, or to one connected
account, has nothing to attach to without them, and an action log that
cannot say which machine did something is not an audit trail. Adding
them after the things that depend on them means rebuilding those things.

Only four entities live here, the four that the slices above this one
consume. The rest of the data model arrives with the features that need
it; empty tables rot.

The store owns its schema and applies it on construction, the same
contract as ``GraphMemoryStore``: opening it is enough, whichever
process gets there first.
"""

from __future__ import annotations

import platform
import socket
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ..debug import debug_log
from ..utils.paths import data_dir, ensure_data_dir

#: How long to wait for another process to finish writing before giving
#: up. The daemon and the dashboard both establish identity at start-up,
#: and the loser should wait rather than fail.
BUSY_TIMEOUT_SEC = 15.0

#: Names the machine rather than the database file. See ``local_device_id``.
DEVICE_ID_FILENAME = "device_id"

#: The workspace every install starts with. Kinds are open-ended so a
#: second one can be added without a migration.
PERSONAL_WORKSPACE_KIND = "personal"

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
-- Identity is established at startup, before the action log opens, so
-- this is usually the connection that creates the file. Journal mode is
-- a property of the database rather than the connection, so whichever
-- store gets there first decides it for everyone; leaving it unset here
-- meant the file's mode depended on start order.
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, kind, name)
);

CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    platform     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connected_accounts (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id  TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    provider      TEXT NOT NULL,
    account_label TEXT NOT NULL DEFAULT '',
    secret_ref    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(user_id, provider, account_label)
);

CREATE INDEX IF NOT EXISTS idx_workspaces_user ON workspaces(user_id);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON connected_accounts(user_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class User:
    id: str
    display_name: str
    created_at: str


@dataclass(frozen=True)
class Workspace:
    id: str
    user_id: str
    name: str
    kind: str
    created_at: str


@dataclass(frozen=True)
class Device:
    id: str
    user_id: str
    name: str
    platform: str
    created_at: str
    last_seen_at: str


@dataclass(frozen=True)
class ConnectedAccount:
    id: str
    user_id: str
    workspace_id: Optional[str]
    provider: str
    account_label: str
    secret_ref: str
    created_at: str


@dataclass(frozen=True)
class LocalIdentity:
    """Who Jarvis is acting for on this machine, right now."""

    user: User
    workspace: Workspace
    device: Device


def local_device_id() -> str:
    """A stable identifier for this machine.

    Kept in a file in the data directory rather than derived from the
    hostname or a MAC address: a hostname changes, and hardware
    identifiers are the kind of thing this project does not collect. It
    lives beside the database rather than inside it so that rebuilding
    or moving the database does not make Jarvis believe it woke up on a
    new machine and quietly lose that machine's permissions.
    """
    path = data_dir() / DEVICE_ID_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    generated = _new_id()
    try:
        ensure_data_dir()
        path.write_text(generated, encoding="utf-8")
        debug_log(f"registered this device as {generated}", "identity")
    except OSError as exc:
        # A read-only home should not stop Jarvis running; the device is
        # simply not remembered between launches.
        debug_log(f"could not persist the device id: {exc}", "identity")
    return generated


def _device_name() -> str:
    try:
        return socket.gethostname() or "this machine"
    except OSError:
        return "this machine"


class IdentityStore:
    """The identity rows, in the same SQLite file as everything else."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        # ``isolation_level=None`` hands transaction control to this class
        # so ``_writing`` can open the write transaction it actually needs.
        # The timeout is how long to wait for the other process rather
        # than failing at it.
        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_SEC,
        )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(_SCHEMA_SQL)

    @contextmanager
    def _writing(self) -> Iterator[None]:
        """Hold the database's write lock across a read-then-write.

        Deciding whether a row exists and then inserting it is only safe
        if nothing can insert between the two, and the daemon and the
        dashboard are separate processes: a lock in this one cannot see
        the other. ``BEGIN IMMEDIATE`` takes SQLite's write lock up
        front, so the second process blocks before its read and then
        sees what the first one wrote rather than deciding the row is
        still missing.

        The in-process lock is kept as well: it serialises this process's
        own threads without a round trip to the file.
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── Reading ──────────────────────────────────────────────────────

    def get_users(self) -> list[User]:
        with self._lock:
            return self._users()

    def _users(self) -> list[User]:
        """Unlocked read, for callers already inside a transaction."""
        rows = self.conn.execute(
            "SELECT * FROM users ORDER BY created_at",
        ).fetchall()
        return [User(**dict(row)) for row in rows]

    def get_workspaces(self) -> list[Workspace]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM workspaces ORDER BY created_at",
            ).fetchall()
        return [Workspace(**dict(row)) for row in rows]

    def get_devices(self, user_id: Optional[str] = None) -> list[Device]:
        """This user's machines, or every machine when unscoped.

        Callers showing someone their own devices must pass ``user_id``.
        There is one user today and the schema is built for a second, so
        an unscoped read is a promise that expires.
        """
        with self._lock:
            if user_id is None:
                rows = self.conn.execute(
                    "SELECT * FROM devices ORDER BY created_at",
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM devices WHERE user_id = ? ORDER BY created_at",
                    (user_id,),
                ).fetchall()
        return [Device(**dict(row)) for row in rows]

    def get_accounts(self, user_id: Optional[str] = None) -> list[ConnectedAccount]:
        """This user's connected accounts, or every one when unscoped."""
        with self._lock:
            if user_id is None:
                rows = self.conn.execute(
                    "SELECT * FROM connected_accounts ORDER BY created_at",
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM connected_accounts WHERE user_id = ?"
                    " ORDER BY created_at",
                    (user_id,),
                ).fetchall()
        return [ConnectedAccount(**dict(row)) for row in rows]

    def raw_account_row(self, account_id: str) -> dict[str, Any]:
        """The stored row as it sits on disk, for tests that assert on it."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM connected_accounts WHERE id = ?", (account_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    # ── The local identity ───────────────────────────────────────────

    def ensure_local_identity(self, display_name: str = "") -> LocalIdentity:
        """Return this install's user, workspace and device, creating any
        that are not there yet.

        Safe to call on every launch: it adds nothing the second time,
        and marks the device as seen again.
        """
        with self._writing():
            user = self._ensure_user(display_name)
            workspace = self._ensure_personal_workspace(user)
            device = self._ensure_this_device(user)
        return LocalIdentity(user=user, workspace=workspace, device=device)

    def _ensure_user(self, display_name: str) -> User:
        existing = self._users()
        if existing:
            return existing[0]

        user = User(id=_new_id(), display_name=display_name, created_at=_now())
        self.conn.execute(
            "INSERT INTO users(id, display_name, created_at) VALUES (?, ?, ?)",
            (user.id, user.display_name, user.created_at),
        )
        debug_log("created the local user row", "identity")
        return user

    def _ensure_personal_workspace(self, user: User) -> Workspace:
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE user_id = ? AND kind = ? ORDER BY created_at",
            (user.id, PERSONAL_WORKSPACE_KIND),
        ).fetchone()
        if row is not None:
            return Workspace(**dict(row))

        workspace = Workspace(
            id=_new_id(),
            user_id=user.id,
            name="Personal",
            kind=PERSONAL_WORKSPACE_KIND,
            created_at=_now(),
        )
        self.conn.execute(
            "INSERT INTO workspaces(id, user_id, name, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                workspace.id,
                workspace.user_id,
                workspace.name,
                workspace.kind,
                workspace.created_at,
            ),
        )
        debug_log("created the personal workspace", "identity")
        return workspace

    def _ensure_this_device(self, user: User) -> Device:
        # Imported through the package so a test can point it at another
        # machine without reaching into this module's globals.
        from . import local_device_id as resolve_device_id

        device_id = resolve_device_id()
        seen_at = _now()

        row = self.conn.execute(
            "SELECT * FROM devices WHERE id = ?", (device_id,),
        ).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (seen_at, device_id),
            )
            device = Device(**dict(row))
            return Device(
                id=device.id,
                user_id=device.user_id,
                name=device.name,
                platform=device.platform,
                created_at=device.created_at,
                last_seen_at=seen_at,
            )

        device = Device(
            id=device_id,
            user_id=user.id,
            name=_device_name(),
            platform=platform.system() or "unknown",
            created_at=seen_at,
            last_seen_at=seen_at,
        )
        self.conn.execute(
            "INSERT INTO devices(id, user_id, name, platform, created_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                device.id,
                device.user_id,
                device.name,
                device.platform,
                device.created_at,
                device.last_seen_at,
            ),
        )
        debug_log(f"registered device {device.name} ({device.platform})", "identity")
        return device

    # ── Connected accounts ───────────────────────────────────────────

    def link_account(
        self,
        provider: str,
        account_label: str = "",
        workspace_id: Optional[str] = None,
    ) -> ConnectedAccount:
        """Record that the user has connected an account with a provider.

        The row holds a *reference* to the credential, never the
        credential: ``secret_ref`` is a name to look up in the OS
        keychain through ``utils.secret_store``. A password in a SQLite
        row is a password in every backup of that row.
        """
        with self._writing():
            users = self._users()
            if not users:
                raise ValueError("no local user yet; call ensure_local_identity first")
            user = users[0]

            row = self.conn.execute(
                "SELECT * FROM connected_accounts"
                " WHERE user_id = ? AND provider = ? AND account_label = ?",
                (user.id, provider, account_label),
            ).fetchone()
            if row is not None:
                return ConnectedAccount(**dict(row))

            account = ConnectedAccount(
                id=_new_id(),
                user_id=user.id,
                workspace_id=workspace_id,
                provider=provider,
                account_label=account_label,
                secret_ref="",
                created_at=_now(),
            )
            account = ConnectedAccount(
                **{**account.__dict__, "secret_ref": f"account:{provider}:{account.id}"},
            )
            self.conn.execute(
                "INSERT INTO connected_accounts"
                "(id, user_id, workspace_id, provider, account_label, secret_ref, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    account.id,
                    account.user_id,
                    account.workspace_id,
                    account.provider,
                    account.account_label,
                    account.secret_ref,
                    account.created_at,
                ),
            )
            debug_log(f"linked a {provider} account", "identity")
            return account

    def unlink_account(self, account_id: str) -> bool:
        """Forget a connected account. The credential itself is the
        keychain's to remove, through ``utils.secret_store``."""
        with self._writing():
            cur = self.conn.execute(
                "DELETE FROM connected_accounts WHERE id = ?", (account_id,),
            )
        return cur.rowcount > 0
