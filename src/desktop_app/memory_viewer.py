"""
🧠 Jarvis Memory Viewer

A beautiful web interface for exploring Jarvis's conversation memories.
Run directly: python -m desktop_app.memory_viewer
"""

from __future__ import annotations

import json
import sqlite3
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request, Response

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.identity import IdentityStore
from jarvis.memory.db import open_database
from jarvis.memory.graph import FIXED_BRANCH_IDS, GraphMemoryStore
from jarvis.utils.paths import data_dir


def _dashboard_root() -> Path:
    """Locate dashboard data in a checkout or a PyInstaller bundle."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "desktop_app" / "dashboard"
    return Path(__file__).resolve().parent / "dashboard"


_DASHBOARD_ROOT = _dashboard_root()
app = Flask(
    __name__,
    static_folder=str(_DASHBOARD_ROOT / "static"),
    template_folder=str(_DASHBOARD_ROOT / "templates"),
)

# ── Access control ────────────────────────────────────────────────────
#
# The dashboard serves the user's diary, personal facts and meal log, and
# can chat as them. `/api/mcp` additionally writes a command that Jarvis
# later spawns, so an unauthenticated write here is a code-execution
# path. Binding to 127.0.0.1 is not sufficient on its own:
#
#   - any process or user on the machine can reach a loopback port;
#   - a page in the user's browser can be pointed at it, and DNS
#     rebinding defeats the assumption that loopback means local.
#
# So: a token minted per server start, and a Host allow-list. The token
# is handed to the browser once via the launch URL and kept in a cookie;
# nothing needs to be stored on disk. Rate limits sit behind both, so a
# token cannot be guessed at speed and the endpoints that register a
# spawnable command cannot be driven in a tight loop.
# The desktop app launches this as a subprocess and needs the same
# token to build the window's URL, so it may mint one and pass it in.
_SESSION_TOKEN = os.environ.get("JARVIS_DASHBOARD_TOKEN") or secrets.token_urlsafe(32)
_TOKEN_COOKIE = "jarvis_dashboard_token"

# Host headers that may address this server. Anything else means the
# request arrived via a name that resolves here — the rebinding case.
_ALLOWED_HOST_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}

# ── Rate limits ──────────────────────────────────────────────────────────
# The dashboard serves one person on their own machine, so these ceilings
# sit far above anything a human clicking around will reach. They exist to
# blunt automation: guessing the session token, and hammering the endpoints
# that write a command Jarvis later spawns.
RATE_LIMIT_WINDOW_SEC = 60.0
# Wrong tokens tolerated per window before refusing to answer at all. Must
# stay clear of the page's own traffic: it polls twice every five seconds, so
# a tab left open across a restart sends 24 failures a minute by itself. The
# token is 32 random bytes, so this ceiling is belt-and-braces rather than the
# thing making a guess hopeless.
MAX_AUTH_FAILURES = 60
# Writes to the MCP config endpoints per window. Adding every entry in the
# catalogue by hand is well inside this.
MAX_WRITES_PER_WINDOW = 30

_rate_lock = threading.Lock()
# Event timestamps per bucket, oldest first. Trimmed to the window on every
# read, so this cannot grow without bound.
_rate_events: dict[str, list[float]] = {}

# Named seam for the clock. Patching ``time.monotonic`` through the module
# attribute would swap it for the whole process; see utils/backoff.py.
_now = time.monotonic


def _trim(bucket: str, now: float) -> list[float]:
    """Drop events that have aged out. Caller holds the lock."""
    events = [t for t in _rate_events.get(bucket, []) if now - t < RATE_LIMIT_WINDOW_SEC]
    _rate_events[bucket] = events
    return events


def _rate_limited(bucket: str, limit: int) -> bool:
    """Whether ``bucket`` is over ``limit``, without recording anything.

    Read-only so a caller holding the correct token is not charged for
    someone else's failures, and so a lockout ages out on its own rather
    than being extended by every request that arrives during it.
    """
    with _rate_lock:
        return len(_trim(bucket, _now())) >= limit


def _record_event(bucket: str) -> None:
    """Count one event against ``bucket``."""
    now = _now()
    with _rate_lock:
        _trim(bucket, now).append(now)


def _reset_rate_limits() -> None:
    """Clear every bucket. For tests and for a fresh launch."""
    with _rate_lock:
        _rate_events.clear()


# Values that count as switching the token off. Anything else — including
# "0", "false" and "no" — leaves it on. A bare truthiness test on the
# environment variable makes ``JARVIS_DASHBOARD_NO_AUTH=0`` *disable*
# authentication, which is the opposite of what anyone typing it means.
_TRUTHY = {"1", "true", "yes", "on"}


def _no_auth_enabled() -> bool:
    """Whether the operator has explicitly asked to skip the token.

    For a local demo where pasting the launch URL is a nuisance. It skips
    the token only: see ``_host_is_allowed``.
    """
    return os.environ.get("JARVIS_DASHBOARD_NO_AUTH", "").strip().lower() in _TRUTHY


def _token_matches(supplied: str) -> bool:
    """Constant-time compare that tolerates any input.

    ``secrets.compare_digest`` raises TypeError on non-ASCII strings, so
    a token containing one byte of Unicode turned an auth failure into a
    500 with a traceback. Reject those as simply wrong.
    """
    if _no_auth_enabled():
        return True
    try:
        return secrets.compare_digest(supplied or "", _SESSION_TOKEN)
    except TypeError:
        return False


def _host_is_allowed(host_header: str) -> bool:
    """Whether this request addressed the server by a name that is ours.

    Deliberately not affected by ``JARVIS_DASHBOARD_NO_AUTH``. The token
    answers "is this the user?"; this answers "did a webpage aim the
    user's browser at us?", and only the first is a nuisance worth
    switching off for a demo. Without this check any site the user
    visits can point a hostname at 127.0.0.1 and drive the dashboard
    from their own browser — reading the diary and POSTing to
    ``/api/mcp``, which registers a command Jarvis later spawns.
    """
    if not host_header:
        return False
    name = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else \
        host_header.split("]")[0] + "]"
    # Host names are case-insensitive, so "LOCALHOST:5050" is the same host.
    return name.lower() in _ALLOWED_HOST_NAMES


@app.before_request
def _guard_request():
    if not _host_is_allowed(request.headers.get("Host", "")):
        return jsonify({"error": "unrecognised Host header"}), 403

    supplied = (
        request.headers.get("X-Dashboard-Token")
        or request.cookies.get(_TOKEN_COOKIE)
        or request.args.get("token", "")
    )

    if not _token_matches(supplied):
        # Checked before recording, so a request already being refused does
        # not re-arm the window that refused it. Recording first would let a
        # flood hold itself locked out for as long as it kept going, and grow
        # the bucket without limit inside the lock every other request needs.
        if _rate_limited("auth", MAX_AUTH_FAILURES):
            return jsonify({"error": "too many failed attempts — try again shortly"}), 429
        # Failures against "/" count too. That path is exempt from the refusal
        # below so it can render its own guidance, but it still compares the
        # token and answers 200 or 401, so leaving it uncounted would make it
        # an oracle that answers guesses forever.
        _record_event("auth")
        debug_log("dashboard rejected a bad token", "memory_viewer")
        if request.path == "/":
            # The landing page accepts the token as a query parameter and
            # stores it, so the user only ever pastes the launch URL. A bare
            # 401 body here would leave them with no way forward.
            return None
        return jsonify({"error": "unauthorised — reopen the dashboard from its launch URL"}), 401

    # A request carrying the real token is never charged for someone else's
    # failures. The bucket is dashboard-wide, so charging it would let a
    # forgotten tab with a dead cookie — the page polls itself twice every
    # five seconds — lock the owner out of their own diary.

    # /api/mcp and /api/mcp/catalogue/<name> both register a command Jarvis
    # will spawn, so they share one budget — limiting them separately would
    # just mean using the other one.
    if request.method in ("POST", "DELETE") and request.path.startswith("/api/mcp"):
        if _rate_limited("mcp_write", MAX_WRITES_PER_WINDOW):
            debug_log("dashboard MCP writes over the limit; refusing", "memory_viewer")
            return jsonify({"error": "too many changes at once — try again shortly"}), 429
        _record_event("mcp_write")
    return None

# Global database connection
_db_conn: Optional[sqlite3.Connection] = None
_graph_store: Optional[GraphMemoryStore] = None
_identity_store: Optional[IdentityStore] = None


def _get_db_path() -> str:
    """Get the database path from settings."""
    try:
        settings = load_settings()
        return settings.db_path
    except Exception:
        # Fallback to default path
        return str(data_dir() / "jarvis.db")


def get_db() -> sqlite3.Connection:
    """Get or create the database connection.

    The dashboard can be opened before Jarvis has ever run: the tray menu
    offers it on a fresh install and people click it. So it applies the
    schema itself rather than assuming the daemon has already started,
    which is what ``GraphMemoryStore`` has always done for the knowledge
    graph. ``open_database`` is the daemon's own definition, so the two
    cannot drift apart.
    """
    global _db_conn
    if _db_conn is None:
        db_path = _get_db_path()
        debug_log(f"Opening dashboard database at {db_path}", "dashboard")
        _db_conn = open_database(db_path)
    return _db_conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert sqlite3.Row to dictionary."""
    return {key: row[key] for key in row.keys()}


