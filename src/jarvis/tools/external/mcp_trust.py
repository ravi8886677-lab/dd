"""Trust decisions about external MCP tools.

An MCP server supplies the name, description and schema of every tool it
offers, and those strings go straight into the model's prompt. That
makes a server's tool list an input channel into Jarvis's reasoning,
which is the substance of two known attacks:

* a **rug pull**, where a tool that was harmless when the user added it
  quietly grows new instructions in a later version;
* a **mislabelled tool**, where a server marks something destructive as
  read-only to slip past whatever gate the host applies.

Two defences here, and they interlock. Fingerprinting establishes trust
on first sight (adding the server to config.json is already the user's
decision) and withholds anything whose definition later changes.
Risk classification then decides whether a call needs a human, treating
the server's own annotations as advisory: they may raise risk freely,
but a downgrade is only honoured for a tool whose fingerprint still
matches, so a rug-pulled tool cannot relabel itself as safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...debug import debug_log


class Risk(Enum):
    """How much damage a tool call could do, as far as we can tell."""

    LOW = "low"          # server explicitly says read-only
    UNKNOWN = "unknown"  # server said nothing useful
    HIGH = "high"        # server says destructive, or reaches the open world


class ConfirmPolicy(Enum):
    """How much the user wants to be asked before an MCP tool runs."""

    OFF = "off"
    DESTRUCTIVE = "destructive"
    UNANNOTATED = "unannotated"
    ALL = "all"


_DEFAULT_POLICY = ConfirmPolicy.DESTRUCTIVE


@dataclass(frozen=True)
class ToolChange:
    """A tool whose definition moved after Jarvis had already accepted it."""

    server_name: str
    tool_name: str
    previous_description: str
    current_description: str
    previous_fingerprint: str
    current_fingerprint: str


def _canonical(value: Any) -> str:
    """Stable JSON for hashing: key order must not move a fingerprint."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return repr(value)


