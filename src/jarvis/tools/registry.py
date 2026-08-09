from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List
import sys
import re
import requests
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os

from .builtin.screenshot import ScreenshotTool
from .builtin.web_search import WebSearchTool
from .builtin.local_files import LocalFilesTool
from .builtin.fetch_web_page import FetchWebPageTool
from .builtin.nutrition.log_meal import LogMealTool
from .builtin.nutrition.fetch_meals import FetchMealsTool
from .builtin.nutrition.delete_meal import DeleteMealTool
from .builtin.refresh_mcp_tools import RefreshMCPToolsTool
from .builtin.computer_use import ComputerUseTool
from .builtin.open_app import OpenAppTool
from .builtin.weather import WeatherTool
from .builtin.stop import StopTool
from .builtin.tool_search import ToolSearchTool
from .types import ToolExecutionResult
from ..config import Settings
from .external.mcp_client import MCPClient
from .external.mcp_gate import GateOutcome, check_confirmation
from .external.mcp_trust import (
    TrustStore,
    classify_risk,
    requires_confirmation,
    resolve_policy,
)

# Jarvis's own argument on MCP tool calls, never forwarded to a server.
_CONFIRMATION_ARG = "confirmation_code"
from ..debug import debug_log


# Registry of all builtin tools
BUILTIN_TOOLS = {
    "screenshot": ScreenshotTool(),
    "webSearch": WebSearchTool(),
    "localFiles": LocalFilesTool(),
    "fetchWebPage": FetchWebPageTool(),
    "logMeal": LogMealTool(),
    "fetchMeals": FetchMealsTool(),
    "deleteMeal": DeleteMealTool(),
    "refreshMCPTools": RefreshMCPToolsTool(),
    "getWeather": WeatherTool(),
    "openApp": OpenAppTool(),
    "computerUse": ComputerUseTool(),
    "stop": StopTool(),
    "toolSearchTool": ToolSearchTool(),
}

# Global MCP tools cache
_mcp_tools_cache: Dict[str, "ToolSpec"] = {}
_mcp_tools_cache_lock = threading.Lock()
_mcp_config_cache: Dict[str, Any] = {}


def initialize_mcp_tools(mcps_config: Dict[str, Any], verbose: bool = True) -> Tuple[Dict[str, "ToolSpec"], Dict[str, str]]:
    """
    Initialize MCP tools cache at startup.

    Args:
        mcps_config: MCP server configuration
        verbose: Whether to print status messages

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    global _mcp_tools_cache, _mcp_config_cache

    with _mcp_tools_cache_lock:
        _mcp_config_cache = mcps_config or {}
        _mcp_tools_cache, errors = discover_mcp_tools(mcps_config)

        if verbose and _mcp_tools_cache:
            debug_log(f"MCP tools cache initialized with {len(_mcp_tools_cache)} tools", "mcp")

        return _mcp_tools_cache.copy(), errors


def discover_and_report_mcp_tools(mcps_config: Dict[str, Any]) -> Dict[str, "ToolSpec"]:
    """Discover MCP tools at startup and print a per-server summary.

    The shared startup path for every front end (voice daemon, text
    chat): one line per configured server saying how many tools it
    offered or why it offered none. Never raises — a broken MCP server
    must not stop the assistant from starting.
    """
    if not mcps_config:
        print("📡 No MCP servers configured", flush=True)
        return {}

    print(f"📡 Discovering MCP tools from {len(mcps_config)} server(s)...", flush=True)
    try:
        mcp_tools, mcp_errors = initialize_mcp_tools(mcps_config, verbose=False)
    except Exception as e:
        debug_log(f"MCP discovery failed: {e}", "mcp")
        print(f"  ⚠️ MCP discovery failed: {e}", flush=True)
        return {}

    counts: Dict[str, int] = {}
    for tool_name in mcp_tools.keys():
        if "__" in tool_name:
            server_name = tool_name.split("__")[0]
            counts[server_name] = counts.get(server_name, 0) + 1

    for server_name in mcps_config.keys():
        count = counts.get(server_name, 0)
        if count > 0:
            print(f"  ✅ {server_name}: {count} tools available", flush=True)
        elif server_name in mcp_errors:
            print(f"  ❌ {server_name}: {mcp_errors[server_name]}", flush=True)
        else:
            print(f"  ⚠️ {server_name}: no tools discovered", flush=True)

    debug_log(f"MCP tools cached: {len(mcp_tools)} total", "mcp")
    return mcp_tools


def get_cached_mcp_tools() -> Dict[str, "ToolSpec"]:
    """Get cached MCP tools without rediscovering."""
    with _mcp_tools_cache_lock:
        return _mcp_tools_cache.copy()


def refresh_mcp_tools(verbose: bool = True) -> Tuple[Dict[str, "ToolSpec"], Dict[str, str]]:
    """
    Refresh MCP tools cache by rediscovering all tools.

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    global _mcp_tools_cache

    with _mcp_tools_cache_lock:
        if not _mcp_config_cache:
            debug_log("No MCP config cached, skipping refresh", "mcp")
            return {}, {}

        if verbose:
            print("🔄 Refreshing MCP tools...", flush=True)

        _mcp_tools_cache, errors = discover_mcp_tools(_mcp_config_cache)

        if verbose:
            print(f"  ✅ Found {len(_mcp_tools_cache)} MCP tools", flush=True)

        debug_log(f"MCP tools cache refreshed with {len(_mcp_tools_cache)} tools", "mcp")
        return _mcp_tools_cache.copy(), errors