# ─────────────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/memories")
def get_memories() -> Response:
    """
    Get all conversation summaries with optional filtering.

    Query params:
    - search: Search query for full-text search
    - topic: Filter by topic (comma-separated for multiple)
    - from_date: Start date (YYYY-MM-DD)
    - to_date: End date (YYYY-MM-DD)
    - limit: Max results (default 100)
    """
    conn = get_db()
    cur = conn.cursor()

    search = request.args.get("search", "").strip()
    topic_filter = request.args.get("topic", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)

    params: list[Any] = []
    conditions: list[str] = []

    # Build query based on filters
    if search:
        # Use FTS for search
        conditions.append("cs.id IN (SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH ?)")
        params.append(search)

    if topic_filter:
        # Filter by topic(s)
        topics = [t.strip().lower() for t in topic_filter.split(",") if t.strip()]
        if topics:
            topic_conditions = " OR ".join(["LOWER(cs.topics) LIKE ?" for _ in topics])
            conditions.append(f"({topic_conditions})")
            params.extend([f"%{t}%" for t in topics])

    if from_date:
        conditions.append("cs.date_utc >= ?")
        params.append(from_date)

    if to_date:
        conditions.append("cs.date_utc <= ?")
        params.append(to_date)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT cs.id, cs.date_utc, cs.ts_utc, cs.summary, cs.topics, cs.source_app
        FROM conversation_summaries cs
        WHERE {where_clause}
        ORDER BY cs.date_utc DESC
        LIMIT ?
    """
    params.append(limit)

    try:
        rows = cur.execute(query, params).fetchall()
        memories = [row_to_dict(row) for row in rows]

        # Parse topics into arrays
        for memory in memories:
            if memory.get("topics"):
                memory["topics_list"] = [t.strip() for t in memory["topics"].split(",") if t.strip()]
            else:
                memory["topics_list"] = []

        return jsonify({"memories": memories, "count": len(memories)})
    except Exception as e:
        return jsonify({"error": str(e), "memories": [], "count": 0}), 500


@app.route("/api/topics")
def get_topics() -> Response:
    """Get all unique topics with their counts."""
    conn = get_db()
    cur = conn.cursor()

    try:
        rows = cur.execute("""
            SELECT topics FROM conversation_summaries WHERE topics IS NOT NULL AND topics != ''
        """).fetchall()

        topic_counts: dict[str, int] = {}
        for row in rows:
            topics_str = row["topics"]
            for topic in topics_str.split(","):
                topic = topic.strip().lower()
                if topic:
                    topic_counts[topic] = topic_counts.get(topic, 0) + 1

        # Sort by count descending
        sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)

        return jsonify({
            "topics": [{"name": name, "count": count} for name, count in sorted_topics]
        })
    except Exception as e:
        return jsonify({"error": str(e), "topics": []}), 500


@app.route("/api/meals")
def get_meals() -> Response:
    """
    Get meal logs with optional date filtering.

    Query params:
    - from_date: Start date (YYYY-MM-DD)
    - to_date: End date (YYYY-MM-DD)
    - limit: Max results (default 100)
    """
    conn = get_db()
    cur = conn.cursor()

    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)

    params: list[Any] = []
    conditions: list[str] = []

    if from_date:
        conditions.append("date(ts_utc) >= ?")
        params.append(from_date)

    if to_date:
        conditions.append("date(ts_utc) <= ?")
        params.append(to_date)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT * FROM meals
        WHERE {where_clause}
        ORDER BY ts_utc DESC
        LIMIT ?
    """
    params.append(limit)

    try:
        rows = cur.execute(query, params).fetchall()
        meals = [row_to_dict(row) for row in rows]
        return jsonify({"meals": meals, "count": len(meals)})
    except Exception as e:
        return jsonify({"error": str(e), "meals": [], "count": 0}), 500


# ── Chat ──────────────────────────────────────────────────────────────
#
# One conversation for the dashboard, held for the life of the server so
# follow-ups ("and where do they live?") see the earlier turns. The
# engine writes the diary and knowledge graph itself, exactly as it does
# for the terminal front end — the browser is another way in, not a
# second assistant.
_chat_lock = threading.Lock()
_chat_memory = None