def fingerprint_tool(tool: Dict[str, Any]) -> str:
    """Return a stable digest of everything about a tool the model can read.

    Covers name, description, input schema and annotations, because each
    of those reaches the prompt or the gate. A change to any of them is
    a change to what the user agreed to.
    """
    material = _canonical(
        {
            "name": tool.get("name"),
            "description": tool.get("description"),
            "inputSchema": tool.get("inputSchema"),
            "annotations": tool.get("annotations"),
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _hint(annotations: Any, key: str) -> bool:
    """Read one annotation hint, counting only a real ``True``.

    A string like ``"yes"`` is not a promise of anything; treating it as
    one would let a sloppy or hostile server into the safe bucket.
    """
    if not isinstance(annotations, dict):
        return False
    return annotations.get(key) is True


def classify_risk(tool: Dict[str, Any]) -> Risk:
    """Classify a tool from the annotations its server supplied.

    The MCP spec's own defaults are pessimistic (an unannotated tool is
    nominally destructive and open-world). Applying that literally would
    put every tool from every unannotated server behind a prompt, which
    is a gate people learn to clear without reading. So silence maps to
    ``UNKNOWN`` and the user's policy decides what that is worth.
    """
    annotations = tool.get("annotations")
    if _hint(annotations, "destructiveHint"):
        return Risk.HIGH
    if _hint(annotations, "readOnlyHint"):
        return Risk.LOW
    if _hint(annotations, "openWorldHint"):
        return Risk.HIGH
    return Risk.UNKNOWN


def resolve_policy(cfg: Any) -> ConfirmPolicy:
    """Return the confirmation policy, read only from the user's own config.

    Deliberately ignores anything carried by a server entry or a tool
    annotation. Those are supplied by whatever the user installed, and a
    gate that the gated party can open is not a gate.
    """
    raw = getattr(cfg, "mcp_confirm", None)
    if not isinstance(raw, str):
        return _DEFAULT_POLICY
    try:
        return ConfirmPolicy(raw.strip().lower())
    except ValueError:
        debug_log(
            f"unrecognised mcp_confirm={raw!r}; using {_DEFAULT_POLICY.value}", "mcp"
        )
        return _DEFAULT_POLICY


def requires_confirmation(policy: ConfirmPolicy, risk: Risk) -> bool:
    """Whether a call at this risk needs a human under this policy."""
    if policy is ConfirmPolicy.OFF:
        return False
    if policy is ConfirmPolicy.ALL:
        return True
    if policy is ConfirmPolicy.UNANNOTATED:
        return risk in (Risk.HIGH, Risk.UNKNOWN)
    return risk is Risk.HIGH


def default_trust_path() -> Path:
    """Where the trust record lives, alongside the rest of Jarvis's config."""
    override = os.environ.get("JARVIS_MCP_TRUST_PATH")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("JARVIS_CONFIG_PATH")
    if base:
        return Path(base).expanduser().parent / "mcp_trust.json"
    return Path.home() / ".config" / "jarvis" / "mcp_trust.json"


class TrustStore:
    """Remembers what each server's tools looked like when first accepted."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_trust_path()
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and isinstance(data.get("servers"), dict):
                    return data
                debug_log(
                    f"MCP trust store at {self.path} has an unexpected shape; "
                    "starting a fresh record",
                    "mcp",
                )
        except Exception as e:  # noqa: BLE001
            # A damaged file must not take the daemon down. Tools get
            # re-recorded on this run, which loses change detection once
            # — noisier than failing shut, but a corrupt file is far more
            # likely to be disk trouble than an attack.
            debug_log(f"MCP trust store unreadable ({e}); starting a fresh record", "mcp")
        return {"servers": {}}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._data, handle, indent=2, sort_keys=True)
                try:
                    tmp_path.chmod(0o600)
                except OSError:
                    pass
                os.replace(tmp_path, self.path)
            except Exception:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
        except Exception as e:  # noqa: BLE001
            debug_log(f"could not save MCP trust store: {e}", "mcp")

    def _record_for(self, server_name: str, tool_name: str) -> Optional[Dict[str, Any]]:
        server = self._data.get("servers", {}).get(server_name)
        if not isinstance(server, dict):
            return None
        tools = server.get("tools")
        if not isinstance(tools, dict):
            return None
        record = tools.get(tool_name)
        return record if isinstance(record, dict) else None

    def _write_record(self, server_name: str, tool: Dict[str, Any]) -> None:
        servers = self._data.setdefault("servers", {})
        server = servers.setdefault(server_name, {})
        tools = server.setdefault("tools", {})
        tools[str(tool.get("name"))] = {
            "fingerprint": fingerprint_tool(tool),
            "description": tool.get("description") or "",
            "accepted_at": time.time(),
        }

    def review(
        self, server_name: str, tools: Sequence[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[ToolChange]]:
        """Split a server's tools into those Jarvis will offer and those it will not.

        First sight is recorded and allowed. A tool whose fingerprint no
        longer matches its record is withheld and keeps being withheld
        until ``accept`` is called for it — a change the user never saw
        should not become trusted just because the daemon restarted.
        """
        allowed: List[Dict[str, Any]] = []
        withheld: List[ToolChange] = []
        dirty = False

        for tool in tools:
            name = str(tool.get("name") or "")
            if not name:
                continue
            current = fingerprint_tool(tool)
            record = self._record_for(server_name, name)

            if record is None:
                self._write_record(server_name, tool)
                dirty = True
                allowed.append(tool)
                continue

            if record.get("fingerprint") == current:
                allowed.append(tool)
                continue

            debug_log(
                f"MCP tool '{server_name}__{name}' changed definition since it was "
                "accepted; withholding it until the change is reviewed",
                "mcp",
            )
            withheld.append(
                ToolChange(
                    server_name=server_name,
                    tool_name=name,
                    previous_description=str(record.get("description") or ""),
                    current_description=str(tool.get("description") or ""),
                    previous_fingerprint=str(record.get("fingerprint") or ""),
                    current_fingerprint=current,
                )
            )

        if dirty:
            self._save()
        return allowed, withheld

    def accept(self, server_name: str, tool_name: str, tool: Dict[str, Any]) -> None:
        """Record the current definition as the accepted one."""
        payload = dict(tool)
        payload["name"] = tool_name
        self._write_record(server_name, payload)
        self._save()
        debug_log(f"MCP tool '{server_name}__{tool_name}' change accepted", "mcp")

    def is_trusted(self, server_name: str, tool: Dict[str, Any]) -> bool:
        """Whether this exact definition is the one on record."""
        record = self._record_for(server_name, str(tool.get("name") or ""))
        if record is None:
            return False
        return record.get("fingerprint") == fingerprint_tool(tool)
