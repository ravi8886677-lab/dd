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
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, request, Response

from jarvis.config import load_settings
from jarvis.debug import debug_log
from jarvis.memory.graph import FIXED_BRANCH_IDS, GraphMemoryStore


app = Flask(__name__)

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
# nothing needs to be stored on disk.
# The desktop app launches this as a subprocess and needs the same
# token to build the window's URL, so it may mint one and pass it in.
_SESSION_TOKEN = os.environ.get("JARVIS_DASHBOARD_TOKEN") or secrets.token_urlsafe(32)
_TOKEN_COOKIE = "jarvis_dashboard_token"

# Host headers that may address this server. Anything else means the
# request arrived via a name that resolves here — the rebinding case.
_ALLOWED_HOST_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _token_matches(supplied: str) -> bool:
    """Constant-time compare that tolerates any input.

    ``secrets.compare_digest`` raises TypeError on non-ASCII strings, so
    a token containing one byte of Unicode turned an auth failure into a
    500 with a traceback. Reject those as simply wrong.
    """
    try:
        return secrets.compare_digest(supplied or "", _SESSION_TOKEN)
    except TypeError:
        return False


def _host_is_allowed(host_header: str) -> bool:
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

    # The landing page accepts the token as a query parameter and stores
    # it, so the user only ever pastes the launch URL.
    if request.path == "/":
        return None

    supplied = (
        request.headers.get("X-Dashboard-Token")
        or request.cookies.get(_TOKEN_COOKIE)
        or request.args.get("token", "")
    )
    if not _token_matches(supplied):
        return jsonify({"error": "unauthorised — reopen the dashboard from its launch URL"}), 401
    return None

# Global database connection
_db_conn: Optional[sqlite3.Connection] = None
_graph_store: Optional[GraphMemoryStore] = None


def _get_db_path() -> str:
    """Get the database path from settings."""
    try:
        settings = load_settings()
        return settings.db_path
    except Exception:
        # Fallback to default path
        base = Path.home() / ".local" / "share" / "jarvis"
        return str(base / "jarvis.db")