def _flush_chat_to_diary(memory) -> bool:
    """Persist pending browser conversation to the diary and graph.

    Mirrors what the terminal front end does on /reset and on exit. Never
    raises: losing a diary entry is bad, but taking the dashboard down
    with it is worse.
    """
    try:
        from jarvis.llm import Tier, resolve_model
        from jarvis.memory.conversation import update_diary_from_dialogue_memory
        from jarvis.memory.db import Database

        if not memory.has_pending_chunks():
            return False

        cfg = load_settings()
        db = Database(_get_db_path(), cfg.sqlite_vss_path)
        try:
            summary_id = update_diary_from_dialogue_memory(
                db=db, dialogue_memory=memory, cfg=cfg,
                source_app="stdin",
                timeout_sec=cfg.llm_chat_timeout_sec,
                force=True,
                graph_picker_model=resolve_model(cfg, Tier.FAST),
            )
        finally:
            db.close()
        return summary_id is not None
    except Exception as e:
        debug_log(f"dashboard diary flush failed: {e}", "memory_viewer")
        return False


def _get_chat_memory():
    global _chat_memory
    if _chat_memory is None:
        from jarvis.memory.conversation import DialogueMemory

        cfg = load_settings()
        _chat_memory = DialogueMemory(
            inactivity_timeout=cfg.dialogue_memory_timeout, max_interactions=20,
        )
    return _chat_memory


@app.route("/api/chat", methods=["POST"])
def chat() -> Response:
    """Answer one message from the dashboard."""
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    # Serialised: the reply engine mutates shared dialogue memory, and two
    # browser tabs posting at once would interleave turns into nonsense.
    with _chat_lock:
        try:
            import dataclasses

            from jarvis.memory.db import Database
            from jarvis.reply.engine import run_reply_engine

            cfg = dataclasses.replace(load_settings(), use_stdin=True)
            db = Database(_get_db_path(), cfg.sqlite_vss_path)
            try:
                reply = run_reply_engine(db, cfg, None, message, _get_chat_memory())
            finally:
                db.close()
        except Exception as e:
            debug_log(f"dashboard chat failed: {e}", "memory_viewer")
            return jsonify({"error": str(e)}), 500

    if not reply:
        return jsonify({"error": "the assistant returned no reply"}), 502

    # A browser tab closes without telling the server, so there is no
    # shutdown hook to flush on. An idle check after each turn bounds how
    # much a closed tab can lose to the inactivity window.
    with _chat_lock:
        try:
            memory = _get_chat_memory()
            if memory.should_update_diary():
                _flush_chat_to_diary(memory)
        except Exception as e:
            debug_log(f"post-turn diary check failed: {e}", "memory_viewer")

    return jsonify({"reply": reply})


def _config_path():
    import os

    from jarvis.config import default_config_path

    override = os.environ.get("JARVIS_CONFIG_PATH")
    return Path(override).expanduser() if override else default_config_path()


@app.route("/api/mcp")
def mcp_list() -> Response:
    """Configured MCP servers, with how many tools each is offering.

    Tool counts come from a live discovery pass, so a server that is
    configured but broken shows up as connected-with-zero rather than
    silently looking fine.
    """
    from jarvis.config import _load_json

    servers = (_load_json(_config_path()).get("mcps") or {})

    counts: dict = {}
    errors: dict = {}
    if servers:
        try:
            from jarvis.tools.registry import discover_mcp_tools

            tools, errors = discover_mcp_tools(servers)
            for name in tools:
                if "__" in name:
                    server = name.split("__")[0]
                    counts[server] = counts.get(server, 0) + 1
        except Exception as e:
            debug_log(f"MCP discovery from dashboard failed: {e}", "memory_viewer")

    return jsonify({
        "servers": [
            {
                "name": name,
                "command": spec.get("command", ""),
                "args": spec.get("args", []),
                "tools": counts.get(name, 0),
                "error": errors.get(name),
            }
            for name, spec in servers.items()
        ]
    })


@app.route("/api/mcp/catalogue")
def mcp_catalogue_list() -> Response:
    """The curated directory, with what a grid needs to render each entry.

    ``configured`` lets the grid show Added rather than Add, so a server is
    not installed twice.

    There is deliberately no "verified" flag. On other directories that mark
    means the vendor vetted the server; Jarvis vets nothing, and the entire
    MCP security layer exists because a server that looks fine may not be.
    """
    from jarvis.config import _load_json
    from jarvis.utils.mcp_catalogue import load_entries

    configured = set((_load_json(_config_path()).get("mcps") or {}).keys())

    return jsonify({
        "entries": [
            {
                "name": e.name,
                "display_name": e.display_name,
                "description": e.description,
                "category": e.category,
                "needs_api_key": bool(e.needs_api_key),
                "api_key_hint": e.api_key_hint or "",
                "command": e.command,
                "args": list(e.args),
                "configured": e.name in configured,
            }
            for e in load_entries()
        ]
    })


@app.route("/api/mcp/catalogue/<name>", methods=["POST"])
def mcp_catalogue_add(name: str) -> Response:
    """Add a curated server using the catalogue's own pinned arguments.

    The manual form lets someone type a package without a version, which the
    supply-chain guard then refuses at spawn time. Adding from the catalogue
    cannot produce that state, because the args come from the entry rather
    than from anything typed.
    """
    from jarvis.config import _load_json, _save_json
    from jarvis.utils.mcp_catalogue import load_by_name

    entry = load_by_name().get(name)
    if entry is None:
        return jsonify({"error": f"no catalogue entry named {name}"}), 404

    payload = request.get_json(silent=True) or {}
    api_key = str(payload.get("api_key", "")).strip()
    extra_env = {}
    if api_key and entry.api_key_env_var:
        extra_env[entry.api_key_env_var] = api_key

    path = _config_path()
    cfg_json = _load_json(path)
    mcps = cfg_json.setdefault("mcps", {})
    collision = _key_collision(mcps, name)
    if collision is not None:
        return collision
    mcps[name] = entry.to_config(extra_env or None)
    if not _save_json(path, cfg_json):
        return jsonify({"error": "could not write config.json"}), 500
    debug_log(f"added catalogue MCP server {name} from the dashboard", "memory_viewer")
    return jsonify({"ok": True})


@app.route("/api/mcp/registry")
def mcp_registry_list() -> Response:
    """The registry directory, read from the local cache only.

    Browsing never reaches the network. Running with no network is a
    supported way to use Jarvis, and the dashboard renders the user's diary,
    so it does not quietly make outbound requests while they read it.
    """
    from jarvis.config import _load_json
    from jarvis.tools.external.mcp_registry import load_cached

    entries, fetched_at = load_cached()
    configured = set((_load_json(_config_path()).get("mcps") or {}).keys())
    for entry in entries:
        entry["configured"] = _registry_config_name(entry["name"]) in configured
    return jsonify({"entries": entries, "fetched_at": fetched_at})


