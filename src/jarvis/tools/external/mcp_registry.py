"""Client for the official MCP registry at registry.modelcontextprotocol.io.

The curated catalogue is nine servers chosen by hand. The registry lists
thousands, published by whoever wrote them.

Two properties make this fit an offline-first assistant:

- **Metadata only.** Reads need no authentication and send nothing about the
  user. Jarvis asks for a list of packages; it does not tell the registry
  who is asking or what they were doing.
- **Cached.** Results are written to disk and the dashboard reads only from
  that cache. Browsing the directory never touches the network; refreshing
  is a separate, explicit action. Running with no network is supported, so
  a directory that needed one would be a directory that vanished.

What the registry proves is **namespace ownership**: that whoever published
`io.github.acme/thing` controls the `acme` GitHub account, or that the
publisher of `com.example/thing` controls `example.com`. It is an identity
signal, not a safety one. Nothing here reviews the code, and an entry's
presence is not an endorsement.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...debug import debug_log
from ...utils.net_guard import guarded_get

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
_REGISTRY_META_KEY = "io.modelcontextprotocol.registry/official"
_FETCH_TIMEOUT_SEC = 20.0
# One page. A directory is browsed and searched, not paged through by hand,
# and an unbounded crawl of a public API is rude as well as slow.
DEFAULT_LIMIT = 100

# Package registries whose specs the supply-chain guard can pin. Anything
# else is listed but not offered for one-click install, because Jarvis
# cannot construct a launch it would itself accept.
_LAUNCHERS = {
    "npm": ("npx", lambda ident, ver: ["-y", f"{ident}@{ver}"]),
    "pypi": ("uvx", lambda ident, ver: [f"{ident}=={ver}"]),
}


class RegistryUnavailableError(RuntimeError):
    """The registry could not be reached or answered with something unusable."""


def _cache_path() -> Path:
    from ...config import default_config_path

    return default_config_path().parent / "mcp_registry_cache.json"


def _namespace_proof(namespace: str) -> str:
    """How the publisher proved they own this namespace.

    ``io.github.*`` is proven by authenticating to that GitHub account;
    everything else is a reverse-DNS name proven by controlling the domain.
    Both answer "who published this", neither answers "is this safe".
    """
    return "github" if namespace.startswith("io.github.") else "dns"


def _install_from_packages(packages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A pinned launch config, or ``None`` if one cannot be built.

    Returning ``None`` is the honest answer for an unpinned or unfamiliar
    package: the supply-chain guard would refuse the launch, so offering an
    Add button would only produce a config that fails to start.

    The guard itself decides what counts as pinned. A registry `version` is
    a free-text field and holds `latest`, `^1.2.3` and `1.0` as readily as
    `1.2.3`, so checking it is non-empty proves nothing; the only check that
    matches what happens at spawn time is running that check.
    """
    from .mcp_supply_chain import validate_server_launch

    for package in packages or []:
        if not isinstance(package, dict):
            continue
        launcher = _LAUNCHERS.get(str(package.get("registryType", "")).lower())
        if launcher is None:
            continue
        identifier = str(package.get("identifier") or "").strip()
        version = str(package.get("version") or "").strip()
        if not identifier or not version:
            continue
        # The launch below speaks stdio. A package declaring sse or
        # streamable-http would start and then hang waiting for a handshake
        # it never gets, and the connections list would say "no tools" with
        # no clue why. An absent transport means stdio, the registry default.
        transport = package.get("transport")
        if isinstance(transport, dict):
            declared = str(transport.get("type") or "stdio").lower()
            if declared != "stdio":
                debug_log(
                    f"registry package {identifier} speaks {declared}, not stdio", "mcp",
                )
                continue
        command, build_args = launcher
        candidate = {
            "transport": "stdio",
            "command": command,
            "args": build_args(identifier, version),
        }
        try:
            validate_server_launch(identifier, candidate)
        except Exception as e:  # noqa: BLE001
            debug_log(f"registry package {identifier}@{version} is not pinned: {e}", "mcp")
            continue
        return candidate
    return None


