"""Review and accept changes to external MCP tool definitions.

When a server's tool changes after Jarvis accepted it, discovery
withholds that tool rather than quietly handing the model a new set of
instructions. This is how a person looks at what changed and decides.

    python -m jarvis.mcp_trust_cli list
    python -m jarvis.mcp_trust_cli audit
    python -m jarvis.mcp_trust_cli accept <server> <tool>
"""

from __future__ import annotations

import argparse
import difflib
import sys
from typing import Any, Dict, List, Optional

from .config import load_settings
from .debug import debug_log
from .tools.external.mcp_audit import audit_server_tools, format_findings
from .tools.external.mcp_client import MCPClient
from .tools.external.mcp_trust import TrustStore, ToolChange


def _servers(cfg: Any) -> Dict[str, Any]:
    return getattr(cfg, "mcps", {}) or {}


def _live_tools(mcps: Dict[str, Any], server_name: str) -> List[Dict[str, Any]]:
    """Ask one server what it currently offers."""
    client = MCPClient(mcps)
    return client.list_tools(server_name)


def _print_change(change: ToolChange) -> None:
    print(f"  🛑 {change.server_name}__{change.tool_name}")
    diff = difflib.unified_diff(
        change.previous_description.splitlines(),
        change.current_description.splitlines(),
        fromfile="accepted",
        tofile="current",
        lineterm="",
    )
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            print(f"       ➕ {line[1:]}")
        elif line.startswith("-") and not line.startswith("---"):
            print(f"       ➖ {line[1:]}")


def _cmd_list(cfg: Any) -> int:
    mcps = _servers(cfg)
    if not mcps:
        print("📡 No MCP servers configured")
        return 0

    store = TrustStore()
    print(f"🔍 Checking {len(mcps)} MCP server(s) for changed tools...")
    total_changed = 0

    for server_name in mcps:
        try:
            tools = _live_tools(mcps, server_name)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {server_name}: could not reach it ({e})")
            continue

        _, withheld = store.review(server_name, tools)
        if not withheld:
            print(f"  ✅ {server_name}: {len(tools)} tool(s), nothing changed")
            continue

        total_changed += len(withheld)
        print(f"  🛑 {server_name}: {len(withheld)} changed tool(s), withheld")
        for change in withheld:
            _print_change(change)

    if total_changed:
        print()
        print(f"  ⚠️ {total_changed} tool(s) withheld until reviewed")
        print("  ↩️  Allow one with: python -m jarvis.mcp_trust_cli accept <server> <tool>")
    return 0


def _cmd_audit(cfg: Any) -> int:
    """Check every configured server's tool definitions, locally."""
    mcps = _servers(cfg)
    if not mcps:
        print("📡 No MCP servers configured")
        return 0

    print(f"🔎 Auditing tool definitions from {len(mcps)} MCP server(s)...")
    tools_by_server: Dict[str, List[Dict[str, Any]]] = {}
    for server_name in mcps:
        try:
            tools_by_server[server_name] = _live_tools(mcps, server_name)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {server_name}: could not reach it ({e})")

    findings = audit_server_tools(tools_by_server)
    total_tools = sum(len(t) for t in tools_by_server.values())

    if not findings:
        print(f"  ✅ {total_tools} tool(s) checked, nothing suspicious")
        return 0

    print(f"  🛑 {len(findings)} finding(s) across {total_tools} tool(s):")
    for line in format_findings(findings):
        print(line)
    print()
    print("  ℹ️  These are shapes worth a look, not proof of anything.")
    return 1


def _cmd_accept(cfg: Any, server_name: str, tool_name: str) -> int:
    mcps = _servers(cfg)
    if server_name not in mcps:
        print(f"❌ No MCP server named '{server_name}' in your config")
        return 1

    try:
        tools = _live_tools(mcps, server_name)
    except Exception as e:  # noqa: BLE001
        print(f"❌ Could not reach '{server_name}': {e}")
        return 1

    match: Optional[Dict[str, Any]] = next(
        (t for t in tools if str(t.get("name")) == tool_name), None
    )
    if match is None:
        print(f"❌ '{server_name}' offers no tool named '{tool_name}'")
        return 1

    store = TrustStore()
    _, withheld = store.review(server_name, [match])
    if not withheld:
        print(f"✅ {server_name}__{tool_name} already matches what you accepted")
        return 0

    print("📋 Accepting this change:")
    _print_change(withheld[0])
    store.accept(server_name, tool_name, match)
    print(f"  ✅ {server_name}__{tool_name} accepted — it will be offered again")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.mcp_trust_cli",
        description="Review and accept changes to external MCP tool definitions.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="show tools whose definitions changed")
    sub.add_parser("audit", help="check every tool definition for known attack shapes")
    accept = sub.add_parser("accept", help="accept one tool's current definition")
    accept.add_argument("server")
    accept.add_argument("tool")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
        cfg = load_settings()
    except Exception as e:  # noqa: BLE001
        debug_log(f"mcp trust CLI could not load settings: {e}", "mcp")
        print(f"❌ Could not load your config: {e}")
        return 1

    if args.command == "list":
        return _cmd_list(cfg)
    if args.command == "audit":
        return _cmd_audit(cfg)
    return _cmd_accept(cfg, args.server, args.tool)


if __name__ == "__main__":  # pragma: no cover - thin entry point
    sys.exit(main())