@app.route("/api/mcp/registry/refresh", methods=["POST"])
def mcp_registry_refresh() -> Response:
    """Fetch the registry. Explicit, because it is the one outbound request
    the dashboard makes and the user should be the one asking for it."""
    from jarvis.tools.external.mcp_registry import RegistryUnavailableError, refresh

    try:
        entries = refresh()
    except RegistryUnavailableError as e:
        return jsonify({"error": f"could not reach the registry: {e}"}), 503
    return jsonify({"ok": True, "count": len(entries)})


def _key_collision(mcps: dict, key: str) -> Optional[Response]:
    """A 409 when ``key`` is taken, or ``None`` when it is free.

    Config keys are flat while registry names are namespaced, so
    `io.github.evil/github`, `io.github.honest/github` and the curated
    `github` all want the same key. Whichever add path runs second must
    refuse: overwriting swaps a configured server, and any credential stored
    beside it, for different code, while the other grid goes on reporting the
    original as installed. The check belongs to every path that writes a key,
    not just the one where the collision was first noticed.
    """
    if key not in mcps:
        return None
    debug_log(f"refused add: config key '{key}' already exists", "memory_viewer")
    return jsonify({
        "error": f"a server named '{key}' already exists; remove it first",
    }), 409


def _registry_config_name(registry_name: str) -> str:
    """A config key for a registry server.

    Registry names are reverse-DNS paths (``io.github.acme/thing``). The
    part after the slash is what a person would call the server, and the
    tool namespace derives from it.
    """
    return registry_name.rsplit("/", 1)[-1] or registry_name


@app.route("/api/mcp/registry/add", methods=["POST"])
def mcp_registry_add() -> Response:
    """Install a cached registry server using its pinned package version."""
    from jarvis.config import _load_json, _save_json
    from jarvis.tools.external.mcp_registry import find_cached
    from jarvis.tools.external.mcp_supply_chain import validate_server_launch

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    entry = find_cached(name) if name else None
    if entry is None:
        return jsonify({"error": f"no cached registry entry named {name}"}), 404
    if not entry.get("install"):
        # Either the package is unpinned or its ecosystem is one the
        # supply-chain guard cannot pin, so any config written here would be
        # refused at spawn time.
        return jsonify({"error": "that server has no pinned package to install"}), 400

    install = entry.get("install")
    # The cache is plain JSON in the config directory, so pin checking at
    # fetch time says nothing about what is on disk now. The spawn-time guard
    # is the only check that agrees with spawn time, so run it here too.
    if not isinstance(install, dict):
        return jsonify({"error": "that server has no usable launch config"}), 400
    path = _config_path()
    cfg_json = _load_json(path)
    mcps = cfg_json.setdefault("mcps", {})
    key = _registry_config_name(name)
    collision = _key_collision(mcps, key)
    if collision is not None:
        return collision
    try:
        validate_server_launch(key, install)
    except Exception as e:  # noqa: BLE001
        debug_log(f"refused registry add {name}: {e}", "memory_viewer")
        return jsonify({"error": "that server has no pinned package to install"}), 400
    mcps[key] = dict(install)
    if not _save_json(path, cfg_json):
        return jsonify({"error": "could not write config.json"}), 500
    debug_log(f"added registry MCP server {name} from the dashboard", "memory_viewer")
    return jsonify({"ok": True})


@app.route("/api/mcp", methods=["POST"])
def mcp_add() -> Response:
    """Add or replace one MCP server, persisting to config.json."""
    from jarvis.config import _load_json, _save_json

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    command = str(payload.get("command", "")).strip()
    if not name or not command:
        return jsonify({"error": "name and command are required"}), 400

    args = payload.get("args") or []
    if isinstance(args, str):
        args = args.split()

    path = _config_path()
    cfg_json = _load_json(path)
    cfg_json.setdefault("mcps", {})[name] = {
        "command": command,
        "args": [str(a) for a in args],
    }
    if not _save_json(path, cfg_json):
        return jsonify({"error": "could not write config.json"}), 500
    return jsonify({"ok": True})


@app.route("/api/mcp/<name>", methods=["DELETE"])
def mcp_remove(name: str) -> Response:
    from jarvis.config import _load_json, _save_json

    path = _config_path()
    cfg_json = _load_json(path)
    if (cfg_json.get("mcps") or {}).pop(name, None) is None:
        return jsonify({"error": f"no MCP server named {name}"}), 404
    if not _save_json(path, cfg_json):
        return jsonify({"error": "could not write config.json"}), 500
    return jsonify({"ok": True})


@app.route("/api/system")
def system_status() -> Response:
    """Live host telemetry for the HUD rail.

    Every field is real or absent — a panel that invents numbers is worse
    than one that says it has none, because an operator reads it as truth.
    """
    out: dict = {}
    try:
        import psutil

        out["cpu"] = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        out["memory"] = {"percent": mem.percent,
                         "used_gb": round(mem.used / 1024 ** 3, 1),
                         "total_gb": round(mem.total / 1024 ** 3, 1)}
        disk = psutil.disk_usage("/")
        out["disk"] = {"percent": disk.percent,
                       "free_gb": round(disk.free / 1024 ** 3, 1)}
    except Exception as e:
        debug_log(f"system telemetry unavailable: {e}", "memory_viewer")

    try:
        cfg = load_settings()
        out["model"] = cfg.llm_chat_model
        out["provider"] = cfg.llm_provider
    except Exception:
        pass

    return jsonify(out)


@app.route("/api/weather")
def weather_now() -> Response:
    """Current conditions, via the same tool the assistant uses.

    Returns 503 rather than a placeholder when location is off or the
    lookup fails, so the panel can say so instead of showing a number
    nobody can trust.
    """
    try:
        from jarvis.memory.db import Database
        from jarvis.tools.base import ToolContext
        from jarvis.tools.registry import BUILTIN_TOOLS

        cfg = load_settings()
        db = Database(_get_db_path(), cfg.sqlite_vss_path)
        try:
            ctx = ToolContext(db=db, cfg=cfg, system_prompt="", original_prompt="weather",
                              redacted_text="weather", max_retries=1,
                              user_print=lambda *a, **k: None)
            result = BUILTIN_TOOLS["getWeather"].run({}, ctx)
        finally:
            db.close()
        if not result.success or not result.reply_text:
            return jsonify({"error": "weather unavailable"}), 503
        return jsonify({"text": result.reply_text})
    except Exception as e:
        debug_log(f"weather panel failed: {e}", "memory_viewer")
        return jsonify({"error": str(e)}), 503


# Fields the settings panel may write. An allow-list, so a POST cannot
# reach unrelated config (db paths, MCP commands, wake words).
_SETTINGS_FIELDS = (
    "llm_provider", "llm_base_url", "llm_chat_model", "fast_model",
    "embedding_provider", "embedding_model",
)