def get_db() -> sqlite3.Connection:
    """Get or create database connection."""
    global _db_conn
    if _db_conn is None:
        db_path = _get_db_path()
        _db_conn = sqlite3.connect(db_path, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
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
    """
    import requests as _rq

    payload = request.get_json(silent=True) or {}
    base = str(payload.get("llm_base_url", "") or "").strip().rstrip("/")
    key = str(payload.get("llm_api_key", "") or "").strip()
    if not key:
        from jarvis.config import _load_json
        key = (_load_json(_config_path()).get("llm_api_key") or "").strip()
    if not base:
        return jsonify({"error": "base URL is required"}), 400

    try:
        resp = _rq.get(f"{base}/models",
                       headers={"Authorization": f"Bearer {key}"} if key else {},
                       timeout=20)
        if not resp.ok:
            return jsonify({"error": f"endpoint returned HTTP {resp.status_code}"}), 502
        data = resp.json()
        models = [m.get("id", "") for m in (data.get("data") or [])]
        return jsonify({"ok": True, "count": len(models), "models": models[:60]})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 502


@app.route("/api/chat/reset", methods=["POST"])
def chat_reset() -> Response:
    """Start a fresh conversation, keeping what was said for the diary."""
    with _chat_lock:
        memory = _get_chat_memory()
        memory.start_new_conversation()
    return jsonify({"ok": True})


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

    page = Response(_INDEX_HTML, mimetype="text/html")
    page.set_cookie(
        _TOKEN_COOKIE,
        _SESSION_TOKEN,
        httponly=True,
        samesite="Strict",
        secure=False,  # loopback HTTP; the token never leaves this machine
    )
    return page


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🧠 Jarvis Memory</title>
    <!-- No webfonts, no CDN, no preconnect. This page renders the user's
         diary and personal facts; a font request would hand a third party
         their IP, User-Agent and the moment they opened it, and would fail
         outright on an offline machine. The stacks below prefer the design's
         faces when installed locally and fall back to system faces that
         exist on Windows, macOS and Linux. -->
    <style>
        :root {
            --font-ui: 'Outfit', -apple-system, '.AppleSystemUIFont', 'Segoe UI',
                       'Inter', 'Ubuntu', 'Cantarell', 'Noto Sans', sans-serif;
            --font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Mono',
                         'Consolas', 'DejaVu Sans Mono', 'Liberation Mono', monospace;
            /* HUD theme: deep navy ground, cyan instrumentation. Kept as
               tokens so the whole surface re-skins from this one block. */
            --bg-primary: #040a14;
            --bg-secondary: #071324;
            --bg-tertiary: #0a1b30;
            --bg-card: #08182b;
            --bg-hover: #0d2440;

            --accent-primary: #22d3ee;
            --accent-secondary: #67e8f9;
            --accent-glow: rgba(34, 211, 238, 0.15);
            --accent-muted: #0e5f74;

            --text-primary: #e8f7fc;
            --text-secondary: #8fb6c8;
            --text-muted: #5c7f93;

            --border-color: #10395a;
            --border-glow: rgba(34, 211, 238, 0.32);

            --success: #22c55e;
            --warning: #f59e0b;
            --error: #ef4444;

            --radius-sm: 6px;
            --radius-md: 10px;
            --radius-lg: 16px;
            --radius-xl: 24px;

            --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3);
            --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.4);
            --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.5);
            --shadow-glow: 0 0 40px var(--accent-glow);
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: var(--font-ui);
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.6;
        }

        /* Animated background */
        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background:
                radial-gradient(ellipse 80% 50% at 50% -20%, rgba(245, 158, 11, 0.08), transparent),
                radial-gradient(ellipse 60% 40% at 100% 100%, rgba(139, 92, 246, 0.05), transparent);
            pointer-events: none;
            z-index: -1;
        }

        /* Header */
        .header {
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(10, 11, 15, 0.85);
            backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border-color);
            padding: 1rem 2rem;
        }

        .header-content {
            max-width: 1400px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 2rem;
        }

        .logo {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .logo-icon {
            font-size: 1.75rem;
            animation: pulse-glow 3s ease-in-out infinite;
        }

        @keyframes pulse-glow {
            0%, 100% { filter: drop-shadow(0 0 8px var(--accent-glow)); }
            50% { filter: drop-shadow(0 0 16px var(--accent-primary)); }
        }

        .logo h1 {
            font-size: 1.5rem;
            font-weight: 600;
            background: linear-gradient(135deg, var(--text-primary), var(--accent-secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        /* Search bar */
        .search-container {
            flex: 1;
            max-width: 500px;
        }

        .search-wrapper {
            position: relative;
        }

        .search-input {
            width: 100%;
            padding: 0.75rem 1rem 0.75rem 3rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            color: var(--text-primary);
            font-family: inherit;
            font-size: 0.95rem;
            transition: all 0.2s ease;
        }

        .search-input::placeholder {
            color: var(--text-muted);
        }

        .search-input:focus {
            outline: none;
            border-color: var(--accent-primary);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }

        .search-icon {
            position: absolute;
            left: 1rem;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-muted);
            font-size: 1.1rem;
        }

        /* Stats badges */
        .stats-badges {
            display: flex;
            gap: 1rem;
        }

        .stat-badge {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        .stat-badge .value {
            font-family: var(--font-mono);
            font-weight: 600;
            color: var(--accent-secondary);
        }

        /* Main layout */
        .main-container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        .tab-content {
            flex: 1;
            min-height: 0;
        }

        /* ── HUD workspace ────────────────────────────────────────── */
        .hud {
            position: relative;
            display: grid;
            grid-template-columns: 290px minmax(0, 1fr) 400px;
            gap: 18px;
            height: calc(100vh - 210px);
            min-height: 560px;
        }
        .hud::before {
            content: ''; position: absolute; inset: 0; pointer-events: none;
            background: repeating-linear-gradient(
                to bottom, rgba(34,211,238,0.030) 0 1px, transparent 1px 3px);
            opacity: 0.5; z-index: 0;
        }
        .hud > * { position: relative; z-index: 1; }
        .hud-rail { display: flex; flex-direction: column; gap: 16px; overflow-y: auto; }
        .hud-panel {
            background: linear-gradient(160deg, rgba(13,36,64,0.85), rgba(8,24,43,0.9));
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px 18px;
            box-shadow: inset 0 0 24px rgba(34,211,238,0.05);
        }
        .hud-panel { position: relative; }
        .hud-panel::before, .hud-panel::after {
            content: ''; position: absolute; width: 14px; height: 14px;
            border-color: var(--accent-primary); opacity: 0.65;
        }
        .hud-panel::before {
            top: -1px; left: -1px;
            border-top: 2px solid; border-left: 2px solid;
            border-top-left-radius: 12px;
        }
        .hud-panel::after {
            bottom: -1px; right: -1px;
            border-bottom: 2px solid; border-right: 2px solid;
            border-bottom-right-radius: 12px;
        }
        .hud-panel h3 {
            font-family: var(--font-mono);
            font-size: 11px;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: var(--accent-secondary);
            margin-bottom: 14px;
        }
        .hud-readout { font-family: var(--font-mono); font-size: 13px; }
        .hud-readout.muted { color: var(--text-secondary); line-height: 1.6; }
        .hud-row { display: flex; justify-content: space-between; margin-bottom: 6px; }
        .hud-row span { color: var(--text-secondary); }
        .hud-row b { color: var(--text-primary); font-weight: 600; }
        .hud-row b.ok { color: var(--accent-primary); }
        .hud-bar {
            height: 4px; border-radius: 2px; margin-bottom: 12px;
            background: rgba(34,211,238,0.12); overflow: hidden;
        }
        .hud-bar i {
            display: block; height: 100%; width: 0%;
            background: var(--accent-primary);
            box-shadow: 0 0 10px var(--accent-primary);
            transition: width 500ms ease;
        }
        .hud-core {
            display: flex; flex-direction: column;
            align-items: center; justify-content: center; gap: 26px;
            min-width: 0;
        }
        .hud-core .orb-stage { height: auto; flex: 0 1 auto; }
        .hud-core .orb-stage canvas { height: auto; max-height: 52vh; }
        .hud-activate {
            font-family: var(--font-mono);
            font-size: 15px; letter-spacing: 0.24em; text-transform: uppercase;
            padding: 13px 46px; border-radius: 8px; cursor: pointer;
            color: #04121c; font-weight: 700;
            background: var(--accent-primary);
            border: 1px solid var(--accent-secondary);
            box-shadow: 0 0 26px rgba(34,211,238,0.55);
        }
        .hud-activate:hover { filter: brightness(1.15); }
        .hud-activate.listening {
            background: transparent; color: var(--accent-secondary);
            box-shadow: 0 0 34px rgba(34,211,238,0.8);
        }
        .hud-conversation { display: flex; flex-direction: column; min-height: 0; padding-bottom: 0; }
        .hud-conversation .chat-log { padding: 0 0 14px; }
        .hud-conversation .chat-composer {
            margin: 0 -18px; border-radius: 0 0 12px 12px;
        }
        .hud-conversation .chat-bubble { max-width: 92%; font-size: 14px; }

        @media (max-width: 1250px) {
            .hud { grid-template-columns: 1fr; height: auto; }
            .hud-core { min-height: 380px; }
        }

        /* ── Chat ─────────────────────────────────────────────────── */
        /* The orb is the assistant's presence: a hero while the transcript
           is empty, stepping back to a header band once there is text to
           read. Height drives the size so the layout reflows properly —
           a transform would scale the pixels while leaving the old box. */
        .orb-stage {
            position: relative;
            width: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
            height: 420px;
            transition: height 420ms ease;
            background: radial-gradient(ellipse at 50% 45%,
                        rgba(245, 158, 11, 0.07), transparent 62%);
        }
        .orb-stage canvas {
            height: 78%;
            width: auto;
            max-width: 100%;
        }
        .orb-state {
            font-family: var(--font-mono);
            font-size: 11px;
            letter-spacing: 0.30em;
            text-transform: uppercase;
            color: var(--accent-secondary);
            padding: 6px 18px;
            border: 1px solid var(--border-color);
            border-radius: 999px;
            background: rgba(8,24,43,0.75);
            text-shadow: 0 0 12px rgba(34,211,238,0.7);
        }
        .chat-shell.talking .orb-stage { height: 132px; }
        .orb-stage canvas { height: 100%; }
        .chat-shell.talking .orb-state { font-size: 10px; padding-top: 6px; }

        /* ── Settings ─────────────────────────────────────────────── */
        .settings-grid {
            display: grid;
            grid-template-columns: 180px minmax(0, 520px);
            gap: 12px 18px;
            align-items: center;
            margin-bottom: 22px;
        }
        .settings-grid label {
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .settings-grid input, .settings-grid select {
            padding: 11px 14px;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            background: var(--bg-primary);
            color: var(--text-primary);
            font-family: var(--font-mono);
            font-size: 13px;
        }
        .settings-grid input:focus, .settings-grid select:focus {
            outline: none; border-color: var(--accent-primary);
        }
        .settings-actions { display: flex; align-items: center; gap: 12px; }
        .settings-actions button {
            padding: 11px 26px; border-radius: 10px; cursor: pointer;
            font-family: var(--font-ui); font-weight: 600; font-size: 14px;
            border: 1px solid var(--border-color);
        }
        .settings-actions .btn-primary {
            background: var(--accent-primary); border-color: var(--accent-primary); color: #04121c;
        }
        .settings-actions .btn-ghost { background: transparent; color: var(--text-secondary); }
        .settings-status { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }
        .settings-status.ok { color: var(--success); }
        .settings-status.bad { color: var(--error); }

        /* ── Connections ──────────────────────────────────────────── */
        .conn-intro { margin-bottom: 22px; max-width: 720px; }
        .conn-intro h2 { font-size: 20px; margin-bottom: 8px; }
        .conn-intro p { color: var(--text-secondary); line-height: 1.6; }
        .conn-intro code {
            font-family: var(--font-mono);
            font-size: 13px;
            color: var(--accent-secondary);
        }
        .conn-add {
            display: grid;
            grid-template-columns: 1fr 1fr 2fr auto;
            gap: 10px;
            margin-bottom: 24px;
        }
        .conn-add input {
            padding: 11px 14px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--bg-primary);
            color: var(--text-primary);
            font-family: var(--font-ui);
            font-size: 14px;
        }
        .conn-add input:focus { outline: none; border-color: var(--accent-primary); }
        .conn-add button {
            padding: 0 22px;
            border-radius: 10px;
            border: 1px solid var(--accent-primary);
            background: var(--accent-primary);
            color: #14110a;
            font-weight: 600;
            cursor: pointer;
        }
        .conn-list { display: flex; flex-direction: column; gap: 12px; }
        .conn-card {
            display: flex;
            align-items: center;
            gap: 16px;
            padding: 16px 18px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
        }
        .conn-card .conn-name { font-weight: 600; font-size: 15px; }
        .conn-card .conn-cmd {
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 4px;
            word-break: break-all;
        }
        .conn-card .conn-meta { flex: 1; min-width: 0; }
        .conn-pill {
            font-family: var(--font-mono);
            font-size: 11px;
            padding: 5px 11px;
            border-radius: 999px;
            white-space: nowrap;
        }
        .conn-pill.ok { background: rgba(34,197,94,0.14); color: var(--success-light); }
        .conn-pill.bad { background: rgba(239,68,68,0.14); color: var(--error-light); }
        .conn-remove {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            border-radius: 8px;
            padding: 7px 13px;
            cursor: pointer;
        }
        .conn-remove:hover { border-color: var(--error); color: var(--error-light); }
        .chat-shell {
            display: flex;
            flex-direction: column;
            height: calc(100vh - 230px);
            min-height: 380px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 14px;
            overflow: hidden;
        }
        .chat-log {
            flex: 1;
            overflow-y: auto;
            padding: 24px;
            display: flex;
            flex-direction: column;
            gap: 14px;
        }
        .chat-msg { display: flex; }
        .chat-msg.user { justify-content: flex-end; }
        .chat-bubble {
            max-width: min(72%, 620px);
            padding: 12px 16px;
            border-radius: 14px;
            line-height: 1.55;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .chat-msg.user .chat-bubble {
            background: var(--accent-primary);
            color: #14110a;
            border-bottom-right-radius: 4px;
        }
        .chat-msg.assistant .chat-bubble {
            background: var(--bg-hover);
            color: var(--text-primary);
            border: 1px solid var(--border);
            border-bottom-left-radius: 4px;
        }
        .chat-msg.pending .chat-bubble {
            color: var(--text-muted);
            font-style: italic;
        }
        .chat-msg.error .chat-bubble {
            border-color: var(--error);
            color: var(--error-light);
        }
        .chat-composer {
            display: flex;
            gap: 10px;
            padding: 14px;
            border-top: 1px solid var(--border);
            background: var(--bg-secondary);
        }
        .chat-composer textarea {
            flex: 1;
            resize: none;
            padding: 12px 14px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--bg-primary);
            color: var(--text-primary);
            font-family: var(--font-ui);
            font-size: 15px;
            line-height: 1.5;
        }
        .chat-composer textarea:focus {
            outline: none;
            border-color: var(--accent-primary);
        }
        .chat-composer button {
            padding: 0 20px;
            border-radius: 10px;
            border: 1px solid var(--border);
            cursor: pointer;
            font-family: var(--font-ui);
            font-weight: 600;
            font-size: 14px;
        }
        .chat-composer .btn-primary {
            background: var(--accent-primary);
            border-color: var(--accent-primary);
            color: #14110a;
        }
        .chat-composer .btn-ghost {
            background: transparent;
            color: var(--text-secondary);
        }
        .chat-composer button:hover { filter: brightness(1.1); }

        .tab-pane.with-sidebar {
            display: grid;
            grid-template-columns: 280px 1fr;
            gap: 2rem;
        }

        .tab-pane.with-sidebar[style*="display: none"] {
            display: none !important;
        }

        /* Sidebar */
        .sidebar {
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }

        .sidebar-section {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            padding: 1.25rem;
        }

        .sidebar-title {
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--text-muted);
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        /* Diary maintenance button (sidebar Clean Up). Same visual weight
           as the other sidebar widgets so it doesn't read as a destructive
           action — the modal is what conveys what it actually does. */
        .diary-maintenance-btn {
            width: 100%;
            padding: 0.6rem 0.85rem;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            color: var(--text-primary);
            font-family: inherit;
            font-size: 0.85rem;
            cursor: pointer;
            transition: background 0.15s ease, border-color 0.15s ease;
        }
        .diary-maintenance-btn:hover {
            background: var(--bg-hover);
            border-color: var(--accent-primary);
        }
        .diary-maintenance-btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
        }
        .diary-maintenance-btn + .diary-maintenance-btn {
            margin-top: 0.5rem;
        }

        /* Date filters */
        .date-filters {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .date-input {
            padding: 0.6rem 0.85rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            color: var(--text-primary);
            font-family: var(--font-mono);
            font-size: 0.85rem;
        }

        .date-input:focus {
            outline: none;
            border-color: var(--accent-primary);
        }

        /* Topic tags */
        .topics-cloud {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            max-height: 300px;
            overflow-y: auto;
        }

        .topic-tag {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.35rem 0.75rem;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-xl);
            font-size: 0.8rem;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.15s ease;
        }

        .topic-tag:hover {
            background: var(--bg-hover);
            border-color: var(--accent-primary);
            color: var(--text-primary);
        }

        .topic-tag.active {
            background: var(--accent-glow);
            border-color: var(--accent-primary);
            color: var(--accent-secondary);
        }

        .topic-count {
            font-family: var(--font-mono);
            font-size: 0.7rem;
            color: var(--text-muted);
        }

        /* Memory list */
        .memory-list {
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }

        .memory-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            padding: 1.5rem;
            transition: all 0.2s ease;
            position: relative;
            overflow: hidden;
        }

        .memory-card::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 3px;
            background: linear-gradient(180deg, var(--accent-primary), var(--accent-muted));
            opacity: 0;
            transition: opacity 0.2s ease;
        }

        .memory-card:hover {
            border-color: var(--border-glow);
            transform: translateX(4px);
            box-shadow: var(--shadow-md);
        }

        .memory-card:hover::before {
            opacity: 1;
        }

        .memory-header {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            margin-bottom: 0.75rem;
        }

        .memory-date {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-family: var(--font-mono);
            font-size: 0.85rem;
            color: var(--accent-secondary);
        }

        .memory-actions {
            display: flex;
            gap: 0.5rem;
            opacity: 0;
            transition: opacity 0.15s ease;
        }

        .memory-card:hover .memory-actions {
            opacity: 1;
        }

        .action-btn {
            padding: 0.4rem 0.6rem;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            color: var(--text-muted);
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.15s ease;
        }

        .action-btn:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }

        .action-btn.delete:hover {
            background: rgba(239, 68, 68, 0.15);
            border-color: var(--error);
            color: var(--error);
        }

        .memory-summary {
            font-size: 0.95rem;
            line-height: 1.7;
            color: var(--text-secondary);
            margin-bottom: 1rem;
        }

        .memory-topics {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
        }

        .memory-topic {
            padding: 0.25rem 0.6rem;
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: var(--font-mono);
        }

        /* Empty state */
        .empty-state {
            text-align: center;
            padding: 4rem 2rem;
            color: var(--text-muted);
        }

        .empty-icon {
            font-size: 4rem;
            margin-bottom: 1rem;
            opacity: 0.5;
        }

        .empty-title {
            font-size: 1.25rem;
            font-weight: 500;
            color: var(--text-secondary);
            margin-bottom: 0.5rem;
        }

        /* Loading */
        .loading {
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 3rem;
        }

        .spinner {
            width: 40px;
            height: 40px;
            border: 3px solid var(--bg-tertiary);
            border-top-color: var(--accent-primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Inline filters (for meals date range) */
        .inline-filters {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 1.5rem;
        }

        .inline-filter-label {
            font-size: 0.85rem;
        }

        .inline-filter-sep {
            color: var(--text-muted);
            font-size: 0.85rem;
        }

        /* Tabs */
        .tabs {
            display: flex;
            gap: 0.5rem;
        }

        .tab {
            padding: 0.6rem 1.25rem;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            color: var(--text-secondary);
            font-size: 0.9rem;
            cursor: pointer;
            transition: all 0.15s ease;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .tab:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }

        .tab.active {
            background: var(--accent-glow);
            border-color: var(--accent-primary);
            color: var(--accent-secondary);
        }

        /* Meal cards */
        .meal-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            padding: 1.25rem;
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 1rem;
            align-items: center;
            transition: all 0.2s ease;
        }

        .meal-card:hover {
            border-color: var(--border-glow);
        }

        .meal-header {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 0.75rem;
        }

        .meal-info h3 {
            font-size: 1rem;
            font-weight: 500;
            margin-bottom: 0.25rem;
        }

        .meal-delete {
            opacity: 0;
            transition: opacity 0.15s ease;
            flex-shrink: 0;
        }

        .meal-card:hover .meal-delete {
            opacity: 1;
        }

        .meal-time {
            font-family: var(--font-mono);
            font-size: 0.8rem;
            color: var(--text-muted);
        }

        .meal-macros {
            display: flex;
            gap: 0.75rem;
        }

        .macro {
            text-align: center;
            padding: 0.5rem 0.75rem;
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
        }

        .macro-value {
            font-family: var(--font-mono);
            font-weight: 600;
            font-size: 1rem;
            color: var(--accent-secondary);
        }

        .macro-label {
            font-size: 0.7rem;
            color: var(--text-muted);
            text-transform: uppercase;
        }

        /* Responsive */
        @media (max-width: 900px) {
            .tab-pane.with-sidebar {
                grid-template-columns: 1fr;
            }

            .sidebar {
                order: 1;
            }

            .stats-badges {
                display: none;
            }
        }

        @media (max-width: 600px) {
            .header-content {
                flex-direction: column;
                gap: 1rem;
            }

            .search-container {
                max-width: none;
                width: 100%;
            }

            .main-container {
                padding: 1rem;
            }
        }

        /* Toast notifications */
        .toast {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            padding: 1rem 1.5rem;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            box-shadow: var(--shadow-lg);
            display: flex;
            align-items: center;
            gap: 0.75rem;
            animation: slide-in 0.3s ease;
            z-index: 1000;
        }

        .toast.success {
            border-color: var(--success);
        }

        .toast.error {
            border-color: var(--error);
        }

        @keyframes slide-in {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
        }

        /* ─── Graph Explorer ─── */
        .alpha-disclaimer {
            display: flex;
            align-items: flex-start;
            gap: 0.75rem;
            padding: 0.85rem 1rem;
            margin-bottom: 1rem;
            background: var(--accent-glow);
            border: 1px solid var(--border-glow);
            border-radius: var(--radius-md);
            color: var(--text-secondary);
            font-size: 0.85rem;
            line-height: 1.5;
        }

        .alpha-disclaimer .alpha-body {
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
        }

        .alpha-disclaimer .alpha-body p {
            margin: 0;
        }

        .alpha-disclaimer code {
            padding: 0.05rem 0.35rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            font-family: var(--font-mono);
            font-size: 0.8rem;
            color: var(--accent-secondary);
        }

        .alpha-disclaimer .alpha-badge {
            flex-shrink: 0;
            padding: 0.15rem 0.5rem;
            background: var(--warning);
            color: var(--bg-primary);
            border-radius: var(--radius-sm);
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        .graph-explorer {
            display: grid;
            grid-template-columns: 240px 1fr 320px;
            gap: 0;
            height: calc(100vh - 340px);
            min-height: 500px;
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            overflow: hidden;
            background: var(--bg-card);
        }

        .graph-tree-sidebar {
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .tree-header {
            padding: 0.75rem 1rem;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-shrink: 0;
        }

        .tree-container {
            flex: 1;
            overflow-y: auto;
            padding: 0.5rem 0;
        }

        .tree-node {
            display: flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.35rem 0.75rem;
            cursor: pointer;
            font-size: 0.85rem;
            color: var(--text-secondary);
            transition: all 0.1s ease;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .tree-node:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }

        .tree-node.selected {
            background: var(--accent-glow);
            color: var(--accent-secondary);
        }

        .tree-toggle {
            width: 16px;
            height: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.6rem;
            color: var(--text-muted);
            flex-shrink: 0;
            transition: transform 0.15s ease;
        }

        .tree-toggle.expanded {
            transform: rotate(90deg);
        }

        .tree-toggle.leaf {
            visibility: hidden;
        }

        .tree-children {
            padding-left: 1rem;
        }

        .tree-children.collapsed {
            display: none;
        }

        .tree-node-name {
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .tree-node-count {
            font-family: var(--font-mono);
            font-size: 0.65rem;
            color: var(--text-muted);
            margin-left: auto;
            flex-shrink: 0;
            padding: 0 0.35rem;
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
        }

        /* Graph canvas */
        .graph-canvas-container {
            position: relative;
            overflow: hidden;
            background: var(--bg-primary);
            background-image:
                radial-gradient(circle, var(--border-color) 1px, transparent 1px);
            background-size: 30px 30px;
        }

        #graph-canvas {
            width: 100%;
            height: 100%;
            display: block;
            cursor: grab;
        }

        #graph-canvas:active {
            cursor: grabbing;
        }

        .graph-toolbar {
            position: absolute;
            top: 0.75rem;
            left: 0.75rem;
            display: flex;
            gap: 0.35rem;
            z-index: 10;
        }

        .graph-btn {
            width: 32px;
            height: 32px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 0.8rem;
            transition: all 0.1s ease;
        }

        .graph-btn:hover {
            background: var(--bg-hover);
            border-color: var(--accent-primary);
            color: var(--text-primary);
        }

        /* Detail sidebar */
        .graph-detail-sidebar {
            border-left: 1px solid var(--border-color);
            overflow-y: auto;
            padding: 1rem;
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }

        .detail-empty {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: var(--text-muted);
            text-align: center;
            gap: 0.5rem;
        }

        .detail-empty .empty-icon {
            font-size: 2.5rem;
            opacity: 0.4;
        }

        .detail-breadcrumb {
            display: flex;
            flex-wrap: wrap;
            gap: 0.25rem;
            font-size: 0.75rem;
            color: var(--text-muted);
        }

        .detail-breadcrumb span {
            cursor: pointer;
            transition: color 0.1s;
        }

        .detail-breadcrumb span:hover {
            color: var(--accent-secondary);
        }

        .detail-breadcrumb .sep {
            cursor: default;
        }

        .detail-breadcrumb .sep:hover {
            color: var(--text-muted);
        }

        .detail-name {
            font-size: 1.2rem;
            font-weight: 600;
            color: var(--text-primary);
        }

        .detail-description {
            font-size: 0.9rem;
            color: var(--text-secondary);
            line-height: 1.6;
        }

        .detail-section {
            margin-top: 0.5rem;
        }

        .detail-section-title {
            font-size: 0.7rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }

        .detail-data {
            font-family: var(--font-mono);
            font-size: 0.8rem;
            line-height: 1.7;
            color: var(--text-secondary);
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            padding: 0.75rem;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 300px;
            overflow-y: auto;
        }

        .detail-meta {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 0.5rem;
        }

        .detail-meta-item {
            background: var(--bg-secondary);
            border-radius: var(--radius-sm);
            padding: 0.5rem 0.65rem;
        }

        .detail-meta-label {
            font-size: 0.65rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .detail-meta-value {
            font-family: var(--font-mono);
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--accent-secondary);
        }

        .detail-children-list {
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }

        .detail-child {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 0.65rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            cursor: pointer;
            transition: all 0.1s ease;
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        .detail-child:hover {
            background: var(--bg-hover);
            border-color: var(--accent-primary);
            color: var(--text-primary);
        }

        .detail-child-name {
            font-weight: 500;
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .detail-actions {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
        }

        .detail-action-btn {
            flex: 1;
            min-width: 80px;
            padding: 0.5rem 0.75rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 0.8rem;
            text-align: center;
            transition: all 0.1s ease;
        }

        .detail-action-btn:hover {
            background: var(--bg-hover);
            border-color: var(--accent-primary);
            color: var(--text-primary);
        }

        .detail-action-btn.delete:hover {
            background: rgba(239, 68, 68, 0.15);
            border-color: var(--error);
            color: var(--error);
        }

        /* Edit form in detail sidebar */
        .detail-edit-field {
            width: 100%;
            padding: 0.5rem 0.65rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            color: var(--text-primary);
            font-family: inherit;
            font-size: 0.85rem;
            resize: vertical;
        }

        .detail-edit-field:focus {
            outline: none;
            border-color: var(--accent-primary);
            box-shadow: 0 0 0 2px var(--accent-glow);
        }

        textarea.detail-edit-field {
            font-family: var(--font-mono);
            font-size: 0.8rem;
            min-height: 80px;
        }

        /* Node creation modal */
        .modal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(4px);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }

        .modal {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-lg);
            padding: 1.5rem;
            width: 400px;
            max-width: 90vw;
            box-shadow: var(--shadow-lg);
        }

        .modal h3 {
            font-size: 1.1rem;
            margin-bottom: 1rem;
            color: var(--text-primary);
        }

        .modal-field {
            margin-bottom: 0.75rem;
        }

        .modal-field label {
            display: block;
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-bottom: 0.25rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .modal-actions {
            display: flex;
            gap: 0.5rem;
            justify-content: flex-end;
            margin-top: 1rem;
        }

        .modal-btn {
            padding: 0.5rem 1.25rem;
            border: 1px solid var(--border-color);
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.1s ease;
        }

        .modal-btn.primary {
            background: var(--accent-primary);
            border-color: var(--accent-primary);
            color: var(--bg-primary);
            font-weight: 600;
        }

        .modal-btn.primary:hover {
            background: var(--accent-secondary);
        }

        .modal-btn.secondary {
            background: var(--bg-secondary);
            color: var(--text-secondary);
        }

        .modal-btn.secondary:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }

        /* Responsive graph */
        @media (max-width: 1100px) {
            .graph-explorer {
                grid-template-columns: 200px 1fr 260px;
            }
        }

        @media (max-width: 800px) {
            .graph-explorer {
                grid-template-columns: 1fr;
                grid-template-rows: 200px 1fr;
                height: calc(100vh - 340px);
            }
            .graph-tree-sidebar {
                border-right: none;
                border-bottom: 1px solid var(--border-color);
            }
            .graph-detail-sidebar {
                display: none;
            }
        }

        /* Scrollbar */
        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }

        ::-webkit-scrollbar-track {
            background: var(--bg-secondary);
        }

        ::-webkit-scrollbar-thumb {
            background: var(--border-color);
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: var(--text-muted);
        }
    </style>
</head>
<body>
    <header class="header">
        <div class="header-content">
            <div class="logo">
                <span class="logo-icon">🧠</span>
                <h1>Jarvis Memory</h1>
            </div>

            <div class="search-container">
                <div class="search-wrapper">
                    <span class="search-icon">🔍</span>
                    <input type="text" class="search-input" id="search-input" placeholder="Search memories..." />
                </div>
            </div>

            <div class="stats-badges">
                <div class="stat-badge">
                    <span>📝</span>
                    <span class="value" id="stats-memories">-</span>
                    <span>diary</span>
                </div>
                <div class="stat-badge">
                    <span>🧠</span>
                    <span class="value" id="stats-nodes">-</span>
                    <span>nodes</span>
                </div>
                <div class="stat-badge">
                    <span>🍽️</span>
                    <span class="value" id="stats-meals">-</span>
                    <span>meals</span>
                </div>
            </div>
        </div>
    </header>

    <main class="main-container">
        <div class="tabs">
            <button class="tab active" data-tab="chat">
                <span>💬</span> Chat
            </button>
            <button class="tab" data-tab="memories">
                <span>💭</span> Diary
            </button>
            <button class="tab" data-tab="graph">
                <span>🧠</span> Knowledge
            </button>
            <button class="tab" data-tab="meals">
                <span>🍽️</span> Meals
            </button>
            <button class="tab" data-tab="connections">
                <span>🔌</span> Connections
            </button>
            <button class="tab" data-tab="settings">
                <span>🔑</span> Settings
            </button>
        </div>

        <div class="tab-content">
            <div id="chat-content" class="tab-pane active">
                <div class="hud">
                    <aside class="hud-rail">
                        <section class="hud-panel">
                            <h3>◈ System Stats</h3>
                            <div id="sys-stats" class="hud-readout">
                                <div class="hud-row"><span>CPU</span><b id="sys-cpu">—</b></div>
                                <div class="hud-bar"><i id="bar-cpu"></i></div>
                                <div class="hud-row"><span>Memory</span><b id="sys-mem">—</b></div>
                                <div class="hud-bar"><i id="bar-mem"></i></div>
                                <div class="hud-row"><span>Disk</span><b id="sys-disk">—</b></div>
                                <div class="hud-bar"><i id="bar-disk"></i></div>
                            </div>
                        </section>

                        <section class="hud-panel">
                            <h3>◈ Weather</h3>
                            <div id="weather-body" class="hud-readout muted">Loading…</div>
                        </section>

                        <section class="hud-panel">
                            <h3>◈ System</h3>
                            <div class="hud-readout">
                                <div class="hud-row"><span>Provider</span><b id="sys-provider">—</b></div>
                                <div class="hud-row"><span>Model</span><b id="sys-model">—</b></div>
                                <div class="hud-row"><span>Status</span><b id="sys-state" class="ok">IDLE</b></div>
                            </div>
                        </section>
                    </aside>

                    <div class="hud-core">
                        <div class="orb-stage">
                            <canvas id="orb" width="640" height="640"></canvas>
                            <div class="orb-state" id="orb-state">idle</div>
                        </div>
                        <button id="activate-btn" class="hud-activate">Activate</button>
                    </div>

                    <section class="hud-panel hud-conversation">
                        <h3>◈ Conversation</h3>
                        <div class="chat-log" id="chat-log">
                            <div class="empty-state">
                                <div class="empty-icon">💬</div>
                                <div class="empty-title">Talk to Jarvis</div>
                                <p>Same assistant as the voice and terminal front ends.</p>
                            </div>
                        </div>
                        <div class="chat-composer">
                            <textarea id="chat-input" rows="1" placeholder="Ask Jarvis something…"></textarea>
                            <button class="btn-primary" id="chat-send">Send</button>
                            <button class="btn-ghost" id="chat-reset" title="Save and start fresh">New</button>
                        </div>
                    </section>
                </div>
            </div>

            <div id="memories-content" class="tab-pane with-sidebar" style="display: none;">
                <aside class="sidebar" id="diary-sidebar">
                    <div class="sidebar-section" id="date-filter-section">
                        <div class="sidebar-title">📅 Date Range</div>
                        <div class="date-filters">
                            <input type="date" class="date-input" id="from-date" placeholder="From" />
                            <input type="date" class="date-input" id="to-date" placeholder="To" />
                        </div>
                    </div>

                    <div class="sidebar-section" id="topics-filter-section">
                        <div class="sidebar-title">🏷️ Topics</div>
                        <div class="topics-cloud" id="topics-cloud">
                            <div class="loading"><div class="spinner"></div></div>
                        </div>
                    </div>

                    <div class="sidebar-section" id="diary-maintenance-section">
                        <div class="sidebar-title">🧹 Maintenance</div>
                        <button class="diary-maintenance-btn" id="btn-scrub-deflections" title="Ask the chat model to remove sentences that narrate assistant failures (e.g. 'the assistant could not…') from old diary entries. The rest of each entry stays verbatim. No entries are deleted. Requires the chat model to be running.">
                            Clean up deflection narration
                        </button>
                        <button class="diary-maintenance-btn" id="btn-optimise-topics" title="Merge near-synonym tags, normalise casing, and split compound tags across all diary entries. Requires the chat model to be running.">
                            Optimise tags
                        </button>
                    </div>
                </aside>
                <div class="memory-list">
                    <div class="loading"><div class="spinner"></div></div>
                </div>
            </div>

            <div id="graph-content" class="tab-pane" style="display: none;">
                <div class="alpha-disclaimer">
                    <span class="alpha-badge">Beta</span>
                    <div class="alpha-body">
                        <p>
                            🧪 The knowledge graph is on by default: a compact <strong>warm profile</strong>
                            (User + Directives branches) is injected into every reply, and query-driven graph
                            recall runs alongside the diary via <code>Enrichment Source = all</code>.
                        </p>
                        <p>
                            👉 Structure and classification are stable; extractor quality is still being tuned.
                            Please share feedback so we can keep refining it.
                        </p>
                    </div>
                </div>
                <div class="graph-explorer">
                    <!-- Left sidebar: tree navigator -->
                    <div class="graph-tree-sidebar">
                        <div class="tree-header">
                            <span>🌳</span> Memory Tree
                        </div>
                        <div class="tree-container" id="tree-container">
                            <div class="loading"><div class="spinner"></div></div>
                        </div>
                    </div>

                    <!-- Centre: graph canvas -->
                    <div class="graph-canvas-container">
                        <div class="graph-toolbar">
                            <button class="graph-btn" id="btn-zoom-in" title="Zoom in">➕</button>
                            <button class="graph-btn" id="btn-zoom-out" title="Zoom out">➖</button>
                            <button class="graph-btn" id="btn-fit" title="Fit to view">📐</button>
                            <button class="graph-btn" id="btn-add-node" title="Add node">✨</button>
                            <button class="graph-btn" id="btn-import-diary" title="Import from Diary">📥</button>
                            <button class="graph-btn" id="btn-consolidate-all" title="Consolidate every populated node (dedupe / merge contradictions / prune common knowledge)">🧹</button>
                        </div>
                        <canvas id="graph-canvas"></canvas>
                    </div>

                    <!-- Right sidebar: node details -->
                    <div class="graph-detail-sidebar" id="detail-sidebar">
                        <div class="detail-empty">
                            <div class="empty-icon">🧠</div>
                            <p>Select a node to view its details</p>
                        </div>
                    </div>
                </div>
            </div>

            <div id="meals-content" class="tab-pane" style="display: none;">
                <div class="inline-filters">
                    <span class="inline-filter-label">📅</span>
                    <input type="date" class="date-input" id="meals-from-date" placeholder="From" />
                    <span class="inline-filter-sep">→</span>
                    <input type="date" class="date-input" id="meals-to-date" placeholder="To" />
                </div>
                <div class="memory-list">
                    <div class="loading"><div class="spinner"></div></div>
                </div>
            </div>

            <div id="settings-content" class="tab-pane" style="display: none;">
                <div class="conn-intro">
                    <h2>🔑 AI Provider</h2>
                    <p>Point Jarvis at any OpenAI-compatible endpoint and supply your
                       own key. Works with Gemini, OpenAI, Groq, Together, OpenRouter,
                       or anything you self-host (Ollama, LM Studio, llama.cpp).
                       Saved to <code>config.json</code> and applied on your next message.</p>
                </div>

                <div class="settings-grid">
                    <label>Provider</label>
                    <select id="set-provider">
                        <option value="openai_compatible">openai_compatible</option>
                        <option value="ollama">ollama</option>
                    </select>

                    <label>Base URL</label>
                    <input id="set-baseurl" placeholder="https://generativelanguage.googleapis.com/v1beta/openai" />

                    <label>API key</label>
                    <input id="set-key" type="password" placeholder="leave blank to keep the current key" />

                    <label>Chat model</label>
                    <input id="set-chat" placeholder="gemini-3.1-flash-lite" />

                    <label>Fast model</label>
                    <input id="set-fast" placeholder="used for planning and routing" />

                    <label>Embedding model</label>
                    <input id="set-embed" placeholder="gemini-embedding-001" />
                </div>

                <div class="settings-actions">
                    <button class="btn-primary" id="set-save">Save</button>
                    <button class="btn-ghost" id="set-test">Test connection</button>
                    <span id="set-status" class="settings-status"></span>
                </div>
            </div>

            <div id="connections-content" class="tab-pane" style="display: none;">
                <div class="conn-intro">
                    <h2>🔌 Connections</h2>
                    <p>MCP servers give Jarvis new tools — Home Assistant, GitHub,
                       Slack, your filesystem, a database. Anything added here is
                       written to <code id="conn-config-path">config.json</code> and
                       picked up on the next conversation.</p>
                </div>

                <div class="conn-add">
                    <input id="conn-name" placeholder="Name (e.g. github)" />
                    <input id="conn-command" placeholder="Command (e.g. npx)" />
                    <input id="conn-args" placeholder="Arguments (e.g. -y @modelcontextprotocol/server-github)" />
                    <button class="btn-primary" id="conn-add-btn">Add</button>
                </div>

                <div id="conn-list" class="conn-list">
                    <div class="loading"><div class="spinner"></div></div>
                </div>
            </div>
        </div>
    </main>

    <script>
        // State
        let currentTab = 'chat';
        let selectedTopics = new Set();
        let searchQuery = '';
        let diaryImportDone = false;
        let fromDate = '';
        let toDate = '';
        let searchDebounce = null;

        // Non-deletable preset node IDs. Loaded from /api/graph/presets on
        // boot so the JS side never drifts from FIXED_BRANCHES in graph.py.
        // Seeded with 'root' so the delete button stays hidden if the fetch
        // hasn't completed yet (fail-closed).
        let PRESET_NODE_IDS = new Set(['root']);

        // DOM Elements
        const searchInput = document.getElementById('search-input');
        const fromDateInput = document.getElementById('from-date');
        const toDateInput = document.getElementById('to-date');
        const mealsFromDateInput = document.getElementById('meals-from-date');
        const mealsToDateInput = document.getElementById('meals-to-date');
        const topicsCloud = document.getElementById('topics-cloud');
        const connPane = document.getElementById('connections-content');
        const settingsPane = document.getElementById('settings-content');
        const chatPane = document.getElementById('chat-content');
        const chatLog = document.getElementById('chat-log');
        const chatInput = document.getElementById('chat-input');
        const memoriesPane = document.getElementById('memories-content');
        const mealsPane = document.getElementById('meals-content');
        const graphContent = document.getElementById('graph-content');
        const memoriesContent = memoriesPane.querySelector('.memory-list');
        const mealsContent = mealsPane.querySelector('.memory-list');
        const tabs = document.querySelectorAll('.tab');

        // Shared utilities
        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
        }

        // API calls
        async function fetchMemories() {
            const params = new URLSearchParams();
            if (searchQuery) params.set('search', searchQuery);
            if (selectedTopics.size > 0) params.set('topic', Array.from(selectedTopics).join(','));
            if (fromDate) params.set('from_date', fromDate);
            if (toDate) params.set('to_date', toDate);

            const response = await fetch('/api/memories?' + params);
            return response.json();
        }

        async function fetchTopics() {
            const response = await fetch('/api/topics');
            return response.json();
        }

        async function fetchMeals() {
            const params = new URLSearchParams();
            if (fromDate) params.set('from_date', fromDate);
            if (toDate) params.set('to_date', toDate);

            const response = await fetch('/api/meals?' + params);
            return response.json();
        }

        async function fetchStats() {
            const response = await fetch('/api/stats');
            return response.json();
        }

        async function deleteMemory(id) {
            const response = await fetch('/api/memory/' + id, { method: 'DELETE' });
            return response.json();
        }

        async function deleteMeal(id) {
            const response = await fetch('/api/meal/' + id, { method: 'DELETE' });
            return response.json();
        }

        // Render functions
        function renderTopics(topics) {
            if (!topics.length) {
                topicsCloud.innerHTML = '<div class="empty-state"><p>No topics yet</p></div>';
                return;
            }

            topicsCloud.innerHTML = topics.map(topic => `
                <button class="topic-tag ${selectedTopics.has(topic.name) ? 'active' : ''}"
                        data-topic="${escapeHtml(topic.name)}">
                    ${escapeHtml(topic.name)}
                    <span class="topic-count">${topic.count}</span>
                </button>
            `).join('');

            // Add click handlers
            topicsCloud.querySelectorAll('.topic-tag').forEach(tag => {
                tag.addEventListener('click', () => {
                    const topic = tag.dataset.topic;
                    if (selectedTopics.has(topic)) {
                        selectedTopics.delete(topic);
                    } else {
                        selectedTopics.add(topic);
                    }
                    renderTopics(topics);
                    loadMemories();
                });
            });
        }

        function formatDate(dateStr) {
            const date = new Date(dateStr + 'T00:00:00');
            const now = new Date();
            const diff = Math.floor((now - date) / (1000 * 60 * 60 * 24));

            if (diff === 0) return 'Today';
            if (diff === 1) return 'Yesterday';
            if (diff < 7) return `${diff} days ago`;

            return date.toLocaleDateString('en-US', {
                weekday: 'short',
                month: 'short',
                day: 'numeric',
                year: date.getFullYear() !== now.getFullYear() ? 'numeric' : undefined
            });
        }

        function renderMemories(memories) {
            if (!memories.length) {
                memoriesContent.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">🌙</div>
                        <div class="empty-title">No memories found</div>
                        <p>Try adjusting your search or filters</p>
                    </div>
                `;
                return;
            }

            memoriesContent.innerHTML = memories.map(memory => `
                <article class="memory-card" data-id="${memory.id}">
                    <div class="memory-header">
                        <div class="memory-date">
                            <span>📅</span>
                            ${formatDate(memory.date_utc)}
                        </div>
                        <div class="memory-actions">
                            <button class="action-btn delete" title="Delete memory">🗑️</button>
                        </div>
                    </div>
                    <p class="memory-summary">${escapeHtml(memory.summary)}</p>
                    ${memory.topics_list.length ? `
                        <div class="memory-topics">
                            ${memory.topics_list.map(t => `<span class="memory-topic">${escapeHtml(t)}</span>`).join('')}
                        </div>
                    ` : ''}
                </article>
            `).join('');

            // Add delete handlers
            memoriesContent.querySelectorAll('.action-btn.delete').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    const card = e.target.closest('.memory-card');
                    const id = card.dataset.id;

                    if (confirm('Delete this memory?')) {
                        const result = await deleteMemory(id);
                        if (result.success) {
                            card.remove();
                            showToast('Memory deleted', 'success');
                            loadStats();
                        } else {
                            showToast('Failed to delete', 'error');
                        }
                    }
                });
            });
        }

        function renderMeals(meals) {
            if (!meals.length) {
                mealsContent.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">🍽️</div>
                        <div class="empty-title">No meals logged</div>
                        <p>Meal tracking data will appear here</p>
                    </div>
                `;
                return;
            }

            mealsContent.innerHTML = meals.map(meal => `
                <div class="meal-card" data-id="${meal.id}">
                    <div class="meal-info">
                        <div class="meal-header">
                            <h3>${meal.description}</h3>
                            <button class="action-btn delete meal-delete" title="Delete meal">🗑️</button>
                        </div>
                        <div class="meal-time">${new Date(meal.ts_utc).toLocaleString()}</div>
                    </div>
                    <div class="meal-macros">
                        ${meal.calories_kcal ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.calories_kcal)}</div>
                                <div class="macro-label">kcal</div>
                            </div>
                        ` : ''}
                        ${meal.protein_g ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.protein_g)}g</div>
                                <div class="macro-label">protein</div>
                            </div>
                        ` : ''}
                        ${meal.carbs_g ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.carbs_g)}g</div>
                                <div class="macro-label">carbs</div>
                            </div>
                        ` : ''}
                        ${meal.fat_g ? `
                            <div class="macro">
                                <div class="macro-value">${Math.round(meal.fat_g)}g</div>
                                <div class="macro-label">fat</div>
                            </div>
                        ` : ''}
                    </div>
                </div>
            `).join('');

            // Add delete handlers for meals
            mealsContent.querySelectorAll('.meal-delete').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    const card = e.target.closest('.meal-card');
                    const id = card.dataset.id;

                    if (confirm('Delete this meal?')) {
                        const result = await deleteMeal(id);
                        if (result.success) {
                            card.remove();
                            showToast('Meal deleted', 'success');
                            loadStats();
                        } else {
                            showToast('Failed to delete meal', 'error');
                        }
                    }
                });
            });
        }

        function showToast(message, type = 'success') {
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.innerHTML = `
                <span>${type === 'success' ? '✅' : '❌'}</span>
                <span>${message}</span>
            `;
            document.body.appendChild(toast);

            setTimeout(() => toast.remove(), 3000);
        }

        // Load data
        async function loadMemories() {
            memoriesContent.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const { memories } = await fetchMemories();
                renderMemories(memories);
            } catch (e) {
                memoriesContent.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Failed to load memories</div></div>';
            }
        }

        async function loadMeals() {
            mealsContent.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const { meals } = await fetchMeals();
                renderMeals(meals);
            } catch (e) {
                mealsContent.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Failed to load meals</div></div>';
            }
        }

        async function loadTopics() {
            try {
                const { topics } = await fetchTopics();
                renderTopics(topics);
            } catch (e) {
                topicsCloud.innerHTML = '<div class="empty-state"><p>Failed to load topics</p></div>';
            }
        }

        async function loadStats() {
            let totalMemories = 0;
            let totalTokens = 0;

            try {
                const stats = await fetchStats();
                totalMemories = stats.total_memories || 0;
                document.getElementById('stats-memories').textContent = totalMemories;
                document.getElementById('stats-meals').textContent = stats.total_meals || 0;
            } catch (e) {}

            // Load graph stats separately
            try {
                const graphStats = await (await fetch('/api/graph/stats')).json();
                totalTokens = graphStats.total_tokens || 0;
                document.getElementById('stats-nodes').textContent = graphStats.total_nodes || 0;
            } catch (e) {}

            // First-time migration: offer to import diary entries if the graph
            // holds no knowledge yet but the user has diary data.
            if (totalTokens === 0 && totalMemories > 0 && !diaryImportDone) {
                showImportDiaryModal(true);
            }
        }

        // Event handlers
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchDebounce);
            searchDebounce = setTimeout(() => {
                searchQuery = e.target.value.trim();
                loadMemories();
            }, 300);
        });

        fromDateInput.addEventListener('change', (e) => {
            fromDate = e.target.value;
            mealsFromDateInput.value = fromDate;
            loadMemories();
        });

        toDateInput.addEventListener('change', (e) => {
            toDate = e.target.value;
            mealsToDateInput.value = toDate;
            loadMemories();
        });

        mealsFromDateInput.addEventListener('change', (e) => {
            fromDate = e.target.value;
            fromDateInput.value = fromDate;
            loadMeals();
        });

        mealsToDateInput.addEventListener('change', (e) => {
            toDate = e.target.value;
            toDateInput.value = toDate;
            loadMeals();
        });

        function switchTab(tabName) {
            tabs.forEach(t => t.classList.remove('active'));
            document.querySelector(`.tab[data-tab="${tabName}"]`).classList.add('active');
            currentTab = tabName;

            // Hide all panes
            chatPane.style.display = 'none';
            memoriesPane.style.display = 'none';
            graphContent.style.display = 'none';
            mealsPane.style.display = 'none';
            connPane.style.display = 'none';
            settingsPane.style.display = 'none';

            if (currentTab === 'chat') {
                chatPane.style.display = '';
                chatInput.focus();
            } else if (currentTab === 'memories') {
                memoriesPane.style.display = '';
                loadMemories();
            } else if (currentTab === 'graph') {
                graphContent.style.display = '';
                initGraph();
            } else if (currentTab === 'connections') {
                connPane.style.display = '';
                loadConnections();
            } else if (currentTab === 'settings') {
                settingsPane.style.display = '';
                loadSettings();
            } else {
                mealsPane.style.display = '';
                loadMeals();
            }
        }

        tabs.forEach(tab => {
            tab.addEventListener('click', () => switchTab(tab.dataset.tab));
        });

        // ── Settings ──────────────────────────────────────────────────
        async function loadSettings() {
            const d = await (await fetch('/api/settings')).json();
            document.getElementById('set-provider').value = d.llm_provider || 'openai_compatible';
            document.getElementById('set-baseurl').value = d.llm_base_url || '';
            document.getElementById('set-chat').value = d.llm_chat_model || '';
            document.getElementById('set-fast').value = d.fast_model || '';
            document.getElementById('set-embed').value = d.embedding_model || '';
            // The key is never sent to the page. A hint identifies which one
            // is loaded without exposing the credential.
            document.getElementById('set-key').placeholder = d.has_key
                ? `key ending ${d.key_hint} is saved — leave blank to keep it`
                : 'paste your API key';
        }

        function settingsPayload() {
            return {
                llm_provider: document.getElementById('set-provider').value,
                llm_base_url: document.getElementById('set-baseurl').value.trim(),
                llm_chat_model: document.getElementById('set-chat').value.trim(),
                fast_model: document.getElementById('set-fast').value.trim(),
                embedding_model: document.getElementById('set-embed').value.trim(),
                llm_api_key: document.getElementById('set-key').value.trim()
            };
        }

        function setStatus(text, cls) {
            const el = document.getElementById('set-status');
            el.textContent = text;
            el.className = 'settings-status ' + (cls || '');
        }

        document.getElementById('set-save').addEventListener('click', async () => {
            setStatus('Saving…');
            const res = await fetch('/api/settings', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(settingsPayload())
            });
            const d = await res.json();
            if (res.ok) {
                document.getElementById('set-key').value = '';
                setStatus('Saved — applies on your next message.', 'ok');
                loadSettings();
            } else {
                setStatus(d.error || 'Could not save.', 'bad');
            }
        });

        document.getElementById('set-test').addEventListener('click', async () => {
            setStatus('Testing…');
            const res = await fetch('/api/settings/test', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(settingsPayload())
            });
            const d = await res.json();
            if (res.ok) setStatus(`Connected — ${d.count} models available.`, 'ok');
            else setStatus(d.error || 'Connection failed.', 'bad');
        });

        // ── Connections (MCP) ─────────────────────────────────────────
        async function loadConnections() {
            const list = document.getElementById('conn-list');
            list.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const data = await (await fetch('/api/mcp')).json();
                if (!data.servers.length) {
                    list.innerHTML = '<div class="empty-state">'
                        + '<div class="empty-icon">🔌</div>'
                        + '<div class="empty-title">No connections yet</div>'
                        + '<p>Add an MCP server above to give Jarvis more tools.</p></div>';
                    return;
                }
                list.innerHTML = data.servers.map(s => {
                    // A configured server offering zero tools is a failure worth
                    // showing — silence here used to look like success.
                    const ok = s.tools > 0;
                    const label = ok ? `${s.tools} tool${s.tools === 1 ? '' : 's'}`
                                     : (s.error ? 'failed' : 'no tools');
                    const cmd = escapeHtml([s.command].concat(s.args || []).join(' '));
                    return `<div class="conn-card">
                        <div class="conn-meta">
                            <div class="conn-name">${escapeHtml(s.name)}</div>
                            <div class="conn-cmd">${cmd}</div>
                            ${s.error ? `<div class="conn-cmd">⚠️ ${escapeHtml(s.error)}</div>` : ''}
                        </div>
                        <span class="conn-pill ${ok ? 'ok' : 'bad'}">${label}</span>
                        <button class="conn-remove" data-name="${escapeHtml(s.name)}">Remove</button>
                    </div>`;
                }).join('');

                list.querySelectorAll('.conn-remove').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        await fetch('/api/mcp/' + encodeURIComponent(btn.dataset.name),
                                    {method: 'DELETE'});
                        loadConnections();
                    });
                });
            } catch (err) {
                list.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div>'
                    + '<div class="empty-title">Could not load connections</div></div>';
            }
        }

        document.getElementById('conn-add-btn').addEventListener('click', async () => {
            const name = document.getElementById('conn-name').value.trim();
            const command = document.getElementById('conn-command').value.trim();
            const args = document.getElementById('conn-args').value.trim();
            if (!name || !command) return;

            const res = await fetch('/api/mcp', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name, command, args: args ? args.split(/\\s+/) : []})
            });
            if (res.ok) {
                document.getElementById('conn-name').value = '';
                document.getElementById('conn-command').value = '';
                document.getElementById('conn-args').value = '';
                loadConnections();
            }
        });

        // ── HUD rail ──────────────────────────────────────────────────
        // Telemetry is polled, not pushed: these numbers are ambient, and a
        // socket for three gauges is not worth the reconnect logic.
        function setGauge(id, barId, percent, label) {
            document.getElementById(id).textContent = label;
            document.getElementById(barId).style.width = Math.max(0, Math.min(100, percent)) + '%';
        }

        async function loadSystem() {
            try {
                const d = await (await fetch('/api/system')).json();
                if (d.cpu !== undefined) setGauge('sys-cpu', 'bar-cpu', d.cpu, d.cpu.toFixed(0) + '%');
                if (d.memory) setGauge('sys-mem', 'bar-mem', d.memory.percent,
                    `${d.memory.used_gb}/${d.memory.total_gb} GB`);
                if (d.disk) setGauge('sys-disk', 'bar-disk', d.disk.percent,
                    `${d.disk.free_gb} GB free`);
                if (d.provider) document.getElementById('sys-provider').textContent = d.provider;
                if (d.model) document.getElementById('sys-model').textContent = d.model;
            } catch (e) { /* rail is ambient; a failed poll is not worth an alert */ }
        }

        async function loadWeather() {
            const body = document.getElementById('weather-body');
            try {
                const res = await fetch('/api/weather');
                const d = await res.json();
                if (!res.ok) {
                    // Say it plainly rather than showing a number nobody can trust.
                    body.textContent = 'Unavailable — enable location in config.';
                    return;
                }
                body.textContent = (d.text || '').split('\\n').slice(0, 4).join('\\n');
            } catch (e) {
                body.textContent = 'Unavailable.';
            }
        }

        loadSystem();
        loadWeather();
        setInterval(loadSystem, 5000);
        setInterval(loadWeather, 15 * 60 * 1000);

        // Activate focuses the composer. It does not start the microphone:
        // a browser cannot reach the mic through Flask, and a button that
        // implies otherwise would be a lie about what this page can do.
        document.getElementById('activate-btn').addEventListener('click', () => {
            chatInput.focus();
        });

        // Mirror the orb's state into the rail readout.
        const _setOrbState = setOrbState;
        setOrbState = function (state) {
            _setOrbState(state);
            const el = document.getElementById('sys-state');
            if (el) el.textContent = state.toUpperCase();
        };

        // ── Orb ───────────────────────────────────────────────────────
        // Same geometry as the desktop widget (orb_widget.py): points spread
        // over a sphere by a Fibonacci spiral, spun about the vertical axis
        // and projected flat, with depth driving size and opacity. Kept in
        // step with MOTION there — speaking is the only state that ripples,
        // because that is the cue that Jarvis is talking.
        const ORB_MOTION = {
            idle:     {spin: 0.18, breathe: 0.028, ripple: 0,     rippleSpeed: 0,   turbulence: 0,     brightness: 0.70},
            thinking: {spin: 0.85, breathe: 0.030, ripple: 0,     rippleSpeed: 0,   turbulence: 0.10,  brightness: 0.85},
            speaking: {spin: 0.34, breathe: 0.030, ripple: 0.115, rippleSpeed: 3.1, turbulence: 0.015, brightness: 1.00}
        };
        const ORB_POINTS = (() => {
            const n = 380, golden = Math.PI * (3 - Math.sqrt(5)), pts = [];
            for (let i = 0; i < n; i++) {
                const y = 1 - (2 * i) / (n - 1);
                const r = Math.sqrt(Math.max(0, 1 - y * y));
                const th = golden * i;
                pts.push([Math.cos(th) * r, y, Math.sin(th) * r]);
            }
            return pts;
        })();

        let orbState = 'idle';
        const orbCanvas = document.getElementById('orb');
        const orbCtx = orbCanvas.getContext('2d');
        const orbLabel = document.getElementById('orb-state');

        function setOrbState(state) {
            orbState = state;
            orbLabel.textContent = state;
        }

        function drawOrb(t) {
            const m = ORB_MOTION[orbState] || ORB_MOTION.idle;
            const w = orbCanvas.width, h = orbCanvas.height;
            const cx = w / 2, cy = h / 2, radius = Math.min(w, h) * 0.235;
            orbCtx.clearRect(0, 0, w, h);

            // ── HUD instrumentation ────────────────────────────────
            // Layered concentric tracks, counter-rotating at different
            // rates. Every layer's speed is derived from m.spin, so the
            // whole assembly accelerates when Jarvis is thinking and
            // settles when idle — the rings report state, not decoration.
            const glow = (colour, blur) => {
                orbCtx.shadowColor = colour;
                orbCtx.shadowBlur = blur;
            };
            const CY = '103,232,249', CY_DEEP = '34,211,238';

            // 1. Outer dashed containment ring.
            glow(`rgba(${CY_DEEP},0.95)`, 18);
            orbCtx.strokeStyle = `rgba(${CY},${0.85 * m.brightness})`;
            orbCtx.lineWidth = 2;
            orbCtx.setLineDash([14, 9]);
            orbCtx.lineDashOffset = -t * m.spin * 90;
            orbCtx.beginPath();
            orbCtx.arc(cx, cy, radius * 1.95, 0, Math.PI * 2);
            orbCtx.stroke();
            orbCtx.setLineDash([]);

            // 2. Heavy segmented arcs — the ring that reads as "active".
            const seg = [
                {r: 1.72, from: 0.15, sweep: 1.55, dir:  1, w: 7,   a: 0.95},
                {r: 1.72, from: 3.30, sweep: 1.20, dir:  1, w: 7,   a: 0.95},
                {r: 1.52, from: 1.90, sweep: 2.10, dir: -1, w: 3.5, a: 0.80},
                {r: 1.52, from: 5.10, sweep: 0.80, dir: -1, w: 3.5, a: 0.80},
                {r: 1.34, from: 0.60, sweep: 2.60, dir:  1, w: 2,   a: 0.55}
            ];
            for (const s of seg) {
                const a0 = s.from + t * m.spin * 1.6 * s.dir;
                glow(`rgba(${CY_DEEP},0.95)`, 20);
                orbCtx.strokeStyle = `rgba(${CY},${s.a * m.brightness})`;
                orbCtx.lineWidth = s.w;
                orbCtx.lineCap = 'round';
                orbCtx.beginPath();
                orbCtx.arc(cx, cy, radius * s.r, a0, a0 + s.sweep);
                orbCtx.stroke();
            }
            orbCtx.lineCap = 'butt';

            // 3. Fine tick track.
            glow(`rgba(${CY_DEEP},0.8)`, 8);
            for (let i = 0; i < 72; i++) {
                const a = (i / 72) * Math.PI * 2 - t * m.spin * 0.55;
                const major = i % 6 === 0;
                const inner = radius * 1.80;
                const outer = radius * (major ? 1.92 : 1.86);
                orbCtx.strokeStyle = `rgba(${CY},${(major ? 0.9 : 0.45) * m.brightness})`;
                orbCtx.lineWidth = major ? 2 : 1;
                orbCtx.beginPath();
                orbCtx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
                orbCtx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer);
                orbCtx.stroke();
            }

            // 4. Inner data ring: discrete blocks, like a readout.
            for (let i = 0; i < 40; i++) {
                const a = (i / 40) * Math.PI * 2 + t * m.spin * 2.2;
                const lit = (i * 7 + Math.floor(t * 6)) % 5 !== 0;
                orbCtx.strokeStyle = `rgba(${CY},${(lit ? 0.75 : 0.16) * m.brightness})`;
                orbCtx.lineWidth = 3;
                orbCtx.beginPath();
                orbCtx.arc(cx, cy, radius * 1.16, a, a + 0.10);
                orbCtx.stroke();
            }

            // 5. Core disc behind the particles, so the sphere sits in light.
            const core = orbCtx.createRadialGradient(cx, cy, 0, cx, cy, radius * 1.05);
            core.addColorStop(0, `rgba(${CY_DEEP},${0.30 * m.brightness})`);
            core.addColorStop(0.65, `rgba(${CY_DEEP},${0.10 * m.brightness})`);
            core.addColorStop(1, 'rgba(4,10,20,0)');
            orbCtx.shadowBlur = 0;
            orbCtx.fillStyle = core;
            orbCtx.beginPath();
            orbCtx.arc(cx, cy, radius * 1.05, 0, Math.PI * 2);
            orbCtx.fill();

            const swell = 1 + m.breathe * Math.sin(t * 1.9);
            const spun = m.spin * t, cs = Math.cos(spun), sn = Math.sin(spun);
            const drawn = [];

            for (let i = 0; i < ORB_POINTS.length; i++) {
                const [x, y, z] = ORB_POINTS[i];
                let r = swell;
                if (m.ripple) r += m.ripple * Math.sin(y * 3.4 - t * m.rippleSpeed);
                if (m.turbulence) {
                    r += m.turbulence * Math.sin(i * 12.9898 + t * 1.7) * Math.cos(i * 4.1414 + t * 0.9);
                }
                const rx = (x * cs - z * sn) * r;
                const rz = (x * sn + z * cs) * r;
                const depth = Math.max(0, Math.min(1, (rz + 1) / 2));
                drawn.push([cx + rx * radius, cy - y * r * radius, depth]);
            }
            drawn.sort((a, b) => a[2] - b[2]);  // far side first
            orbCtx.shadowColor = 'rgba(34,211,238,0.9)';
            orbCtx.shadowBlur = 10;

            for (const [px, py, depth] of drawn) {
                const size = 0.7 + 2.0 * Math.pow(depth, 1.3);
                const alpha = (0.18 + 0.82 * Math.pow(depth, 1.6)) * m.brightness;
                const cr = Math.round(0x0e + (0x67 - 0x0e) * depth);
                const cg = Math.round(0x5f + (0xe8 - 0x5f) * depth);
                const cb = Math.round(0x74 + (0xf9 - 0x74) * depth);
                orbCtx.fillStyle = `rgba(${cr},${cg},${cb},${alpha})`;
                orbCtx.beginPath();
                orbCtx.arc(px, py, size, 0, Math.PI * 2);
                orbCtx.fill();
            }
            orbCtx.shadowBlur = 0;

            // Core label, over the sphere.
            orbCtx.save();
            orbCtx.shadowColor = 'rgba(34,211,238,1)';
            orbCtx.shadowBlur = 16;
            orbCtx.fillStyle = `rgba(232,247,252,${0.95 * m.brightness})`;
            orbCtx.font = `600 ${Math.round(radius * 0.30)}px 'JetBrains Mono', monospace`;
            orbCtx.textAlign = 'center';
            orbCtx.textBaseline = 'middle';
            orbCtx.fillText('J.A.R.V.I.S', cx, cy);
            orbCtx.restore();
        }

        (function orbLoop(start) {
            const step = (now) => {
                drawOrb((now - start) / 1000);
                requestAnimationFrame(step);
            };
            requestAnimationFrame(step);
        })(performance.now());

        // ── Chat ──────────────────────────────────────────────────────
        let chatBusy = false;

        function appendBubble(role, text) {
            const empty = chatLog.querySelector('.empty-state');
            if (empty) empty.remove();
            document.querySelector('.hud-core')?.classList.add('talking');
            const row = document.createElement('div');
            row.className = 'chat-msg ' + role;
            row.innerHTML = `<div class="chat-bubble">${escapeHtml(text)}</div>`;
            chatLog.appendChild(row);
            chatLog.scrollTop = chatLog.scrollHeight;
            return row;
        }

        async function sendChat() {
            const message = chatInput.value.trim();
            if (!message || chatBusy) return;

            chatBusy = true;
            setOrbState('thinking');
            chatInput.value = '';
            chatInput.style.height = 'auto';
            appendBubble('user', message);

            // The reply engine plans, may call tools, then answers — that
            // can run to tens of seconds, so the wait needs to be visible
            // or the page looks broken.
            const pending = appendBubble('assistant pending', 'Thinking…');

            try {
                const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message})
                });
                const data = await response.json();
                pending.remove();
                if (!response.ok) {
                    appendBubble('assistant error', data.error || 'Something went wrong.');
                } else {
                    appendBubble('assistant', data.reply);
                    // Hold SPEAKING for roughly as long as the reply takes to
                    // read, so the ripple lines up with the user reading it.
                    setOrbState('speaking');
                    const dwell = Math.min(9000, 1200 + (data.reply || '').length * 45);
                    setTimeout(() => { if (!chatBusy) setOrbState('idle'); }, dwell);
                }
            } catch (err) {
                pending.remove();
                appendBubble('assistant error', 'Could not reach Jarvis: ' + err.message);
            } finally {
                chatBusy = false;
                if (orbState === 'thinking') setOrbState('idle');
                chatInput.focus();
            }
        }

        document.getElementById('chat-send').addEventListener('click', sendChat);

        chatInput.addEventListener('keydown', (e) => {
            // Enter sends; Shift+Enter is a newline, as in every chat app.
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChat();
            }
        });

        chatInput.addEventListener('input', () => {
            chatInput.style.height = 'auto';
            chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + 'px';
        });

        document.getElementById('chat-reset').addEventListener('click', async () => {
            await fetch('/api/chat/reset', {method: 'POST'});
            chatLog.innerHTML = '<div class="empty-state">'
                + '<div class="empty-icon">💬</div>'
                + '<div class="empty-title">Fresh conversation</div>'
                + '<p>The previous one was saved to the diary.</p></div>';
            document.querySelector('.hud-core')?.classList.remove('talking');
            setOrbState('idle');
        });

        // Diary maintenance button lives in the diary tab's sidebar, which
        // renders on page load (diary is the default tab). Wire its handler
        // here on the always-run setup path — initGraph only fires when the
        // user opens the Knowledge tab, and the diary clean button must
        // work even for users who never visit it.
        document.getElementById('btn-scrub-deflections').addEventListener('click', () => {
            showScrubDeflectionsModal();
        });

        document.getElementById('btn-optimise-topics').addEventListener('click', () => {
            showOptimiseTopicsModal();
        });

        // ─── Graph Explorer ────────────────────────────────────────────
        let graphInitialised = false;
        let graphNodes = [];
        let graphEdges = [];
        let selectedNodeId = null;
        let graphZoom = 1;
        let graphPanX = 0;
        let graphPanY = 0;
        let isDragging = false;
        let dragStartX = 0;
        let dragStartY = 0;
        let hoveredNodeId = null;

        // Layout positions (computed once per data load)
        const nodePositions = new Map();

        const canvas = document.getElementById('graph-canvas');
        const ctx = canvas.getContext('2d');

        async function loadPresetNodeIds() {
            try {
                const resp = await fetch('/api/graph/presets');
                const data = await resp.json();
                if (Array.isArray(data.ids)) {
                    PRESET_NODE_IDS = new Set(data.ids);
                }
            } catch (e) {
                // Fail-closed: leave the seeded {'root'} set in place.
                console.warn('Failed to load preset node IDs; falling back to root-only.', e);
            }
        }

        async function initGraph() {
            if (!graphInitialised) {
                setupCanvasEvents();
                graphInitialised = true;
            }
            resizeCanvas();
            await loadPresetNodeIds();
            loadGraphData();
            loadTreeData();
        }

        function resizeCanvas() {
            const container = canvas.parentElement;
            canvas.width = container.clientWidth * window.devicePixelRatio;
            canvas.height = container.clientHeight * window.devicePixelRatio;
            canvas.style.width = container.clientWidth + 'px';
            canvas.style.height = container.clientHeight + 'px';
            ctx.setTransform(window.devicePixelRatio, 0, 0, window.devicePixelRatio, 0, 0);
        }

        async function loadGraphData() {
            try {
                const resp = await fetch('/api/graph/nodes?max_depth=10');
                const data = await resp.json();
                graphNodes = data.nodes || [];
                graphEdges = data.edges || [];
                computeLayout();
                fitToView();
                drawGraph();
            } catch (e) {
                console.error('Failed to load graph:', e);
            }
        }

        async function loadTreeData() {
            const container = document.getElementById('tree-container');
            try {
                const resp = await fetch('/api/graph/tree?max_depth=10');
                const tree = await resp.json();
                container.innerHTML = '';
                if (tree.node) {
                    renderTreeNode(container, tree, 0);
                }
            } catch (e) {
                container.innerHTML = '<div class="detail-empty"><p>Failed to load tree</p></div>';
            }
        }

        function renderTreeNode(container, treeData, depth) {
            const node = treeData.node;
            const children = treeData.children || [];
            const hasChildren = children.length > 0;

            const el = document.createElement('div');

            const nodeEl = document.createElement('div');
            nodeEl.className = 'tree-node' + (selectedNodeId === node.id ? ' selected' : '');
            nodeEl.dataset.nodeId = node.id;
            nodeEl.style.paddingLeft = (0.75 + depth * 0.75) + 'rem';

            const toggle = document.createElement('span');
            toggle.className = 'tree-toggle' + (hasChildren ? ' expanded' : ' leaf');
            toggle.textContent = '▶';

            const nameSpan = document.createElement('span');
            nameSpan.className = 'tree-node-name';
            nameSpan.textContent = node.name;

            nodeEl.appendChild(toggle);
            nodeEl.appendChild(nameSpan);

            if (hasChildren) {
                const countSpan = document.createElement('span');
                countSpan.className = 'tree-node-count';
                countSpan.textContent = children.length;
                nodeEl.appendChild(countSpan);
            }

            nodeEl.addEventListener('click', (e) => {
                e.stopPropagation();
                selectNode(node.id);
            });

            el.appendChild(nodeEl);

            if (hasChildren) {
                const childContainer = document.createElement('div');
                childContainer.className = 'tree-children';

                toggle.addEventListener('click', (e) => {
                    e.stopPropagation();
                    childContainer.classList.toggle('collapsed');
                    toggle.classList.toggle('expanded');
                });

                children.forEach(child => {
                    renderTreeNode(childContainer, child, depth + 1);
                });

                el.appendChild(childContainer);
            }

            container.appendChild(el);
        }

        function computeLayout() {
            nodePositions.clear();
            if (graphNodes.length === 0) return;

            // Build adjacency for tree layout
            const childrenMap = new Map();
            graphNodes.forEach(n => childrenMap.set(n.id, []));
            graphEdges.forEach(e => {
                const list = childrenMap.get(e.source);
                if (list) list.push(e.target);
            });

            // Radial tree layout
            const root = graphNodes.find(n => n.id === 'root') || graphNodes[0];
            const visited = new Set();
            const RING_SPACING = 160;
            const MIN_ARC = 40;

            function layoutSubtree(nodeId, cx, cy, startAngle, endAngle, depth) {
                if (visited.has(nodeId)) return;
                visited.add(nodeId);

                nodePositions.set(nodeId, { x: cx, y: cy });

                const kids = childrenMap.get(nodeId) || [];
                if (kids.length === 0) return;

                const radius = RING_SPACING * (depth + 1);
                const arcPerChild = (endAngle - startAngle) / kids.length;

                kids.forEach((kidId, i) => {
                    const angle = startAngle + arcPerChild * (i + 0.5);
                    const kx = cx + Math.cos(angle) * radius;
                    const ky = cy + Math.sin(angle) * radius;
                    const halfArc = arcPerChild * 0.45;
                    layoutSubtree(kidId, kx, ky, angle - halfArc, angle + halfArc, depth + 1);
                });
            }

            layoutSubtree(root.id, 0, 0, 0, Math.PI * 2, 0);

            // Place any unvisited nodes in a line below
            let offsetX = -200;
            graphNodes.forEach(n => {
                if (!visited.has(n.id)) {
                    nodePositions.set(n.id, { x: offsetX, y: 500 });
                    offsetX += 100;
                }
            });
        }

        function fitToView() {
            if (nodePositions.size === 0) return;

            const cw = canvas.width / window.devicePixelRatio;
            const ch = canvas.height / window.devicePixelRatio;
            const padding = 80;

            let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
            nodePositions.forEach(pos => {
                minX = Math.min(minX, pos.x);
                maxX = Math.max(maxX, pos.x);
                minY = Math.min(minY, pos.y);
                maxY = Math.max(maxY, pos.y);
            });

            const graphW = maxX - minX || 1;
            const graphH = maxY - minY || 1;
            graphZoom = Math.min((cw - padding * 2) / graphW, (ch - padding * 2) / graphH, 2);
            graphZoom = Math.max(graphZoom, 0.1);

            const centerX = (minX + maxX) / 2;
            const centerY = (minY + maxY) / 2;
            graphPanX = cw / 2 - centerX * graphZoom;
            graphPanY = ch / 2 - centerY * graphZoom;

            drawGraph();
        }

        function drawGraph() {
            const cw = canvas.width / window.devicePixelRatio;
            const ch = canvas.height / window.devicePixelRatio;
            ctx.clearRect(0, 0, cw, ch);

            ctx.save();
            ctx.translate(graphPanX, graphPanY);
            ctx.scale(graphZoom, graphZoom);

            // Draw edges
            ctx.lineWidth = 1.5 / graphZoom;
            graphEdges.forEach(edge => {
                const from = nodePositions.get(edge.source);
                const to = nodePositions.get(edge.target);
                if (!from || !to) return;

                ctx.beginPath();
                ctx.moveTo(from.x, from.y);
                ctx.lineTo(to.x, to.y);
                ctx.strokeStyle = 'rgba(245, 158, 11, 0.15)';
                ctx.stroke();
            });

            // Draw nodes
            graphNodes.forEach(node => {
                const pos = nodePositions.get(node.id);
                if (!pos) return;

                const isSelected = node.id === selectedNodeId;
                const isHovered = node.id === hoveredNodeId;
                const isRoot = node.id === 'root';
                const baseRadius = isRoot ? 24 : Math.max(12, Math.min(20, 10 + node.access_count * 0.5));
                const radius = baseRadius;

                // Glow for selected/hovered
                if (isSelected || isHovered) {
                    ctx.beginPath();
                    ctx.arc(pos.x, pos.y, radius + 6, 0, Math.PI * 2);
                    ctx.fillStyle = isSelected
                        ? 'rgba(245, 158, 11, 0.25)'
                        : 'rgba(245, 158, 11, 0.12)';
                    ctx.fill();
                }

                // Node circle
                ctx.beginPath();
                ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);

                if (isSelected) {
                    ctx.fillStyle = '#f59e0b';
                } else if (isRoot) {
                    ctx.fillStyle = '#1a1d26';
                } else if (node.has_children) {
                    ctx.fillStyle = '#1e222c';
                } else {
                    ctx.fillStyle = '#161920';
                }
                ctx.fill();

                ctx.lineWidth = (isSelected ? 2.5 : 1.5) / graphZoom;
                ctx.strokeStyle = isSelected ? '#fbbf24' : isHovered ? '#f59e0b' : '#27272a';
                ctx.stroke();

                // Label
                const fontSize = Math.max(10, 12 / graphZoom);
                ctx.font = `500 ${fontSize}px Outfit, sans-serif`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillStyle = isSelected ? '#fef3c7' : '#f4f4f5';

                // Truncate name to fit
                let label = node.name;
                if (label.length > 14) label = label.slice(0, 12) + '…';
                ctx.fillText(label, pos.x, pos.y);
            });

            ctx.restore();
        }

        function getNodeAtPosition(screenX, screenY) {
            const x = (screenX - graphPanX) / graphZoom;
            const y = (screenY - graphPanY) / graphZoom;

            for (let i = graphNodes.length - 1; i >= 0; i--) {
                const node = graphNodes[i];
                const pos = nodePositions.get(node.id);
                if (!pos) continue;

                const isRoot = node.id === 'root';
                const radius = isRoot ? 24 : Math.max(12, Math.min(20, 10 + node.access_count * 0.5));
                const dx = pos.x - x;
                const dy = pos.y - y;
                if (dx * dx + dy * dy <= (radius + 4) * (radius + 4)) {
                    return node;
                }
            }
            return null;
        }

        function setupCanvasEvents() {
            canvas.addEventListener('mousedown', (e) => {
                isDragging = true;
                dragStartX = e.offsetX;
                dragStartY = e.offsetY;
            });

            canvas.addEventListener('mousemove', (e) => {
                if (isDragging) {
                    graphPanX += e.offsetX - dragStartX;
                    graphPanY += e.offsetY - dragStartY;
                    dragStartX = e.offsetX;
                    dragStartY = e.offsetY;
                    drawGraph();
                } else {
                    const node = getNodeAtPosition(e.offsetX, e.offsetY);
                    const newHovered = node ? node.id : null;
                    if (newHovered !== hoveredNodeId) {
                        hoveredNodeId = newHovered;
                        canvas.style.cursor = newHovered ? 'pointer' : 'grab';
                        drawGraph();
                    }
                }
            });

            canvas.addEventListener('mouseup', (e) => {
                const wasDrag = Math.abs(e.offsetX - dragStartX) > 3 || Math.abs(e.offsetY - dragStartY) > 3;
                isDragging = false;

                if (!wasDrag) {
                    const node = getNodeAtPosition(e.offsetX, e.offsetY);
                    if (node) {
                        selectNode(node.id);
                    }
                }
            });

            canvas.addEventListener('wheel', (e) => {
                e.preventDefault();
                const delta = e.deltaY > 0 ? 0.9 : 1.1;
                const mouseX = e.offsetX;
                const mouseY = e.offsetY;

                // Zoom towards mouse position
                graphPanX = mouseX - (mouseX - graphPanX) * delta;
                graphPanY = mouseY - (mouseY - graphPanY) * delta;
                graphZoom *= delta;
                graphZoom = Math.max(0.05, Math.min(5, graphZoom));

                drawGraph();
            }, { passive: false });

            // Toolbar
            document.getElementById('btn-zoom-in').addEventListener('click', () => {
                const cw = canvas.width / window.devicePixelRatio;
                const ch = canvas.height / window.devicePixelRatio;
                graphPanX = cw/2 - (cw/2 - graphPanX) * 1.3;
                graphPanY = ch/2 - (ch/2 - graphPanY) * 1.3;
                graphZoom *= 1.3;
                drawGraph();
            });

            document.getElementById('btn-zoom-out').addEventListener('click', () => {
                const cw = canvas.width / window.devicePixelRatio;
                const ch = canvas.height / window.devicePixelRatio;
                graphPanX = cw/2 - (cw/2 - graphPanX) * 0.7;
                graphPanY = ch/2 - (ch/2 - graphPanY) * 0.7;
                graphZoom *= 0.7;
                drawGraph();
            });

            document.getElementById('btn-fit').addEventListener('click', fitToView);

            document.getElementById('btn-add-node').addEventListener('click', () => {
                showCreateNodeModal(selectedNodeId || 'root');
            });

            document.getElementById('btn-import-diary').addEventListener('click', () => {
                showImportDiaryModal();
            });

            document.getElementById('btn-consolidate-all').addEventListener('click', () => {
                showConsolidateAllModal();
            });

            // Resize observer
            new ResizeObserver(() => {
                if (currentTab === 'graph') {
                    resizeCanvas();
                    drawGraph();
                }
            }).observe(canvas.parentElement);
        }

        async function selectNode(nodeId) {
            selectedNodeId = nodeId;

            // Update tree selection highlight in-place (no re-render)
            document.querySelectorAll('.tree-node').forEach(el => {
                el.classList.toggle('selected', el.dataset.nodeId === nodeId);
            });

            // Redraw graph
            drawGraph();

            // Load node details
            const sidebar = document.getElementById('detail-sidebar');
            sidebar.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

            try {
                const resp = await fetch('/api/graph/node/' + nodeId);
                const data = await resp.json();
                renderNodeDetail(data);
            } catch (e) {
                sidebar.innerHTML = '<div class="detail-empty"><p>Failed to load node</p></div>';
            }
        }

        function renderNodeDetail(data) {
            const { node, children, ancestors } = data;
            const sidebar = document.getElementById('detail-sidebar');

            const breadcrumb = ancestors.map((a, i) => {
                const isLast = i === ancestors.length - 1;
                return `<span onclick="selectNode('${a.id}')">${escapeHtml(a.name)}</span>` +
                       (isLast ? '' : '<span class="sep"> › </span>');
            }).join('');

            const childrenHtml = children.length > 0
                ? children.map(c => `
                    <div class="detail-child" onclick="selectNode('${c.id}')">
                        <span>${c.has_children || c.data_token_count > 0 ? '📁' : '📄'}</span>
                        <span class="detail-child-name">${escapeHtml(c.name)}</span>
                        <span class="tree-node-count">${c.data_token_count}t</span>
                    </div>
                `).join('')
                : '<div style="color: var(--text-muted); font-size: 0.85rem;">No children</div>';

            const dataHtml = node.data
                ? `<div class="detail-data">${escapeHtml(node.data)}</div>`
                : '<div style="color: var(--text-muted); font-size: 0.85rem; font-style: italic;">No data stored</div>';

            const lastAccessed = new Date(node.last_accessed).toLocaleDateString('en-GB', {
                day: 'numeric', month: 'short', year: 'numeric'
            });

            sidebar.innerHTML = `
                <div class="detail-breadcrumb">${breadcrumb}</div>
                <div class="detail-name">${escapeHtml(node.name)}</div>
                <div class="detail-description">${escapeHtml(node.description)}</div>

                <div class="detail-meta">
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Accesses</div>
                        <div class="detail-meta-value">${node.access_count}</div>
                    </div>
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Tokens</div>
                        <div class="detail-meta-value">${node.data_token_count}</div>
                    </div>
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Last seen</div>
                        <div class="detail-meta-value">${lastAccessed}</div>
                    </div>
                    <div class="detail-meta-item">
                        <div class="detail-meta-label">Children</div>
                        <div class="detail-meta-value">${children.length}</div>
                    </div>
                </div>

                <div class="detail-section">
                    <div class="detail-section-title">💾 Data</div>
                    ${dataHtml}
                </div>

                <div class="detail-section">
                    <div class="detail-section-title">📂 Children</div>
                    <div class="detail-children-list">${childrenHtml}</div>
                </div>

                <div class="detail-actions">
                    <button class="detail-action-btn" onclick="editNode('${node.id}')">✏️ Edit</button>
                    <button class="detail-action-btn" onclick="showCreateNodeModal('${node.id}')">➕ Add child</button>
                    ${!PRESET_NODE_IDS.has(node.id) ? `<button class="detail-action-btn delete" onclick="deleteNode('${node.id}')">🗑️ Delete</button>` : ''}
                </div>
            `;
        }

        async function editNode(nodeId) {
            const resp = await fetch('/api/graph/node/' + nodeId);
            const { node } = await resp.json();

            const sidebar = document.getElementById('detail-sidebar');
            sidebar.innerHTML = `
                <div class="detail-name">✏️ Edit Node</div>
                <div class="modal-field">
                    <label>Name</label>
                    <input type="text" class="detail-edit-field" id="edit-name" value="${escapeHtml(node.name)}" />
                </div>
                <div class="modal-field">
                    <label>Description</label>
                    <textarea class="detail-edit-field" id="edit-desc" rows="3">${escapeHtml(node.description)}</textarea>
                </div>
                <div class="modal-field">
                    <label>Data</label>
                    <textarea class="detail-edit-field" id="edit-data" rows="8">${escapeHtml(node.data)}</textarea>
                </div>
                <div class="detail-actions">
                    <button class="detail-action-btn" onclick="saveNodeEdit('${nodeId}')" style="background: var(--accent-glow); border-color: var(--accent-primary); color: var(--accent-secondary);">💾 Save</button>
                    <button class="detail-action-btn" onclick="selectNode('${nodeId}')">Cancel</button>
                </div>
            `;
        }

        async function saveNodeEdit(nodeId) {
            const name = document.getElementById('edit-name').value.trim();
            const description = document.getElementById('edit-desc').value.trim();
            const data = document.getElementById('edit-data').value;

            if (!name) { showToast('Name is required', 'error'); return; }

            try {
                await fetch('/api/graph/node/' + nodeId, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, description, data })
                });
                showToast('Node updated', 'success');
                loadGraphData();
                loadTreeData();
                selectNode(nodeId);
            } catch (e) {
                showToast('Failed to update', 'error');
            }
        }

        async function deleteNode(nodeId) {
            if (!confirm('Delete this node? Children will be orphaned.')) return;

            try {
                await fetch('/api/graph/node/' + nodeId, { method: 'DELETE' });
                showToast('Node deleted', 'success');
                selectedNodeId = null;
                document.getElementById('detail-sidebar').innerHTML =
                    '<div class="detail-empty"><div class="empty-icon">🧠</div><p>Select a node to view its details</p></div>';
                loadGraphData();
                loadTreeData();
            } catch (e) {
                showToast('Failed to delete', 'error');
            }
        }

        function showCreateNodeModal(parentId) {
            // Remove existing modal if any
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>✨ New Memory Node</h3>
                    <div class="modal-field">
                        <label>Name</label>
                        <input type="text" class="detail-edit-field" id="new-node-name" placeholder="e.g. Work Projects" />
                    </div>
                    <div class="modal-field">
                        <label>Description</label>
                        <textarea class="detail-edit-field" id="new-node-desc" rows="2" placeholder="Brief description of what this node holds…"></textarea>
                    </div>
                    <div class="modal-field">
                        <label>Data (optional)</label>
                        <textarea class="detail-edit-field" id="new-node-data" rows="4" placeholder="Initial memories…"></textarea>
                    </div>
                    <div class="modal-actions">
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                        <button class="modal-btn primary" id="btn-create-node">Create</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) overlay.remove();
            });

            document.getElementById('btn-create-node').addEventListener('click', async () => {
                const name = document.getElementById('new-node-name').value.trim();
                const description = document.getElementById('new-node-desc').value.trim();
                const data = document.getElementById('new-node-data').value;

                if (!name) { showToast('Name is required', 'error'); return; }

                try {
                    const resp = await fetch('/api/graph/node', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name, description, data, parent_id: parentId })
                    });
                    const result = await resp.json();
                    overlay.remove();
                    showToast('Node created', 'success');
                    loadGraphData();
                    loadTreeData();
                    if (result.node) selectNode(result.node.id);
                } catch (e) {
                    showToast('Failed to create node', 'error');
                }
            });

            document.getElementById('new-node-name').focus();
        }

        function showImportDiaryModal(firstTime = false) {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const title = firstTime
                ? '🧠 Build Your Knowledge Graph'
                : '📥 Import from Diary';
            const description = firstTime
                ? 'You have diary entries that can be imported into the new knowledge graph. '
                  + 'This will extract facts from your conversation history and organise them '
                  + 'into a searchable knowledge base. This may take a while for large diaries.'
                : 'Import all existing diary entries into graph memory. Each diary summary '
                  + 'will be processed through the LLM to extract facts and organise them '
                  + 'into the graph. This may take a while for large diaries.';
            const cancelLabel = firstTime ? 'Not Now' : 'Cancel';

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>${title}</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        ${description}
                    </p>
                    <div id="import-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="import-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="import-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0/0</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="import-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="import-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="import-actions">
                        <button class="modal-btn secondary" id="btn-cancel-import">${cancelLabel}</button>
                        <button class="modal-btn primary" id="btn-start-import">Start Import</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-import').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.importing) dismiss();
            });

            document.getElementById('btn-start-import').addEventListener('click', async () => {
                overlay.dataset.importing = 'true';
                document.getElementById('import-progress').style.display = 'block';
                document.getElementById('btn-start-import').disabled = true;
                document.getElementById('btn-start-import').textContent = 'Importing…';

                try {
                    const resp = await fetch('/api/graph/import-diary', { method: 'POST' });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    document.getElementById('import-count').textContent = `0/${msg.total}`;
                                } else if (msg.type === 'progress') {
                                    const pct = Math.round((msg.processed / msg.total) * 100);
                                    document.getElementById('import-bar').style.width = pct + '%';
                                    document.getElementById('import-count').textContent = `${msg.processed}/${msg.total}`;
                                    document.getElementById('import-status').textContent = `Processing ${msg.date}…`;
                                    const log = document.getElementById('import-log');
                                    const icon = msg.error ? '❌' : '📅';
                                    const detail = msg.error ? `error: ${msg.error}` : `${msg.facts} fact${msg.facts !== 1 ? 's' : ''}`;
                                    log.innerHTML += `<div>${icon} ${msg.date} — ${detail}</div>`;
                                    log.scrollTop = log.scrollHeight;
                                } else if (msg.type === 'complete') {
                                    document.getElementById('import-status').textContent = msg.message;
                                    document.getElementById('import-bar').style.width = '100%';
                                    document.getElementById('import-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.importing;
                                    diaryImportDone = true;
                                    loadGraphData();
                                    loadTreeData();
                                    loadStats();
                                    showToast('Diary import complete', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('import-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('import-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.importing;
                                    showToast('Import failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    document.getElementById('import-status').textContent = 'Connection error: ' + e.message;
                    document.getElementById('import-actions').innerHTML = `
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                    `;
                    delete overlay.dataset.importing;
                    showToast('Import failed', 'error');
                }
            });
        }

        function showConsolidateAllModal() {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>🧹 Consolidate All Nodes</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        Re-run the merge prompt over every populated node. Dedupes near-duplicate lines, drops contradictions, folds repeated activities into patterns, and prunes common knowledge. Useful after a prompt change to back-fill the new rules across historical data. Cannot be undone.
                    </p>
                    <div id="consolidate-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="consolidate-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="consolidate-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0 nodes</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="consolidate-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="consolidate-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="consolidate-actions">
                        <button class="modal-btn secondary" id="btn-cancel-consolidate">Cancel</button>
                        <button class="modal-btn primary" id="btn-start-consolidate">Start</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-consolidate').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.consolidating) dismiss();
            });

            document.getElementById('btn-start-consolidate').addEventListener('click', async () => {
                overlay.dataset.consolidating = 'true';
                document.getElementById('consolidate-progress').style.display = 'block';
                document.getElementById('btn-start-consolidate').disabled = true;
                document.getElementById('btn-start-consolidate').textContent = 'Consolidating…';

                try {
                    const resp = await fetch('/api/graph/consolidate-all', { method: 'POST' });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    let nodeCount = 0;
                    let totalNodes = 0;

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    totalNodes = msg.total || 0;
                                    document.getElementById('consolidate-count').textContent = `0 / ${totalNodes} node${totalNodes !== 1 ? 's' : ''}`;
                                } else if (msg.type === 'progress') {
                                    nodeCount++;
                                    const countLabel = totalNodes
                                        ? `${nodeCount} / ${totalNodes} node${totalNodes !== 1 ? 's' : ''}`
                                        : `${nodeCount} node${nodeCount !== 1 ? 's' : ''}`;
                                    document.getElementById('consolidate-count').textContent = countLabel;
                                    document.getElementById('consolidate-status').textContent = `Consolidating ${msg.node}…`;
                                    const log = document.getElementById('consolidate-log');
                                    const arrow = msg.delta < 0 ? '⬇️' : (msg.delta > 0 ? '⬆️' : '➖');
                                    log.innerHTML += `<div>${arrow} ${msg.node} — ${msg.before} → ${msg.after} lines (Δ${msg.delta})</div>`;
                                    log.scrollTop = log.scrollHeight;
                                    // Real progress when the total is known; fall back to indeterminate pulse otherwise.
                                    const pct = totalNodes
                                        ? Math.min(100, Math.round((nodeCount / totalNodes) * 100))
                                        : 50 + (nodeCount % 2) * 50;
                                    document.getElementById('consolidate-bar').style.width = pct + '%';
                                } else if (msg.type === 'complete') {
                                    document.getElementById('consolidate-bar').style.width = '100%';
                                    document.getElementById('consolidate-status').textContent = `Done — ${msg.nodes} node${msg.nodes !== 1 ? 's' : ''}, ${msg.total_before} → ${msg.total_after} lines (Δ${msg.total_delta})`;
                                    document.getElementById('consolidate-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.consolidating;
                                    loadGraphData();
                                    loadTreeData();
                                    loadStats();
                                    showToast('Graph consolidated', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('consolidate-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('consolidate-bar').style.width = '0%';
                                    document.getElementById('consolidate-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.consolidating;
                                    showToast('Consolidation failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    document.getElementById('consolidate-status').textContent = 'Connection error: ' + e.message;
                    // Reset the bar so a half-filled UI doesn't linger next to an error message.
                    document.getElementById('consolidate-bar').style.width = '0%';
                    document.getElementById('consolidate-actions').innerHTML = `
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                    `;
                    delete overlay.dataset.consolidating;
                    showToast('Consolidation failed', 'error');
                }
            });
        }

        function showScrubDeflectionsModal() {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            // Body copy is intentionally explicit about *what stays* and
            // *what is removed*. Users have correctly worried about
            // "clean" buttons quietly destroying data — say exactly what
            // happens so the action is unsurprising.
            overlay.innerHTML = `
                <div class="modal">
                    <h3>🧹 Clean up deflection narration</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 12px; line-height: 1.5;">
                        Asks the chat model to rewrite each old diary entry, removing only
                        sentences that narrate the assistant's failures (for example
                        "the assistant could not…", "offered to search…", "did not have
                        information"). The rest of each entry stays verbatim.
                    </p>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        If a summary is <em>entirely</em> deflection narration it is left as-is rather
                        than emptied. No diary entries are deleted. Requires the chat model
                        to be running. Cannot be undone.
                    </p>
                    <div id="scrub-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="scrub-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="scrub-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0 entries</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="scrub-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="scrub-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="scrub-actions">
                        <button class="modal-btn secondary" id="btn-cancel-scrub">Cancel</button>
                        <button class="modal-btn primary" id="btn-start-scrub">Start</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-scrub').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.scrubbing) dismiss();
            });

            document.getElementById('btn-start-scrub').addEventListener('click', async () => {
                overlay.dataset.scrubbing = 'true';
                document.getElementById('scrub-progress').style.display = 'block';
                // The sweep is one synchronous LLM call per row; on a
                // multi-year diary that's many minutes. The user must
                // be able to bail out without closing the browser. When
                // the AbortController fires, the fetch reader rejects
                // with AbortError and the Flask generator gets a closed
                // pipe on its next yield, ending the sweep cleanly. Any
                // rows already rewritten stay rewritten — partial
                // progress is the design (the bulk sweep is idempotent,
                // so a re-run picks up where this run stopped).
                const controller = new AbortController();
                let processed = 0;
                let totalRows = 0;
                document.getElementById('scrub-actions').innerHTML = `
                    <button class="modal-btn secondary" id="btn-abort-scrub">Abort</button>
                `;
                document.getElementById('btn-abort-scrub').addEventListener('click', () => {
                    controller.abort();
                });

                try {
                    const resp = await fetch('/api/diary/scrub-deflections', {
                        method: 'POST',
                        signal: controller.signal,
                    });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    totalRows = msg.total || 0;
                                    document.getElementById('scrub-count').textContent =
                                        `0 / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`;
                                } else if (msg.type === 'progress') {
                                    processed = msg.processed;
                                    const countLabel = totalRows
                                        ? `${processed} / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`
                                        : `${processed} entr${processed === 1 ? 'y' : 'ies'}`;
                                    document.getElementById('scrub-count').textContent = countLabel;
                                    document.getElementById('scrub-status').textContent = `Cleaning ${msg.date_utc}…`;
                                    const log = document.getElementById('scrub-log');
                                    let icon, detail;
                                    if (msg.error) {
                                        icon = '❌';
                                        detail = `error: ${msg.error}`;
                                    } else if (msg.would_empty) {
                                        // Model wanted to empty the row; kept original instead.
                                        icon = '🛡️';
                                        detail = 'would have emptied · kept original';
                                    } else if (msg.rewritten) {
                                        const delta = (msg.chars_before || 0) - (msg.chars_after || 0);
                                        icon = '🧹';
                                        detail = `rewritten · ${delta} chars removed`;
                                    } else {
                                        icon = '➖';
                                        detail = 'clean';
                                    }
                                    // Use textContent on a constructed node
                                    // rather than innerHTML+=. The values
                                    // come from server-controlled JSON, but
                                    // a corrupted DB row could contain a
                                    // malformed date_utc and the endpoint
                                    // surfaces an exception class name on
                                    // error — neither should be able to
                                    // inject markup into the modal log.
                                    const entry = document.createElement('div');
                                    entry.textContent = `${icon} ${msg.date_utc} — ${detail}`;
                                    log.appendChild(entry);
                                    log.scrollTop = log.scrollHeight;
                                    const pct = totalRows
                                        ? Math.min(100, Math.round((processed / totalRows) * 100))
                                        : 50 + (processed % 2) * 50;
                                    document.getElementById('scrub-bar').style.width = pct + '%';
                                } else if (msg.type === 'complete') {
                                    document.getElementById('scrub-bar').style.width = '100%';
                                    const summary = msg.rows === 0
                                        ? 'No diary entries found.'
                                        : `Done — ${msg.rows_rewritten} of ${msg.rows} entr${msg.rows === 1 ? 'y' : 'ies'} rewritten`
                                          + (msg.rows_would_empty ? ` (${msg.rows_would_empty} kept original to avoid emptying)` : '');
                                    document.getElementById('scrub-status').textContent = summary;
                                    document.getElementById('scrub-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.scrubbing;
                                    loadStats();
                                    loadMemories();
                                    showToast('Diary cleaned', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('scrub-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('scrub-bar').style.width = '0%';
                                    document.getElementById('scrub-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.scrubbing;
                                    showToast('Diary clean failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    if (e.name === 'AbortError') {
                        // User-initiated abort. Partial progress stays
                        // in the DB (the sweep is per-row idempotent and
                        // re-running picks up where this run stopped).
                        const summary = totalRows
                            ? `Stopped — ${processed} of ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'} processed`
                            : 'Stopped before any entries were processed';
                        document.getElementById('scrub-status').textContent = summary;
                        document.getElementById('scrub-actions').innerHTML = `
                            <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                        `;
                        delete overlay.dataset.scrubbing;
                        loadStats();
                        loadMemories();
                        // No toast on user-initiated abort — the modal
                        // status update communicates the partial result.
                    } else {
                        document.getElementById('scrub-status').textContent = 'Connection error: ' + e.message;
                        document.getElementById('scrub-bar').style.width = '0%';
                        document.getElementById('scrub-actions').innerHTML = `
                            <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                        `;
                        delete overlay.dataset.scrubbing;
                        showToast('Diary clean failed', 'error');
                    }
                }
            });
        }

        function showOptimiseTopicsModal() {
            const existing = document.querySelector('.modal-overlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <h3>🏷️ Optimise tags</h3>
                    <p style="color: var(--text-secondary); margin-bottom: 12px; line-height: 1.5;">
                        Uses the chat model to build a normalised tag taxonomy across all diary entries.
                        Near-synonyms are merged into one canonical form (e.g. "cook" and "cooking"
                        both become "cooking"). Compound tags that cover clearly distinct topics are split.
                    </p>
                    <p style="color: var(--text-secondary); margin-bottom: 16px; line-height: 1.5;">
                        Only the tags are changed — diary text is untouched. No entries are deleted.
                        Requires the chat model to be running. Cannot be undone.
                    </p>
                    <div id="optimise-progress" style="display: none;">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                            <span id="optimise-status" style="color: var(--text-secondary); font-size: 0.85em;">Processing…</span>
                            <span id="optimise-count" style="color: var(--accent-primary); font-size: 0.85em; font-family: var(--font-mono);">0 entries</span>
                        </div>
                        <div style="background: var(--bg-tertiary); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div id="optimise-bar" style="background: var(--accent-primary); height: 100%; width: 0%; transition: width 0.3s ease; border-radius: 6px;"></div>
                        </div>
                        <div id="optimise-log" style="margin-top: 12px; max-height: 200px; overflow-y: auto; font-size: 0.8em; font-family: var(--font-mono); color: var(--text-muted); line-height: 1.6;"></div>
                    </div>
                    <div class="modal-actions" id="optimise-actions">
                        <button class="modal-btn secondary" id="btn-cancel-optimise">Cancel</button>
                        <button class="modal-btn primary" id="btn-start-optimise">Start</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const dismiss = () => overlay.remove();
            document.getElementById('btn-cancel-optimise').addEventListener('click', dismiss);
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay && !overlay.dataset.optimising) dismiss();
            });

            document.getElementById('btn-start-optimise').addEventListener('click', async () => {
                overlay.dataset.optimising = 'true';
                document.getElementById('optimise-progress').style.display = 'block';
                document.getElementById('btn-start-optimise').disabled = true;
                document.getElementById('btn-start-optimise').textContent = 'Optimising…';

                try {
                    const resp = await fetch('/api/diary/optimise-topics', { method: 'POST' });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    let processed = 0;
                    let totalRows = 0;

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\\n');
                        buffer = lines.pop();

                        for (const line of lines) {
                            if (!line.trim()) continue;
                            try {
                                const msg = JSON.parse(line);
                                if (msg.type === 'start') {
                                    totalRows = msg.total || 0;
                                    document.getElementById('optimise-status').textContent = 'Building tag taxonomy…';
                                    document.getElementById('optimise-count').textContent =
                                        `0 / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`;
                                } else if (msg.type === 'progress') {
                                    processed = msg.processed;
                                    const countLabel = totalRows
                                        ? `${processed} / ${totalRows} entr${totalRows === 1 ? 'y' : 'ies'}`
                                        : `${processed} entr${processed === 1 ? 'y' : 'ies'}`;
                                    document.getElementById('optimise-count').textContent = countLabel;
                                    document.getElementById('optimise-status').textContent = `Applying to ${msg.date_utc}…`;
                                    const log = document.getElementById('optimise-log');
                                    let icon, detail;
                                    if (msg.error) {
                                        icon = '❌';
                                        detail = `error: ${msg.error}`;
                                    } else if (!msg.topics_changed) {
                                        icon = '➖';
                                        detail = 'no change';
                                    } else {
                                        const oldN = msg.old_topic_count || 0;
                                        const newN = msg.new_topic_count || 0;
                                        icon = '🏷️';
                                        detail = newN < oldN
                                            ? `${oldN} → ${newN} tags (merged)`
                                            : newN > oldN
                                                ? `${oldN} → ${newN} tags (split)`
                                                : `${newN} tag${newN === 1 ? '' : 's'} updated`;
                                    }
                                    const entry = document.createElement('div');
                                    entry.textContent = `${icon} ${msg.date_utc} — ${detail}`;
                                    log.appendChild(entry);
                                    log.scrollTop = log.scrollHeight;
                                    const pct = totalRows
                                        ? Math.min(100, Math.round((processed / totalRows) * 100))
                                        : 50 + (processed % 2) * 50;
                                    document.getElementById('optimise-bar').style.width = pct + '%';
                                } else if (msg.type === 'complete') {
                                    document.getElementById('optimise-bar').style.width = '100%';
                                    let summary;
                                    if (msg.rows === 0) {
                                        summary = 'No diary entries found.';
                                    } else {
                                        const parts = [];
                                        if (msg.rows_changed > 0) {
                                            parts.push(`${msg.rows_changed} of ${msg.rows} entr${msg.rows === 1 ? 'y' : 'ies'} updated`);
                                        } else {
                                            parts.push(`${msg.rows} entr${msg.rows === 1 ? 'y' : 'ies'} checked — all tags already optimal`);
                                        }
                                        if (msg.topics_merged > 0) parts.push(`${msg.topics_merged} tag${msg.topics_merged === 1 ? '' : 's'} merged`);
                                        if (msg.topics_expanded > 0) parts.push(`${msg.topics_expanded} tag${msg.topics_expanded === 1 ? '' : 's'} split`);
                                        summary = 'Done — ' + parts.join(', ');
                                    }
                                    document.getElementById('optimise-status').textContent = summary;
                                    document.getElementById('optimise-actions').innerHTML = `
                                        <button class="modal-btn primary" onclick="this.closest('.modal-overlay').remove()">Done</button>
                                    `;
                                    delete overlay.dataset.optimising;
                                    loadStats();
                                    loadTopics();
                                    loadMemories();
                                    showToast('Tags optimised', 'success');
                                } else if (msg.type === 'error') {
                                    document.getElementById('optimise-status').textContent = 'Error: ' + msg.message;
                                    document.getElementById('optimise-bar').style.width = '0%';
                                    document.getElementById('optimise-actions').innerHTML = `
                                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                                    `;
                                    delete overlay.dataset.optimising;
                                    showToast('Tag optimisation failed', 'error');
                                }
                            } catch (e) { /* skip malformed lines */ }
                        }
                    }
                } catch (e) {
                    document.getElementById('optimise-status').textContent = 'Connection error: ' + e.message;
                    document.getElementById('optimise-bar').style.width = '0%';
                    document.getElementById('optimise-actions').innerHTML = `
                        <button class="modal-btn secondary" onclick="this.closest('.modal-overlay').remove()">Close</button>
                    `;
                    delete overlay.dataset.optimising;
                    showToast('Tag optimisation failed', 'error');
                }
            });
        }

        // Initial load. Chat is the default tab, so the diary lists load
        // lazily when that tab is opened; stats and topics feed the header
        // and sidebar, which are cheap and always visible.
        loadStats();
        loadTopics();
        chatInput.focus();
    </script>
</body>
</html>"""


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
    print("  🔒 That token is minted per launch — the dashboard shows your")
    print("     diary and can act as you, so it is not open to the machine.")
    print("\n  Press Ctrl+C to stop\n")
    print("=" * 60 + "\n")

    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()