def is_mcp_cache_initialized() -> bool:
    """Check if MCP tools cache has been initialized."""
    with _mcp_tools_cache_lock:
        return len(_mcp_config_cache) > 0 or len(_mcp_tools_cache) > 0



# ToolSpec for MCP compatibility
@dataclass(frozen=True)
class ToolSpec:
    name: str  # canonical tool identifier (camelCase)
    description: str  # Human-readable description (matches MCP format)
    inputSchema: Optional[Dict[str, Any]] = None  # JSON Schema for arguments (matches MCP format)
    # Server-supplied MCP annotations (readOnlyHint and friends). Drives
    # the confirmation gate; ``None`` for built-in tools.
    annotations: Optional[Dict[str, Any]] = None


def discover_mcp_tools(mcps_config: Dict[str, Any]) -> Tuple[Dict[str, ToolSpec], Dict[str, str]]:
    """Discover all tools from configured MCP servers and create ToolSpec entries for them.

    Returns:
        Tuple of (discovered_tools, errors) where errors maps server name to error message.
    """
    if not mcps_config:
        return {}, {}

    try:
        client = MCPClient(mcps_config)
        discovered_tools = {}
        errors: Dict[str, str] = {}

        trust_store = TrustStore()

        for server_name in mcps_config.keys():
            try:
                tools = client.list_tools(server_name)
                # A tool whose definition moved since the user accepted it
                # is withheld rather than offered: the description reaches
                # the model as instructions, so a silent edit is an edit to
                # what Jarvis was told to do.
                tools, withheld = trust_store.review(server_name, tools)
                for change in withheld:
                    print(
                        f"  🛑 {server_name}: withholding '{change.tool_name}' — its "
                        "description changed since you accepted it",
                        flush=True,
                    )
                    print(
                        f"     ↩️  Review and allow: python -m jarvis.mcp_trust_cli "
                        f"accept {server_name} {change.tool_name}",
                        flush=True,
                    )

                for tool_info in tools:
                    tool_name = tool_info.get("name")
                    if not tool_name:
                        continue

                    # Create a unique tool name: server__toolname
                    full_tool_name = f"{server_name}__{tool_name}"

                    # Create a ToolSpec for this MCP tool
                    description = tool_info.get("description", f"Tool from {server_name} MCP server")
                    input_schema = tool_info.get("inputSchema", {"type": "object", "properties": {}, "required": []})
                    discovered_tools[full_tool_name] = ToolSpec(
                        name=full_tool_name,
                        description=description,
                        inputSchema=input_schema,
                        annotations=tool_info.get("annotations"),
                    )

            except BaseException as e:
                # ExceptionGroups (from anyio TaskGroup) wrap the real cause;
                # extract the first sub-exception for a useful error message.
                cause = e
                if hasattr(e, "exceptions") and e.exceptions:
                    cause = e.exceptions[0]
                debug_log(f"Failed to discover tools from MCP server '{server_name}': {cause}", "mcp")
                errors[server_name] = str(cause)
                continue

        return discovered_tools, errors

    except Exception as e:
        debug_log(f"Failed to discover MCP tools: {e}", "mcp")
        return {}, {"_global": str(e)}