def _key_hint(key: str) -> str:
    """Last four characters, so a user can tell which key is loaded."""
    key = (key or "").strip()
    return f"…{key[-4:]}" if len(key) >= 4 else ""


@app.route("/api/yolo")
def yolo_state() -> Response:
    """Current YOLO state, for the button's label and countdown."""
    from jarvis import approval

    return jsonify({
        "active": approval.is_active(),
        "remaining_sec": int(approval.remaining_sec()),
        "label": approval.describe_remaining(),
        "choices": list(approval.GRANT_CHOICES_MINUTES),
        "min_minutes": approval.MIN_GRANT_MINUTES,
        "max_minutes": approval.MAX_GRANT_MINUTES,
        "step_minutes": approval.GRANT_STEP_MINUTES,
    })


@app.route("/api/yolo", methods=["POST"])
def yolo_set() -> Response:
    """Open or close the YOLO window.

    POST rather than GET on purpose. Jarvis's own `fetchWebPage` issues
    GETs, so keeping the state-changing verb off GET means that even if a
    tool were pointed at this URL it could only read. It could not
    authenticate either — the session token is given to this process, not
    to the daemon — but a privilege-granting endpoint should not lean on
    one control alone.
    """
    from jarvis import approval

    payload = request.get_json(silent=True) or {}
    if payload.get("off"):
        approval.revoke()
        return jsonify({"active": False, "label": approval.describe_remaining()})

    minutes = payload.get("minutes")
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
        return jsonify({"error": "minutes must be a number"}), 400
    if not approval.grant(minutes):
        return jsonify({"error": "that is not a usable duration"}), 400

    debug_log(f"YOLO granted for {minutes} minutes from the dashboard", "memory_viewer")
    return jsonify({
        "active": approval.is_active(),
        "remaining_sec": int(approval.remaining_sec()),
        "label": approval.describe_remaining(),
    })


@app.route("/api/settings")
def settings_get() -> Response:
    """Current LLM settings. The key itself is never returned.

    Sending it back would put a live credential in the page source, in
    the browser cache, and in anything that scrapes the DOM. A hint is
    enough to answer "which key is this?".
    """
    from jarvis.config import _load_json

    raw = _load_json(_config_path())
    out = {f: raw.get(f, "") for f in _SETTINGS_FIELDS}
    out["has_key"] = bool((raw.get("llm_api_key") or "").strip())
    out["key_hint"] = _key_hint(raw.get("llm_api_key", ""))
    return jsonify(out)


@app.route("/api/settings", methods=["POST"])
def settings_post() -> Response:
    """Write LLM settings, including an optional new API key."""
    from jarvis.config import _load_json, _save_json

    payload = request.get_json(silent=True) or {}
    raw = _load_json(_config_path())

    for field in _SETTINGS_FIELDS:
        if field in payload:
            raw[field] = str(payload[field] or "").strip()

    # Only replace the key when a new one is actually supplied, so the
    # user can change the model without re-typing their credential.
    new_key = str(payload.get("llm_api_key", "") or "").strip()
    if new_key:
        raw["llm_api_key"] = new_key

    if not _save_json(_config_path(), raw):
        return jsonify({"error": "could not write config.json"}), 500
    return jsonify({"ok": True, "note": "Applied on the next message."})


@app.route("/api/settings/test", methods=["POST"])
def settings_test() -> Response:
    """Ask the configured endpoint for its model list.

    Saving a wrong key otherwise fails silently much later, inside a
    reply, where the user cannot tell config from outage.

    The saved key is only ever sent back to the host it was saved for.
    Without that, POSTing {"llm_base_url": "http://attacker/v1"} made
    this endpoint read the user's live credential out of config.json and
    hand it to an arbitrary server — credential exfiltration through a
    button labelled "Test connection". Testing a new host therefore
    requires supplying its key in the request.
    """
    import ipaddress
    import socket
    import urllib.parse

    import requests as _rq

    from jarvis.config import _load_json

    payload = request.get_json(silent=True) or {}
    base = str(payload.get("llm_base_url", "") or "").strip().rstrip("/")
    if not base:
        return jsonify({"error": "base URL is required"}), 400

    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ("http", "https"):
        return jsonify({"error": "only http and https endpoints can be tested"}), 400
    if not parsed.hostname:
        return jsonify({"error": "that URL has no host"}), 400

    saved = _load_json(_config_path())
    saved_base = str(saved.get("llm_base_url", "") or "").strip().rstrip("/")
    key = str(payload.get("llm_api_key", "") or "").strip()

    if not key:
        # Reuse the stored credential only for the host it belongs to.
        same_host = urllib.parse.urlparse(saved_base).hostname == parsed.hostname
        if same_host:
            key = str(saved.get("llm_api_key", "") or "").strip()
        else:
            return jsonify({
                "error": "Enter the API key for this endpoint to test it. "
                         "The saved key is only sent to the host it was saved for."
            }), 400

    # Block the loopback/link-local/private ranges. A local endpoint is a
    # legitimate setup (Ollama, LM Studio), so this is allowed explicitly
    # rather than by accident — and only when no credential is at stake.
    try:
        resolved = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
        if (resolved.is_private or resolved.is_loopback or resolved.is_link_local) and key:
            return jsonify({
                "error": "Refusing to send a key to a private address. "
                         "Local servers (Ollama, LM Studio) need no key — leave it blank."
            }), 400
    except Exception:
        return jsonify({"error": "could not resolve that host"}), 400

    try:
        resp = _rq.get(f"{base}/models",
                       headers={"Authorization": f"Bearer {key}"} if key else {},
                       timeout=20, allow_redirects=False)
        if not resp.ok:
            return jsonify({"error": f"endpoint returned HTTP {resp.status_code}"}), 502
        data = resp.json()
        models = [m.get("id", "") for m in (data.get("data") or [])]
        return jsonify({"ok": True, "count": len(models), "models": models[:60]})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 502


@app.route("/api/chat/reset", methods=["POST"])
def chat_reset() -> Response:
    """Write the conversation to the diary, then start a fresh one.

    The flush has to happen before the boundary is drawn, and it has to
    happen at all: the UI tells the user "the previous one was saved",
    and until this existed that was simply untrue — closing the tab
    discarded everything said in the browser. chat.spec.md states the
    contract as "nothing said is lost on exit"; the terminal front end
    honoured it and this one did not.
    """
    with _chat_lock:
        memory = _get_chat_memory()
        try:
            saved = _flush_chat_to_diary(memory)
        except Exception as e:
            # The user still gets their fresh conversation; they are told
            # the save failed rather than left with a broken button.
            debug_log(f"diary flush raised during reset: {e}", "memory_viewer")
            saved = False
        memory.start_new_conversation()
    return jsonify({"ok": True, "saved": saved})


