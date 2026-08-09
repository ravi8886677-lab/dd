"""Local audit of external MCP tool definitions.

A server's tool descriptions and schemas are read by the model as
instructions, so they are the delivery mechanism for tool poisoning and
cross-server shadowing. This looks for the shapes those attacks need,
on this machine, without sending anything anywhere: the descriptions
name the user's servers and, between them, describe everything their
assistant can do.

Every check is structural, never lexical. Jarvis has to work in any
language, so matching on English phrases would miss an attack written
in Portuguese while flagging an honest Portuguese description. What is
checked instead is text the user cannot see, text imitating the
prompt's own framing, references to credential paths, and one server's
description reaching for another server's tools — all of which mean the
same thing whatever language surrounds them.

This does not replace reading a description. It narrows what is worth
reading.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Sequence


class Severity(Enum):
    WARN = "warn"
    HIGH = "high"


@dataclass(frozen=True)
class Finding:
    """One thing worth a person's attention about a tool definition."""

    server_name: str
    tool_name: str
    code: str
    severity: Severity
    detail: str


# Characters that render as nothing (or reorder what follows) but are read
# by the model. The user reviewing a description cannot see these, which is
# precisely why instructions get hidden in them.
_INVISIBLE = {
    "­",  # soft hyphen
    "​", "‌", "‍",  # zero-width space / non-joiner / joiner
    "‎", "‏",  # LTR / RTL marks
    " ", " ",  # line / paragraph separators
    "‪", "‫", "‬", "‭", "‮",  # bidi embedding/override
    "⁠",  # word joiner
    "⁦", "⁧", "⁨", "⁩",  # bidi isolates
    "﻿",  # zero-width no-break space
}

# Whitespace a legitimate description genuinely uses.
_ALLOWED_CONTROL = {"\t", "\n", "\r"}

# Markup that imitates the structure of a prompt rather than describing a
# tool. Matched as whole constructs so ordinary prose comparing values with
# `<` and `>` does not trip it.
_INSTRUCTION_MARKUP = re.compile(
    r"""
    <\s*/?\s*(?:important|system|assistant|user|instructions?|tool_call)\b[^>]*>
    | \[\s*/?\s*(?:INST|SYS|SYSTEM|IMPORTANT)\s*\]
    | <\|[^|>]{1,40}\|>
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Paths that hold credentials. A tool description has no honest reason to
# name one: reading them is the payload of most published poisoning
# demonstrations.
_SENSITIVE_PATHS = re.compile(
    r"""
    ~?/?\.ssh/ | \bid_rsa\b | \bid_ed25519\b
    | ~?/?\.aws/credentials | ~?/?\.config/gcloud
    | \.env\b | \bcredentials\.json\b
    | ~?/?\.netrc | ~?/?\.npmrc | ~?/?\.docker/config\.json
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Below this length a tool name matches inside ordinary words too often to
# be evidence of anything.
_MIN_SHADOW_NAME_LEN = 4


def _text_of(tool: Dict[str, Any]) -> str:
    """All model-visible text in a tool definition, as one string."""
    parts: List[str] = [str(tool.get("name") or ""), str(tool.get("description") or "")]
    for key in ("inputSchema", "outputSchema", "annotations"):
        value = tool.get(key)
        if value:
            try:
                # ``ensure_ascii`` must stay off: the default would turn a
                # hidden U+202E into the literal text "‮", which is
                # visible and would sail past the invisible-character check.
                parts.append(json.dumps(value, default=str, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                parts.append(str(value))
    return "\n".join(parts)


def _hidden_characters(text: str) -> List[str]:
    """Return the invisible characters present, by Unicode name."""
    found: List[str] = []
    for char in text:
        if char in _ALLOWED_CONTROL:
            continue
        category = unicodedata.category(char)
        if char in _INVISIBLE or category in ("Cf", "Cc"):
            try:
                name = unicodedata.name(char)
            except ValueError:
                name = f"U+{ord(char):04X}"
            if name not in found:
                found.append(name)
    return found


def audit_tool(server_name: str, tool: Dict[str, Any]) -> List[Finding]:
    """Audit one tool definition in isolation."""
    findings: List[Finding] = []
    tool_name = str(tool.get("name") or "?")
    text = _text_of(tool)

    hidden = _hidden_characters(text)
    if hidden:
        findings.append(
            Finding(
                server_name=server_name,
                tool_name=tool_name,
                code="hidden_characters",
                severity=Severity.HIGH,
                detail=(
                    "Contains characters that render as nothing but are read by the "
                    f"model: {', '.join(hidden[:5])}. Instructions hidden this way "
                    "are invisible when you review the description."
                ),
            )
        )

    markup = _INSTRUCTION_MARKUP.search(text)
    if markup:
        findings.append(
            Finding(
                server_name=server_name,
                tool_name=tool_name,
                code="instruction_markup",
                severity=Severity.HIGH,
                detail=(
                    f"Contains {markup.group(0)!r}, which imitates the structure of "
                    "the prompt rather than describing what the tool does."
                ),
            )
        )

    path = _SENSITIVE_PATHS.search(text)
    if path:
        findings.append(
            Finding(
                server_name=server_name,
                tool_name=tool_name,
                code="sensitive_path",
                severity=Severity.HIGH,
                detail=(
                    f"Names the credential path {path.group(0)!r}. A tool description "
                    "has no ordinary reason to mention one."
                ),
            )
        )

    return findings


def audit_server_tools(
    tools_by_server: Dict[str, Sequence[Dict[str, Any]]]
) -> List[Finding]:
    """Audit every tool, including checks that need the whole picture.

    Cross-server shadowing can only be seen from here: it is one
    server's description reaching for a tool belonging to another.
    """
    findings: List[Finding] = []

    owner_of: Dict[str, str] = {}
    for server_name, tools in tools_by_server.items():
        for tool in tools:
            name = str(tool.get("name") or "")
            if len(name) >= _MIN_SHADOW_NAME_LEN:
                owner_of[name] = server_name

    for server_name, tools in tools_by_server.items():
        for tool in tools:
            findings.extend(audit_tool(server_name, tool))

            description = str(tool.get("description") or "")
            for other_name, owner in owner_of.items():
                if owner == server_name:
                    continue
                if re.search(rf"\b{re.escape(other_name)}\b", description):
                    findings.append(
                        Finding(
                            server_name=server_name,
                            tool_name=str(tool.get("name") or "?"),
                            code="cross_server_reference",
                            severity=Severity.HIGH,
                            detail=(
                                f"Describes itself in terms of '{other_name}', which "
                                f"belongs to the '{owner}' server. A server steering "
                                "calls to another server's tools is how shadowing "
                                "works."
                            ),
                        )
                    )

    return findings


def format_findings(findings: Iterable[Finding]) -> List[str]:
    """Render findings as user-facing lines."""
    lines: List[str] = []
    for finding in findings:
        icon = "🛑" if finding.severity is Severity.HIGH else "⚠️"
        lines.append(f"  {icon} {finding.server_name}__{finding.tool_name} [{finding.code}]")
        lines.append(f"       {finding.detail}")
    return lines