def generate_tools_json_schema(allowed_tools: Optional[List[str]] = None, mcp_tools: Optional[Dict[str, ToolSpec]] = None) -> List[Dict[str, Any]]:
    """
    Generate tools in OpenAI-compatible JSON schema format for native tool calling.

    This format is supported by Ollama for models with native tool calling support
    (Llama 3.1+, Llama 3.2, Qwen 3, Mistral, etc.).

    Returns a list of tool definitions in this format:
    [
        {
            "type": "function",
            "function": {
                "name": "toolName",
                "description": "Tool description",
                "parameters": {
                    "type": "object",
                    "properties": {...},
                    "required": [...]
                }
            }
        }
    ]
    """
    names = list(allowed_tools or list(BUILTIN_TOOLS.keys()))
    tools: List[Dict[str, Any]] = []

    # Add built-in tools
    for tool_name in names:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            continue

        tool_def = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema or {"type": "object", "properties": {}, "required": []},
            }
        }
        tools.append(tool_def)

    # Add discovered MCP tools
    if mcp_tools:
        for tool_name, spec in mcp_tools.items():
            if tool_name in names:  # Only include if allowed
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": _with_confirmation_field(spec.inputSchema),
                    }
                }
                tools.append(tool_def)

    return tools


def _server_owns_confirmation_arg(spec: Optional["ToolSpec"]) -> bool:
    """Whether the server's own schema already declares ``confirmation_code``.

    A server is entitled to a parameter of that name (an OTP tool, say).
    Where it claims one, Jarvis neither advertises its own nor strips the
    value, so the user's real code reaches the server instead of being
    eaten and compared against the gate's four digits.
    """
    schema = getattr(spec, "inputSchema", None)
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    return isinstance(properties, dict) and _CONFIRMATION_ARG in properties


