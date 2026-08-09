from __future__ import annotations

import asyncio
import glob as _glob
import os
import shlex as _shlex
import shutil
import sys as _sys
from typing import Any, Dict, Optional, List
from contextlib import asynccontextmanager

from mcp import ClientSession  # type: ignore
from mcp.client.stdio import stdio_client, StdioServerParameters  # type: ignore
from mcp.client.streamable_http import streamablehttp_client  # type: ignore

from ...debug import debug_log
from .mcp_supply_chain import validate_server_launch


class RemoteEndpointError(RuntimeError):
    """A remote MCP server's URL is missing or unsafe to connect to."""


# Transport names that mean "talk to it over HTTP". Streamable HTTP is the
# only network transport in the spec; the older SSE transport is
# deprecated and deliberately absent.
_REMOTE_TRANSPORTS = {"http", "https", "streamable_http", "streamable-http"}

# Hosts where plain HTTP cannot be observed by anything off this machine.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def is_remote_transport(transport: Optional[str]) -> bool:
    """Whether this transport name means a network connection."""
    return str(transport or "").strip().lower() in _REMOTE_TRANSPORTS


def remote_token_key(server_name: str) -> str:
    """Credential-store key holding the bearer token for a remote server."""
    return f"mcp_token:{server_name}"


def _validate_remote_url(server_name: str, url: str) -> None:
    """Raise ``RemoteEndpointError`` unless this URL is safe to send a token to."""
    from urllib.parse import urlparse

    if not url:
        raise RemoteEndpointError(
            f"MCP server '{server_name}' uses an HTTP transport but has no "
            '"url". Add one, e.g. "url": "https://mcp.example.com/mcp".'
        )

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RemoteEndpointError(
            f"MCP server '{server_name}' has an unsupported URL scheme "
            f"'{parsed.scheme}'. Only http and https are used to reach an MCP "
            "server."
        )
    if not parsed.hostname:
        raise RemoteEndpointError(
            f"MCP server '{server_name}' has a URL with no host: {url!r}"
        )
    if parsed.username or parsed.password:
        # These leak into logs, proxies and the Referer of anything the
        # server fetches. A token belongs in the credential store.
        raise RemoteEndpointError(
            f"MCP server '{server_name}' embeds credentials in its URL. Remove "
            "them; store the token with the credential store instead."
        )
    if parsed.scheme == "http" and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise RemoteEndpointError(
            f"MCP server '{server_name}' uses plain http to "
            f"'{parsed.hostname}'. The bearer token would cross the network in "
            "clear text — use https, or point at localhost for a server on "
            "this machine."
        )



class MCPServerSessionError(RuntimeError):
    """Raised when a stateful MCP server's session has been lost.

    Public, stable type that callers can catch to distinguish a
    transient session failure (subprocess crashed, idle timeout
    elapsed mid-call) from a tool-level error returned by ``call_tool``.
    The persistent runtime retries once internally before this surfaces
    to ``MCPClient`` callers.
    """

# Static directories to search when a command isn't on the daemon's PATH.
# macOS GUI-launched processes often miss Homebrew, nvm, fnm, and Volta paths.
_EXTRA_PATH_DIRS: List[str] = [
    "/opt/homebrew/bin",           # Homebrew (Apple Silicon)
    "/usr/local/bin",              # Homebrew (Intel) / manual installs
    os.path.expanduser("~/.volta/bin"),             # Volta
    os.path.expanduser("~/.local/bin"),             # pipx / uvx
]

# Glob patterns for version-managed directories (nvm, fnm).
# Sorted in reverse so the highest version is preferred.
_EXTRA_PATH_GLOBS: List[str] = [
    os.path.expanduser("~/.nvm/versions/node/*/bin"),   # nvm
    os.path.expanduser("~/.fnm/node-versions/*/installation/bin"),  # fnm
]


# Environment variables whose names say they hold a credential. An MCP
# server is third-party code, and inheriting the user's whole shell
# hands every one of them a copy of any cloud key or token exported
# there. A server that genuinely needs one declares it in its own
# ``env`` block, which is merged on top of this and so always wins —
# that block is the sanctioned way to pass a secret to a server.
_SECRET_NAME_PATTERNS = (
    "_TOKEN", "TOKEN_", "_SECRET", "SECRET_", "_KEY", "APIKEY", "API_KEY",
    "_PASSWORD", "PASSWORD_", "_CREDENTIAL", "CREDENTIAL_", "_PASSWD",
    "_PRIVATE_KEY", "_ACCESS_KEY", "_SESSION_TOKEN", "_AUTH",
)

# Names that match the patterns above but carry no secret.
_SECRET_NAME_ALLOWED = {"SSH_AUTH_SOCK", "GPG_AGENT_INFO"}