@app.route("/api/stats")
def get_stats() -> Response:
    """Get memory statistics."""
    conn = get_db()
    cur = conn.cursor()

    try:
        # Total memories
        total_memories = cur.execute("SELECT COUNT(*) as count FROM conversation_summaries").fetchone()["count"]

        # Date range
        date_range = cur.execute("""
            SELECT MIN(date_utc) as earliest, MAX(date_utc) as latest
            FROM conversation_summaries
        """).fetchone()

        # Memories by month
        monthly_stats = cur.execute("""
            SELECT strftime('%Y-%m', date_utc) as month, COUNT(*) as count
            FROM conversation_summaries
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """).fetchall()

        # Total meals
        total_meals = cur.execute("SELECT COUNT(*) as count FROM meals").fetchone()["count"]

        return jsonify({
            "total_memories": total_memories,
            "earliest_date": date_range["earliest"],
            "latest_date": date_range["latest"],
            "monthly_stats": [row_to_dict(row) for row in monthly_stats],
            "total_meals": total_meals
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/memory/<int:memory_id>")
def get_memory(memory_id: int) -> Response:
    """Get a single memory by ID."""
    conn = get_db()
    cur = conn.cursor()

    try:
        row = cur.execute("""
            SELECT * FROM conversation_summaries WHERE id = ?
        """, (memory_id,)).fetchone()

        if row:
            memory = row_to_dict(row)
            if memory.get("topics"):
                memory["topics_list"] = [t.strip() for t in memory["topics"].split(",") if t.strip()]
            else:
                memory["topics_list"] = []
            return jsonify({"memory": memory})
        else:
            return jsonify({"error": "Memory not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/memory/<int:memory_id>", methods=["DELETE"])
def delete_memory(memory_id: int) -> Response:
    """Delete a memory by ID."""
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("DELETE FROM conversation_summaries WHERE id = ?", (memory_id,))
        conn.commit()

        if cur.rowcount > 0:
            return jsonify({"success": True, "message": "Memory deleted"})
        else:
            return jsonify({"error": "Memory not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/meal/<int:meal_id>", methods=["DELETE"])
def delete_meal(meal_id: int) -> Response:
    """Delete a meal by ID."""
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
        conn.commit()

        if cur.rowcount > 0:
            return jsonify({"success": True, "message": "Meal deleted"})
        else:
            return jsonify({"error": "Meal not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Identity API
# ─────────────────────────────────────────────────────────────────────────────

def get_identity_store() -> IdentityStore:
    """Get or create the identity store (shares the same DB)."""
    global _identity_store
    if _identity_store is None:
        _identity_store = IdentityStore(_get_db_path())
    return _identity_store


@app.route("/api/identity")
def get_identity() -> Response:
    """Who this dashboard is showing, and which machine it is running on.

    Establishes the identity if it is not there yet, because the
    dashboard can be opened before the daemon has ever run.
    """
    try:
        store = get_identity_store()
        identity = store.ensure_local_identity()
        debug_log(f"dashboard is showing device {identity.device.id}", "identity")
        return jsonify({
            "user": {
                "id": identity.user.id,
                "display_name": identity.user.display_name,
            },
            "workspace": {
                "id": identity.workspace.id,
                "name": identity.workspace.name,
                "kind": identity.workspace.kind,
            },
            "device": {
                "id": identity.device.id,
                "name": identity.device.name,
                "platform": identity.device.platform,
                "last_seen_at": identity.device.last_seen_at,
            },
            "devices": [
                {
                    "id": device.id,
                    "name": device.name,
                    "platform": device.platform,
                    "last_seen_at": device.last_seen_at,
                    "is_this_one": device.id == identity.device.id,
                }
                for device in store.get_devices(user_id=identity.user.id)
            ],
            "accounts": [
                {
                    "id": account.id,
                    "provider": account.provider,
                    "label": account.account_label,
                    "workspace_id": account.workspace_id,
                }
                for account in store.get_accounts(user_id=identity.user.id)
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Graph Memory (v2) API
# ─────────────────────────────────────────────────────────────────────────────

def get_graph_store() -> GraphMemoryStore:
    """Get or create the graph memory store (shares the same DB)."""
    global _graph_store
    if _graph_store is None:
        _graph_store = GraphMemoryStore(_get_db_path())
    return _graph_store


@app.route("/api/graph/nodes")
def graph_get_all_nodes() -> Response:
    """Get all nodes for the graph visualisation."""
    store = get_graph_store()
    try:
        root_id = request.args.get("root", "root")
        max_depth = min(int(request.args.get("max_depth", 10)), 20)
        data = store.get_graph_data(root_id, max_depth=max_depth)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/tree")
def graph_get_tree() -> Response:
    """Get the full tree structure for the sidebar."""
    store = get_graph_store()
    try:
        root_id = request.args.get("root", "root")
        max_depth = min(int(request.args.get("max_depth", 10)), 20)
        tree = store.get_subtree(root_id, max_depth=max_depth)
        return jsonify(tree)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/node/<node_id>")
def graph_get_node(node_id: str) -> Response:
    """Get a single node with its children and ancestors."""
    store = get_graph_store()
    try:
        node = store.get_node(node_id)
        if node is None:
            return jsonify({"error": "Node not found"}), 404

        store.touch_node(node_id)
        children = store.get_children(node_id)
        ancestors = store.get_ancestors(node_id)

        return jsonify({
            "node": node.to_dict(),
            "children": [c.to_dict() for c in children],
            "ancestors": [a.to_dict() for a in ancestors],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/node", methods=["POST"])
def graph_create_node() -> Response:
    """Create a new memory node."""
    store = get_graph_store()
    try:
        body = request.get_json()
        if not body or not body.get("name"):
            return jsonify({"error": "name is required"}), 400

        # Validate field types
        name = body["name"]
        description = body.get("description", "")
        data = body.get("data", "")
        parent_id = body.get("parent_id", "root")
        if not isinstance(name, str) or not isinstance(description, str) \
                or not isinstance(data, str) or not isinstance(parent_id, str):
            return jsonify({"error": "name, description, data, and parent_id must be strings"}), 400

        node = store.create_node(
            name=name,
            description=description,
            data=data,
            parent_id=parent_id,
        )
        return jsonify({"node": node.to_dict()}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/node/<node_id>", methods=["PUT"])
def graph_update_node(node_id: str) -> Response:
    """Update an existing memory node."""
    store = get_graph_store()
    try:
        body = request.get_json()
        if not body:
            return jsonify({"error": "Request body is required"}), 400

        kwargs = {}
        for field in ("name", "description", "data", "parent_id"):
            if field in body:
                if not isinstance(body[field], str):
                    return jsonify({"error": f"{field} must be a string"}), 400
                kwargs[field] = body[field]

        node = store.update_node(node_id, **kwargs)
        if node is None:
            return jsonify({"error": "Node not found or invalid parent"}), 404

        return jsonify({"node": node.to_dict()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/node/<node_id>", methods=["DELETE"])
def graph_delete_node(node_id: str) -> Response:
    """Delete a memory node."""
    store = get_graph_store()
    try:
        if node_id == "root":
            return jsonify({"error": "Cannot delete root node"}), 400
        if node_id in FIXED_BRANCH_IDS:
            return jsonify({"error": "Cannot delete preset branch"}), 400

        deleted = store.delete_node(node_id)
        if deleted:
            return jsonify({"success": True})
        return jsonify({"error": "Node not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/presets")
def graph_presets() -> Response:
    """IDs of non-deletable preset nodes (root + FIXED_BRANCH_IDS).

    Single source of truth for the UI: avoids duplicating the branch list
    on the JS side, so adding a new fixed branch only requires editing
    ``FIXED_BRANCHES`` in graph.py.
    """
    return jsonify({"ids": ["root", *sorted(FIXED_BRANCH_IDS)]})


@app.route("/api/graph/recent")
def graph_recent_nodes() -> Response:
    """Get recently accessed nodes."""
    store = get_graph_store()
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
        nodes = store.get_recent_nodes(limit)
        return jsonify({"nodes": [n.to_dict() for n in nodes]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/top")
def graph_top_nodes() -> Response:
    """Get most frequently accessed nodes."""
    store = get_graph_store()
    try:
        limit = min(int(request.args.get("limit", 15)), 50)
        nodes = store.get_top_nodes(limit)
        return jsonify({"nodes": [n.to_dict() for n in nodes]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/stats")
def graph_stats() -> Response:
    """Get graph memory statistics."""
    store = get_graph_store()
    try:
        return jsonify({
            "total_nodes": store.get_node_count(),
            "total_tokens": store.get_total_tokens(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/import-diary", methods=["POST"])
def graph_import_diary() -> Response:
    """Import all diary conversation summaries into the graph memory system.

    Processes each summary through the extract → traverse → append → split
    pipeline. Returns a streaming response with progress updates so the UI
    can show real-time feedback.
    """
    from jarvis.config import load_settings
    from jarvis.memory.db import Database
    from jarvis.memory.graph_ops import update_graph_from_dialogue
    from jarvis.llm import resolve_model, Tier

    def generate():
        try:
            settings = load_settings()
            db_path = _get_db_path()
            db = Database(db_path, sqlite_vss_path=None)
            # Run the best-child picker on the small router-chain model so
            # historical import doesn't page in the big chat model for every
            # placement decision.
            picker_model = resolve_model(settings, Tier.FAST)

            summaries = db.get_all_conversation_summaries()
            total = len(summaries)

            if total == 0:
                yield json.dumps({"type": "complete", "message": "No diary entries found to import.", "processed": 0, "total": 0}) + "\n"
                return

            yield json.dumps({"type": "start", "total": total}) + "\n"

            store = get_graph_store()
            processed = 0
            total_facts = 0

            for row in summaries:
                summary_text = row["summary"]
                date_utc = row["date_utc"]
                error_msg = None

                try:
                    debug_log(f"graph import: processing {date_utc} ({len(summary_text)} chars)", "memory")
                    result = update_graph_from_dialogue(
                        store=store,
                        summary=summary_text,
                        cfg=settings,
                        chat_model=settings.llm_chat_model,
                        timeout_sec=settings.llm_chat_timeout_sec,
                        thinking=getattr(settings, 'llm_thinking_enabled', False),
                        date_utc=date_utc,
                        picker_model=picker_model,
                    )
                    facts_stored = len(result.stored)
                    total_facts += facts_stored
                except Exception as e:
                    debug_log(f"graph import: failed for {date_utc} — {e}", "memory")
                    facts_stored = 0
                    error_msg = str(e)

                processed += 1
                progress_msg = {
                    "type": "progress",
                    "processed": processed,
                    "total": total,
                    "date": date_utc,
                    "facts": facts_stored,
                }
                if error_msg:
                    progress_msg["error"] = error_msg
                yield json.dumps(progress_msg) + "\n"

            yield json.dumps({
                "type": "complete",
                "message": f"Imported {total_facts} facts from {total} diary entries.",
                "processed": processed,
                "total": total,
                "total_facts": total_facts,
            }) + "\n"

            db.close()

        except Exception as e:
            debug_log(f"graph import failed: {e}", "memory")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/graph/consolidate-all", methods=["POST"])
def graph_consolidate_all() -> Response:
    """Run the merge prompt's consolidation rules over every populated node.

    Migration path for nodes that accumulated contradictions before
    merge-on-write landed: under merge-on-write, a node only gets
    cleaned when a new related fact arrives, so backlog stays dirty
    until something nudges it. This endpoint nudges everything at
    once via `consolidate_all_populated_nodes`, streaming NDJSON
    progress so the UI can show per-node line-count deltas.
    """
    from jarvis.config import load_settings
    from jarvis.memory.graph_ops import (
        consolidate_all_populated_nodes,
        is_populated_node,
    )
    from jarvis.llm import resolve_model, Tier

    def generate():
        try:
            settings = load_settings()
            picker_model = resolve_model(settings, Tier.FAST)
            store = get_graph_store()

            # Count populated nodes upfront so the UI can render a
            # real progress bar. Reuses the shared predicate from
            # `graph_ops` so the count can never drift from the set
            # the generator actually walks. The double scan is
            # acceptable here — `get_all_nodes` is one cheap SQLite
            # read and the bar's accuracy is worth more than the saved
            # walk on the rarely-pressed maintenance op.
            total_nodes = sum(
                1 for n in store.get_all_nodes() if is_populated_node(n)
            )
            yield json.dumps({"type": "start", "total": total_nodes}) + "\n"

            total_before = 0
            total_after = 0
            node_count = 0
            # Stream per-node deltas as the generator yields them so
            # the UI gets real-time feedback on graphs with many
            # nodes — buffering the full sweep would defeat NDJSON.
            for name, before, after in consolidate_all_populated_nodes(
                store=store,
                cfg=settings,
                chat_model=settings.llm_chat_model,
                timeout_sec=20.0,
                thinking=getattr(settings, 'llm_thinking_enabled', False),
                picker_model=picker_model,
            ):
                node_count += 1
                total_before += before
                total_after += after
                yield json.dumps({
                    "type": "progress",
                    "node": name,
                    "before": before,
                    "after": after,
                    "delta": after - before,
                }) + "\n"

            yield json.dumps({
                "type": "complete",
                "nodes": node_count,
                "total_before": total_before,
                "total_after": total_after,
                "total_delta": total_after - total_before,
            }) + "\n"
        except Exception as e:
            debug_log(f"consolidate-all failed: {e}", "memory")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/diary/scrub-deflections", methods=["POST"])
def diary_scrub_deflections() -> Response:
    """Ask the chat model to remove deflection narration from every diary row.

    The summariser prompt forbids deflection narration at write time, but
    rows written before the prompt was tightened can still contain leaked
    phrasing. This endpoint walks every row and asks the configured chat
    model to rewrite it, dropping sentences that narrate the assistant's
    own failures while keeping everything else verbatim.

    Streams NDJSON progress so the UI can render per-row deltas. Crucially,
    the event payload contains *only* counts (char deltas, booleans, the
    date) — never raw summary text — so this endpoint cannot leak diary
    content to the UI.

    Requires the chat model to be running. Per-row rewrite failures are
    fail-open: the row is left untouched, the sweep continues.
    """
    from jarvis.config import load_settings
    from jarvis.memory.conversation import rewrite_all_diary_summaries
    from jarvis.memory.db import Database

    def generate():
        db = None
        try:
            settings = load_settings()
            db_path = _get_db_path()
            # Open with the configured VSS path so embedding refresh
            # actually targets the same vector store the rest of the app
            # reads from. Without this the bulk sweep would silently skip
            # re-embedding on installations that have VSS enabled.
            sqlite_vss_path = getattr(settings, "sqlite_vss_path", None)
            db = Database(db_path, sqlite_vss_path=sqlite_vss_path)

            total = len(db.get_all_conversation_summaries())
            yield json.dumps({"type": "start", "total": total}) + "\n"

            if total == 0:
                yield json.dumps({
                    "type": "complete",
                    "rows": 0,
                    "rows_rewritten": 0,
                    "rows_would_empty": 0,
                    "embeddings_refreshed": 0,
                }) + "\n"
                return

            rows_rewritten = 0
            rows_would_empty = 0
            rows_seen = 0
            embeddings_refreshed = 0

            for event in rewrite_all_diary_summaries(db, settings):
                rows_seen += 1
                if event.get("rewritten"):
                    rows_rewritten += 1
                if event.get("would_empty"):
                    rows_would_empty += 1
                if event.get("embedding_refreshed"):
                    embeddings_refreshed += 1
                yield json.dumps({
                    "type": "progress",
                    "processed": rows_seen,
                    "total": total,
                    **event,
                }) + "\n"

            yield json.dumps({
                "type": "complete",
                "rows": rows_seen,
                "rows_rewritten": rows_rewritten,
                "rows_would_empty": rows_would_empty,
                "embeddings_refreshed": embeddings_refreshed,
            }) + "\n"
        except Exception as e:
            debug_log(f"diary rewrite failed: {type(e).__name__}", "memory")
            # Surface only the class name to the streaming UI so a
            # corrupted row's content cannot leak via the exception
            # message.
            yield json.dumps({"type": "error", "message": type(e).__name__}) + "\n"
        finally:
            # The connection leaks if we close only on the success path —
            # a mid-iteration exception would orphan it until GC.
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/diary/optimise-topics", methods=["POST"])
def diary_optimise_topics() -> Response:
    """Normalise topic tags across every diary row via one LLM call.

    Collects all unique tags, asks the configured chat model to propose a
    normalised taxonomy (merging synonyms, splitting compound tags), then
    applies the mapping to every row whose topics change. Streams NDJSON
    progress so the UI shows per-row feedback in real time.

    Event payload contains only counts and the date — never raw tag strings
    — so this endpoint cannot leak diary content to the streaming UI.
    """
    from jarvis.config import load_settings
    from jarvis.memory.conversation import optimise_diary_topics
    from jarvis.memory.db import Database

    def generate():
        db = None
        try:
            settings = load_settings()
            db_path = _get_db_path()
            sqlite_vss_path = getattr(settings, "sqlite_vss_path", None)
            db = Database(db_path, sqlite_vss_path=sqlite_vss_path)

            total = len(db.get_all_conversation_summaries())
            yield json.dumps({"type": "start", "total": total}) + "\n"

            if total == 0:
                yield json.dumps({
                    "type": "complete",
                    "rows": 0,
                    "rows_changed": 0,
                    "topics_merged": 0,
                    "topics_expanded": 0,
                }) + "\n"
                return

            rows_changed = 0
            rows_seen = 0
            topics_merged = 0
            topics_expanded = 0

            for event in optimise_diary_topics(db, settings):
                rows_seen += 1
                if event.get("topics_changed"):
                    rows_changed += 1
                    old_n = event.get("old_topic_count", 0)
                    new_n = event.get("new_topic_count", 0)
                    if new_n < old_n:
                        topics_merged += old_n - new_n
                    elif new_n > old_n:
                        topics_expanded += new_n - old_n
                yield json.dumps({
                    "type": "progress",
                    "processed": rows_seen,
                    "total": total,
                    **event,
                }) + "\n"

            yield json.dumps({
                "type": "complete",
                "rows": rows_seen,
                "rows_changed": rows_changed,
                "topics_merged": topics_merged,
                "topics_expanded": topics_expanded,
            }) + "\n"
        except Exception as e:
            debug_log(f"diary topic optimise failed: {type(e).__name__}", "memory")
            yield json.dumps({"type": "error", "message": type(e).__name__}) + "\n"
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the dashboard, taking the token from the launch URL.

    Rejecting an unauthenticated page load outright would leave the user
    with a bare 401 and no way forward, so the page itself explains how
    to open it properly. The token is stored in a host-only, same-site
    cookie so subsequent API calls carry it without ever landing in
    browser history.
    """
    supplied = (
        request.args.get("token", "")
        or request.cookies.get(_TOKEN_COOKIE, "")
        or request.headers.get("X-Dashboard-Token", "")
    )
    if not _token_matches(supplied):
        return Response(
            "<h1>🔒 Jarvis dashboard</h1>"
            "<p>Open the URL printed in the terminal where you started it — "
            "it carries a one-time access token for this session.</p>",
            status=401,
            mimetype="text/html",
        )

    page = Response(render_template("index.html"), mimetype="text/html")
    page.set_cookie(
        _TOKEN_COOKIE,
        _SESSION_TOKEN,
        httponly=True,
        samesite="Strict",
        secure=False,  # loopback HTTP; the token never leaves this machine
    )
    return page


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Run the memory viewer server."""
    import sys

    port = 5050
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    print("\n" + "=" * 60)
    print("🧠 Jarvis Memory Viewer")
    print("=" * 60)
    print(f"\n  📂 Database: {_get_db_path()}")
    print(f"  🌐 URL: http://localhost:{port}/?token={_SESSION_TOKEN}")
    if _no_auth_enabled():
        print("\n  ⚠️  JARVIS_DASHBOARD_NO_AUTH is set — no token is required.")
        print("     Any process on this machine can read your diary and")
        print("     register an MCP command that Jarvis will run as you.")
        print("     Unset it for anything but a local demo.")
    else:
        print("  🔒 That token is minted per launch — the dashboard shows your")
        print("     diary and can act as you, so it is not open to the machine.")
    print("\n  Press Ctrl+C to stop\n")
    print("=" * 60 + "\n")

    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