def _with_confirmation_field(input_schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Advertise the optional ``confirmation_code`` argument on an MCP tool.

    Offered on every MCP tool rather than only the gated ones: which
    tools are gated depends on the user's policy and on annotations that
    can change between runs, and a model that has no way to express an
    approval cannot relay one the user just gave. The field is optional,
    so it never blocks an ungated call, and it is stripped before the
    call reaches the server.
    """
    schema = dict(input_schema or {"type": "object", "properties": {}, "required": []})
    properties = dict(schema.get("properties") or {})
    if _CONFIRMATION_ARG in properties:
        # The server declares this name itself; overwriting it would hide
        # the real parameter's type and meaning from the model.
        return schema
    properties[_CONFIRMATION_ARG] = {
        "type": "string",
        "description": (
            "Only for tools that ask for one. The code shown on the user's screen; "
            "without it such a call is proposed, not run."
        ),
    }
    schema["properties"] = properties
    return schema


def generate_tools_description(allowed_tools: Optional[List[str]] = None, mcp_tools: Optional[Dict[str, ToolSpec]] = None) -> str:
    """Produce a compact tool help string for the system prompt using OpenAI standard format."""
    names = list(allowed_tools or list(BUILTIN_TOOLS.keys()))
    lines: List[str] = []
    lines.append("Tool-use protocol: Use the tool_calls field in your response:")
    lines.append('tool_calls: [{"id": "call_<id>", "type": "function", "function": {"name": "<toolName>", "arguments": "<json_string>"}}]')
    lines.append("\nAvailable tools and when to use them:")

    # Add built-in tools
    for tool_name in names:
        tool = BUILTIN_TOOLS.get(tool_name)
        if not tool:
            continue
        lines.append(f"\n{tool.name}: {tool.description}")
        if tool.inputSchema:
            # Extract a simple parameter summary from the JSON schema
            props = tool.inputSchema.get("properties", {})
            required = tool.inputSchema.get("required", [])
            param_descriptions = []
            for prop_name, prop_def in props.items():
                prop_type = prop_def.get("type", "any")
                is_required = prop_name in required
                req_marker = " (required)" if is_required else ""
                param_descriptions.append(f"{prop_name}: {prop_type}{req_marker}")
            if param_descriptions:
                lines.append(f"Input: {', '.join(param_descriptions)}")

    # Add discovered MCP tools
    if mcp_tools:
        for tool_name, spec in mcp_tools.items():
            if tool_name in names:  # Only include if allowed
                lines.append(f"\n{spec.name}: {spec.description}")
                # Same schema the native path advertises, so a model on the
                # text fallback is told about ``confirmation_code`` too.
                # Without it the gate's PROPOSED message asks for an
                # argument the tool's own parameter list does not mention,
                # and a small model that follows the list never approves.
                described_schema = _with_confirmation_field(spec.inputSchema)
                if described_schema:
                    # Extract a simple parameter summary from the JSON schema
                    props = described_schema.get("properties", {})
                    required = described_schema.get("required", [])
                    param_descriptions = []
                    for prop_name, prop_def in props.items():
                        prop_type = prop_def.get("type", "any")
                        is_required = prop_name in required
                        req_marker = " (required)" if is_required else ""
                        param_descriptions.append(f"{prop_name}: {prop_type}{req_marker}")
                    if param_descriptions:
                        lines.append(f"Input: {', '.join(param_descriptions)}")

    return "\n".join(lines)

def _normalize_time_range(args: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    since: Optional[str] = None
    until: Optional[str] = None
    if args and isinstance(args, dict):
        try:
            since_val = args.get("since_utc")
            since = str(since_val) if since_val else None
        except Exception:
            since = None
        try:
            until_val = args.get("until_utc")
            until = str(until_val) if until_val else None
        except Exception:
            until = None
    if since is None and until is None:
        # Default last 24h
        return (now - timedelta(days=1)).isoformat(), now.isoformat()
    if since is None and until is not None:
        # backfill 24h prior to until
        try:
            until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except Exception:
            until_dt = now
        return (until_dt - timedelta(days=1)).isoformat(), until_dt.isoformat()
    if since is not None and until is None:
        return since, now.isoformat()
    return since or (now - timedelta(days=1)).isoformat(), until or now.isoformat()


def _gate_mcp_call(
    cfg: Settings,
    full_tool_name: str,
    server_name: str,
    mcp_tool_name: str,
    arguments: Dict[str, Any],
    supplied_code: Optional[str],
) -> Optional[ToolExecutionResult]:
    """Apply the confirmation gate, or return ``None`` to let the call run.

    The risk comes from the server's own annotations, which are advisory:
    they can raise risk freely, but a read-only claim is only honoured
    for a tool whose definition still matches what the user accepted. A
    rug-pulled tool is withheld at discovery, so one that relabels itself
    as harmless mid-conversation cannot talk its way past the gate.
    """
    policy = resolve_policy(cfg)
    spec = get_cached_mcp_tools().get(full_tool_name)
    risk = classify_risk({"annotations": getattr(spec, "annotations", None)})

    if not requires_confirmation(policy, risk):
        return None

    outcome, message = check_confirmation(
        server_name, mcp_tool_name, arguments, supplied_code
    )
    if outcome is GateOutcome.APPROVED:
        return None
    if outcome is GateOutcome.PROPOSED:
        return ToolExecutionResult(success=True, reply_text=message)
    return ToolExecutionResult(success=False, reply_text=None, error_message=message)


def run_tool_with_retries(
    db,
    cfg: Settings,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    system_prompt: str,
    original_prompt: str,
    redacted_text: str,
    max_retries: int = 1,
    language: Optional[str] = None,
) -> ToolExecutionResult:
    # Normalize tool name to canonical camelCase
    raw_name = (tool_name or "").strip()
    name = raw_name

    # Check if tool name is a discovered MCP tool (server__toolname format)
    if "__" in raw_name:
        server_name, mcp_tool_name = raw_name.split("__", 1)
        mcps_config = getattr(cfg, "mcps", {})
        if mcps_config and server_name in mcps_config:
            try:
                if MCPClient is None:
                    return ToolExecutionResult(success=False, reply_text=None, error_message="MCP client not available. Install 'mcp' package.")

                # Only tools that survived discovery may run. A tool the
                # trust store withheld is absent from the cache, and
                # without this check the model could still call it by
                # name — from its own conversation history, or because
                # another server's description named it — which would
                # walk straight past the withholding.
                cached = get_cached_mcp_tools()
                if is_mcp_cache_initialized() and raw_name not in cached:
                    debug_log(
                        f"refused MCP tool '{raw_name}': not among the tools "
                        "discovery offered",
                        "mcp",
                    )
                    return ToolExecutionResult(
                        success=False,
                        reply_text=None,
                        error_message=(
                            f"'{raw_name}' is not available. Its definition may have "
                            "changed since it was accepted, in which case it is "
                            "withheld until reviewed. Tell the user, and do not "
                            "retry it."
                        ),
                    )

                # ``confirmation_code`` is Jarvis's own argument. Strip it
                # before the call so it is never forwarded to the server —
                # unless the server itself declares a property by that
                # name, in which case it is the server's argument and
                # Jarvis never injected one.
                arguments = dict(tool_args or {})
                spec = cached.get(raw_name)
                if _server_owns_confirmation_arg(spec):
                    supplied_code = None
                else:
                    supplied_code = arguments.pop(_CONFIRMATION_ARG, None)

                gate_result = _gate_mcp_call(
                    cfg, raw_name, server_name, mcp_tool_name, arguments, supplied_code
                )
                if gate_result is not None:
                    return gate_result

                client = MCPClient(mcps_config)
                result = client.invoke_tool(server_name=server_name, tool_name=mcp_tool_name, arguments=arguments)
                is_error = bool(result.get("isError", False))
                text = result.get("text") or None
                return ToolExecutionResult(success=(not is_error), reply_text=text, error_message=(text if is_error else None))
            except Exception as e:
                detail = str(e) or type(e).__name__
                return ToolExecutionResult(success=False, reply_text=None, error_message=f"MCP tool '{raw_name}' error: {detail}")

    # Friendly user print helper (non-debug only)
    def _user_print(message: str) -> None:
        # 4-space indent: tool messages happen INSIDE an agentic-loop
        # turn. The turn header (`  🔁 Turn N/M`) sits at 2 spaces, so
        # per-tool activity nests one level deeper for visual hierarchy.
        if not getattr(cfg, "voice_debug", False):
            try:
                print(f"    {message}")
            except Exception:
                pass

    # Check builtin tools first
    if name in BUILTIN_TOOLS:
        tool = BUILTIN_TOOLS[name]
        return tool.execute(
            db=db,
            cfg=cfg,
            tool_args=tool_args,
            system_prompt=system_prompt,
            original_prompt=original_prompt,
            redacted_text=redacted_text,
            max_retries=max_retries,
            user_print=_user_print,
            language=language,
        )

    # Unknown tool
    debug_log(f"unknown tool requested: {tool_name}", "tools")
    return ToolExecutionResult(success=False, reply_text=None, error_message=f"Unknown tool: {tool_name}")