def _normalise(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One registry record as a directory entry, or ``None`` to skip it.

    A public API's response shape is not ours to trust, so anything that does
    not fit is dropped rather than being allowed to abort the whole page.
    """
    if not isinstance(record, dict):
        return None
    server = record.get("server")
    if not isinstance(server, dict):
        return None
    name = str(server.get("name") or "").strip()
    if not name:
        return None

    raw_meta = record.get("_meta")
    meta = (raw_meta or {}).get(_REGISTRY_META_KEY) if isinstance(raw_meta, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    # The registry returns every published version of every server. A
    # directory wants the current one.
    if meta.get("isLatest") is False:
        return None
    if str(meta.get("status") or "active").lower() != "active":
        return None

    namespace = name.split("/", 1)[0]
    remotes = server.get("remotes")
    remote_url = None
    if isinstance(remotes, list) and remotes and isinstance(remotes[0], dict):
        url = remotes[0].get("url")
        remote_url = str(url) if url else None

    return {
        "name": name,
        "namespace": namespace,
        "namespace_proof": _namespace_proof(namespace),
        "title": str(server.get("title") or name),
        "description": str(server.get("description") or ""),
        "version": str(server.get("version") or ""),
        "install": _install_from_packages(server.get("packages") or []),
        "remote_url": remote_url,
    }


def _normalise_safely(record: Any) -> Optional[Dict[str, Any]]:
    """``_normalise`` with a net under it.

    One malformed record must not empty the directory, and it must not
    surface as a 500 from an endpoint documented to answer 503.
    """
    try:
        return _normalise(record)
    except Exception as e:  # noqa: BLE001
        debug_log(f"skipping unusable registry record: {e}", "mcp")
        return None


def fetch(limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Read one page of the registry. Raises rather than returning stale data.

    Uses the shared SSRF guard: the URL is fixed, but the guard also
    re-validates redirects, and a public endpoint that starts answering with
    a redirect inward should not be followed.
    """
    try:
        response = guarded_get(
            REGISTRY_URL,
            headers={"Accept": "application/json"},
            timeout=_FETCH_TIMEOUT_SEC,
            params={"limit": limit},
        )
        response.raise_for_status()
        payload = response.json()
        records = payload.get("servers") if isinstance(payload, dict) else None
    except RegistryUnavailableError:
        raise
    except Exception as e:  # noqa: BLE001
        debug_log(f"MCP registry fetch failed: {e}", "mcp")
        raise RegistryUnavailableError(str(e)) from e

    if not isinstance(records, list):
        raise RegistryUnavailableError("registry response had no server list")

    entries = [e for e in (_normalise_safely(r) for r in records) if e is not None]
    debug_log(f"MCP registry returned {len(entries)} current servers", "mcp")
    return entries


def refresh(limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Fetch and cache. The cache is only replaced once a fetch succeeds."""
    entries = fetch(limit)
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_at": time.time(), "entries": entries}, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        # The entries are still good; only the cache write failed.
        debug_log(f"could not write the MCP registry cache: {e}", "mcp")
    return entries


def load_cached() -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """Cached entries and when they were fetched. Never touches the network."""
    path = _cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries")
        if not isinstance(entries, list):
            return [], None
        # The file is on disk and may have been written by a different
        # version or hand-edited. Callers index these by name, so anything
        # without one is dropped here rather than raising downstream.
        usable = [e for e in entries if isinstance(e, dict) and e.get("name")]
        return usable, data.get("fetched_at")
    except FileNotFoundError:
        return [], None
    except Exception as e:  # noqa: BLE001
        debug_log(f"unusable MCP registry cache: {e}", "mcp")
        return [], None


def find_cached(name: str) -> Optional[Dict[str, Any]]:
    """One cached entry by its full registry name."""
    entries, _ = load_cached()
    for entry in entries:
        if entry.get("name") == name:
            return entry
    return None