def _looks_like_a_secret(name: str) -> bool:
    upper = name.upper()
    if upper in _SECRET_NAME_ALLOWED:
        return False
    return any(pattern in upper for pattern in _SECRET_NAME_PATTERNS)


def _inheritable_environment() -> Dict[str, str]:
    """The parent environment minus anything that looks like a credential."""
    kept: Dict[str, str] = {}
    withheld: List[str] = []
    for name, value in os.environ.items():
        if _looks_like_a_secret(name):
            withheld.append(name)
        else:
            kept[name] = value
    if withheld:
        debug_log(
            "withheld credential-shaped variables from MCP server environment: "
            + ", ".join(sorted(withheld)),
            "mcp",
        )
    return kept


def _get_user_shell() -> str:
    """Return the user's login shell, falling back to /bin/bash."""
    return os.environ.get("SHELL", "/bin/bash")


def _resolve_command(command: str) -> str:
    """Resolve a command name to an absolute path.

    First checks the current PATH via ``shutil.which``.  If that fails,
    probes a list of common directories that GUI-launched daemons on macOS
    typically miss (Homebrew, nvm, fnm, Volta, etc.).  As a final fallback,
    spawns the user's login shell to resolve the command.

    Returns the resolved absolute path, or raises ``FileNotFoundError``.
    """
    # Already absolute — just verify it exists
    if os.path.isabs(command):
        if os.path.isfile(command):
            return command
        raise FileNotFoundError(f"MCP server command does not exist: {command}")

    # Try standard PATH first
    found = shutil.which(command)
    if found:
        return found

    # Probe static extra directories
    for d in _EXTRA_PATH_DIRS:
        candidate = os.path.join(d, command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # Probe version-managed directories (nvm, fnm) — prefer highest version
    for pattern in _EXTRA_PATH_GLOBS:
        dirs = sorted(_glob.glob(pattern), reverse=True)
        for d in dirs:
            candidate = os.path.join(d, command)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    # Fallback: ask the user's login shell (catches all custom PATH additions)
    if _sys.platform != "win32":
        try:
            import subprocess
            shell = _get_user_shell()
            # Quote the command so shell metacharacters in a misconfigured
            # ``mcps[*].command`` cannot inject extra commands into the
            # login shell. Defensive — config is user-owned, but keeping
            # the value safe for any path that touches a shell is cheap.
            result = subprocess.run(
                [shell, "-lc", f"which {_shlex.quote(command)}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

    raise FileNotFoundError(
        f"MCP server command not found on PATH: {command}. "
        "Ensure Node.js and npx are installed and available."
    )


class _StdioConnection:
    """Async context manager that wraps a ``stdio_client`` session AND
    owns the ``/dev/null`` file used to suppress the MCP server's stderr.

    The wrapped context manager is built synchronously by
    ``MCPClient._connect_stdio`` so existing call sites and tests that
    construct a connection eagerly continue to work. The wrapper's job
    is to close the devnull handle when the async context exits,
    regardless of how the inner context terminates. Without this the
    devnull handle leaked once per ``_session`` call (i.e. every MCP
    tool invocation), eventually exhausting the process FD limit on
    long-running daemons.
    """

    def __init__(self, inner_cm, errlog) -> None:
        self._cm = inner_cm
        self._errlog = errlog

    async def __aenter__(self):
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._cm.__aexit__(exc_type, exc, tb)
        finally:
            try:
                self._errlog.close()
            except Exception:
                pass


class _RemoteConnection:
    """Adapts ``streamablehttp_client`` to the ``(read, write)`` shape.

    The HTTP transport yields a third element, a callable returning the
    session id. Nothing above this layer uses it, and normalising here
    means the runtime's worker loop does not care which transport it got.
    """

    def __init__(self, inner_cm) -> None:
        self._cm = inner_cm

    async def __aenter__(self):
        streams = await self._cm.__aenter__()
        return streams[0], streams[1]

    async def __aexit__(self, exc_type, exc, tb):
        return await self._cm.__aexit__(exc_type, exc, tb)


class MCPClient:
    """Lightweight manager to connect to external MCP servers and call tools."""

    def __init__(self, mcps_config: Dict[str, Any]) -> None:
        self.server_configs: Dict[str, Dict[str, Any]] = mcps_config or {}

    def _connect(self, server_cfg: Dict[str, Any], server_name: str = "?"):
        """Build a connection for whichever transport this server uses."""
        transport = str(server_cfg.get("transport") or "stdio").strip().lower()
        if transport == "stdio":
            return self._connect_stdio(server_cfg, server_name)
        if is_remote_transport(transport):
            return self._connect_http(server_cfg, server_name)
        raise NotImplementedError(
            f"MCP server '{server_name}' uses transport '{transport}', which is "
            "not supported. Use 'stdio' for a local server or 'http' for a "
            "hosted one."
        )

    def _connect_http(self, server_cfg: Dict[str, Any], server_name: str = "?"):
        """Build an async context manager for the Streamable HTTP transport.

        The URL is validated before anything is opened, so an unsafe
        endpoint never receives a request — in particular never receives
        the bearer token.
        """
        url = str(server_cfg.get("url") or "").strip()
        _validate_remote_url(server_name, url)

        headers: Dict[str, str] = {
            str(k): str(v) for k, v in (server_cfg.get("headers") or {}).items()
        }

        from . import mcp_oauth

        auth = None
        if mcp_oauth.uses_oauth(server_cfg):
            # The provider owns the Authorization header, refreshing the
            # token as it expires. A second one set here would race it.
            headers.pop("Authorization", None)
            auth = mcp_oauth.build_provider(server_name, url)
        else:
            # The credential store is the authority for a static token. A
            # stale Authorization header left in config.json must not win
            # over it, or rotating the token in the keychain would
            # silently do nothing.
            from ...utils import secret_store

            token = secret_store.get_secret(remote_token_key(server_name))
            if token:
                headers["Authorization"] = f"Bearer {token}"

        timeout = server_cfg.get("timeout_sec")
        kwargs: Dict[str, Any] = {"headers": headers or None, "auth": auth}
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            kwargs["timeout"] = float(timeout)

        debug_log(f"connecting to MCP server '{server_name}' at {url}", "mcp")
        return _RemoteConnection(streamablehttp_client(url, **kwargs))

    def _connect_stdio(self, server_cfg: Dict[str, Any], server_name: str = "?"):
        """Build an async context manager for the stdio transport.

        Returns an ``_StdioConnection`` that owns both the stdio_client
        session and the ``/dev/null`` handle used to silence the server
        subprocess's stderr. Path resolution and PATH injection happen
        synchronously here so any ``FileNotFoundError`` surfaces at the
        call site, before the ``async with`` block.

        The supply-chain guard runs first, so a launch that would
        auto-install an unpinned package never reaches the point of
        spawning a subprocess.
        """
        validate_server_launch(server_name, server_cfg)
        command = str(server_cfg.get("command"))
        # Windows compatibility: prefer npx.cmd when requested
        if os.name == "nt" and command.lower() == "npx":
            command = "npx.cmd"
        # Resolve command to an absolute path
        command = _resolve_command(command)
        # Expand user (~) in args for filesystem paths
        raw_args = server_cfg.get("args") or []
        args = [os.path.expanduser(str(a)) if isinstance(a, str) else a for a in raw_args]
        user_env = server_cfg.get("env") or {}
        # The environment is always built explicitly. ``env=None`` does not
        # mean "inherit": the SDK substitutes ``get_default_environment()``,
        # which carries only HOME, PATH, SHELL and TERM. A server launched
        # that way loses proxy settings, NODE_EXTRA_CA_CERTS, registry
        # credentials and every other variable npx and uv consult, and the
        # usual symptom is a launch that hangs until the setup timeout with
        # nothing on stderr to explain it.
        #
        # The resolved command's directory is prepended to PATH so shebangs
        # like #!/usr/bin/env node find sibling binaries.
        cmd_dir = os.path.dirname(command)
        current_path = os.environ.get("PATH", "")
        env = {**_inheritable_environment(), **user_env}
        if cmd_dir and cmd_dir not in current_path.split(os.pathsep):
            env["PATH"] = cmd_dir + os.pathsep + current_path
        params = StdioServerParameters(command=command, args=args, env=env)
        # Suppress MCP server stderr noise (npm warnings, usage banners, etc.)
        # from polluting the daemon's log output.
        # Must use a real file (not StringIO) because the subprocess needs fileno().
        devnull = open(os.devnull, "w")
        # Build the underlying transport CM eagerly so any synchronous
        # construction error closes devnull instead of leaking it. The
        # wrapper guarantees the handle is also closed on every async
        # exit path — this is the actual leak fix.
        try:
            inner = stdio_client(params, errlog=devnull)
        except Exception:
            devnull.close()
            raise
        return _StdioConnection(inner, errlog=devnull)

    @asynccontextmanager
    async def _session(self, server_name: str):
        cfg = self._require_known_cfg(server_name)

        async with self._connect(cfg, server_name) as (read, write):
            # Disable anyio TaskGroup cancellation propagation issues by scoping session strictly here
            async with ClientSession(read, write) as session:
                await session.initialize()
                try:
                    yield session
                finally:
                    # Let nested contexts handle their own shutdown cleanly
                    pass

    async def list_tools_async(self, server_name: str) -> List[Dict[str, Any]]:
        async with self._session(server_name) as session:
            tools_result = await session.list_tools()
            # Extract tools from the ListToolsResult object
            tools_list = getattr(tools_result, "tools", tools_result) if hasattr(tools_result, "tools") else tools_result
            
            return [_tool_to_dict(t) for t in tools_list]

    async def invoke_tool_async(self, server_name: str, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        async with self._session(server_name) as session:
            res = await session.call_tool(tool_name, arguments or {})
            return _result_to_dict(res)

    # Convenience sync wrappers
    def list_tools(self, server_name: str) -> List[Dict[str, Any]]:
        """Discover tools from the named server.

        Routes through the persistent MCP runtime so the same stdio
        session that services discovery also services subsequent
        ``invoke_tool`` calls — avoids paying subprocess startup twice.
        """
        cfg = self._require_known_cfg(server_name)
        from .mcp_runtime import get_runtime, _WorkerDeadError

        runtime = get_runtime()
        try:
            res = runtime.list_tools(server_name, cfg)
        except _WorkerDeadError as e:
            raise MCPServerSessionError(str(e)) from e

        tools_list = getattr(res, "tools", res) if hasattr(res, "tools") else res
        return [_tool_to_dict(t) for t in tools_list]

    def invoke_tool(self, server_name: str, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Invoke a tool against the named server.

        Routes through the persistent MCP runtime so the server's stdio
        session stays alive across calls. Stateful servers (e.g.
        chrome-devtools-mcp, which owns a Chrome process) cannot survive
        the one-shot ``asyncio.run`` pattern: tearing down the session
        kills the subprocess and any children it launched.

        On a transient session loss (subprocess died, idle timeout
        elapsed mid-call) the runtime retries once with a fresh worker.
        If that retry also fails, a ``MCPServerSessionError`` propagates;
        callers can distinguish that from tool-level errors carried in
        the returned dict's ``isError`` field.
        """
        cfg = self._require_known_cfg(server_name)
        from .mcp_runtime import get_runtime, _WorkerDeadError

        runtime = get_runtime()
        try:
            res = runtime.invoke(server_name, cfg, tool_name, arguments)
        except _WorkerDeadError as e:
            raise MCPServerSessionError(str(e)) from e
        return _result_to_dict(res)

    def _require_known_cfg(self, server_name: str) -> Dict[str, Any]:
        """Return the server config, validating that we can reach it.

        Transport support is decided in ``_connect``; this only checks the
        server is configured at all, so both transports take one path.
        """
        cfg = self.server_configs.get(server_name)
        if not cfg:
            raise ValueError(
                f"Unknown MCP server '{server_name}'. Check config.mcps."
            )
        return cfg


def _as_plain_data(value: Any) -> Any:
    """Reduce an SDK model to plain JSON-able data.

    Annotations arrive as pydantic models. They are fingerprinted and
    compared across runs, so they have to reduce to the same structure
    every time rather than to a repr that carries object identity.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, dict):
        return {k: _as_plain_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_plain_data(v) for v in value]
    return value


def _tool_to_dict(tool: Any) -> Dict[str, Any]:
    """Convert an SDK ``Tool`` to the internal dict shape.

    ``annotations`` drives the confirmation gate and ``outputSchema``
    describes structured results, so both travel with the tool rather
    than being discarded at discovery.
    """
    return {
        "name": getattr(tool, "name", None),
        "description": getattr(tool, "description", None),
        "inputSchema": getattr(tool, "inputSchema", None),
        "outputSchema": _as_plain_data(getattr(tool, "outputSchema", None)),
        "annotations": _as_plain_data(getattr(tool, "annotations", None)),
    }


def _result_to_dict(res: Any) -> Dict[str, Any]:
    """Convert an MCP ``call_tool`` response object to the internal dict shape."""
    raw_content = getattr(res, "content", None)
    is_error = getattr(res, "isError", False)
    meta = getattr(res, "meta", None)
    return {
        "content": raw_content,
        "text": _flatten_content(raw_content),
        "isError": is_error,
        "meta": meta,
    }


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (_flatten_content(item) for item in content)))
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        if content.get("type") == "text" and "data" in content:
            return str(content.get("data") or "")
        try:
            return str(content)
        except Exception:
            return ""
    # Real MCP SDK content blocks (mcp.types.TextContent models) are
    # pydantic models, not dicts — pull `.text` directly. Falling through to
    # str(model) instead produces a repr like "type='text' text='...'
    # annotations=None meta=None" rather than the actual text.
    text_attr = getattr(content, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    try:
        return str(content)
    except Exception:
        return ""


