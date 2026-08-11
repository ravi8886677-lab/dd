"""
Tool selection — pick relevant tools for a user query.

Strategies (ToolSelectionStrategy enum):
  - ALL:       return every tool (no filtering)
  - KEYWORD:   score tools by keyword overlap with the query
  - EMBEDDING: rank tools by cosine similarity of embeddings
  - LLM:       ask a lightweight LLM call to choose tools
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Dict, List, Optional, TYPE_CHECKING

from ..debug import debug_log
from ..llm import LLMBackend

if TYPE_CHECKING:
    from .base import Tool
    from .registry import ToolSpec


class ToolSelectionStrategy(Enum):
    ALL = "all"
    KEYWORD = "keyword"
    EMBEDDING = "embedding"
    LLM = "llm"


# Tools that must always be available regardless of selection strategy.
_ALWAYS_INCLUDED = {"stop"}

# Minimum number of tools to return from similarity-based strategies.
# Prevents overly aggressive filtering that would leave the model with nothing useful.
_MIN_SELECTED = 3

# Maximum number of tools to return from similarity-based strategies. A high
# cap keeps the prompt small enough that small models (gemma4:e2b) don't drift
# to their training priors under token pressure. When the top-ranked tool is a
# clear winner and the rest are noise, we want 3–5 tools, not 29.
_MAX_SELECTED = 8

# Relative similarity threshold for embedding strategy.
# A tool is kept when its cosine similarity >= top_score * _RELATIVE_THRESHOLD.
# This adapts to the actual score distribution rather than using a fixed cutoff
# that either passes everything (too low) or nothing (too high).
#
# Set high (0.97) because nomic-embed-text gives a very high baseline
# similarity across all tools (most pairs land in the 0.6–0.8 range regardless
# of semantic overlap). A looser threshold like 0.85 lets nearly every tool
# through, defeating the filter. 0.97 keeps only the tools genuinely close to
# the top match.
_RELATIVE_THRESHOLD = 0.97

# Hard cap on tools returned by the LLM router. Small routing models
# (gemma4:e2b and similar) sometimes echo the entire catalogue; the cap
# guarantees the downstream prompt stays compact regardless.
_LLM_MAX_SELECTED = 5

# How many candidates retrieval hands the router when the catalogue is
# bigger than this. The router is good at discriminating between a few
# plausible tools and bad at reading hundreds of descriptions, so it is
# given a shortlist rather than the catalogue. Wider than the 5 it may
# return, because retrieval ranks on similarity alone and the router needs
# room to overrule it. A catalogue at or below this size is handed over
# whole: there would be nothing to remove, and the embedding call would
# buy nothing.
_RERANK_CANDIDATES = 15

# Common English stop-words excluded from keyword matching.
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "i", "me", "my",
    "you", "your", "he", "she", "it", "we", "they", "them", "this",
    "that", "what", "which", "who", "when", "where", "how", "not", "no",
    "so", "if", "or", "and", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "into", "about", "up", "out",
    "off", "over", "just", "also", "very", "too", "some", "any", "all",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")

# MCP tools are registered as ``server__tool``; see ``run_tool_with_retries``.
_MCP_NAME_SEP = "__"

# Tool summaries embed to the same vector every time, so they are computed
# once per catalogue rather than once per turn. Keyed by (model, summary):
# the model matters because vectors from different models are not
# comparable, and the summary matters because an edited description must
# not keep scoring against its old text.
_EMBED_CACHE: Dict[tuple, List[float]] = {}
_EMBED_CACHE_MAX = 2048


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenise(text: str) -> List[str]:
    """Lowercase and split on non-alphanumeric boundaries, removing stop-words."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP_WORDS]


def _build_tool_keywords(name: str, description: str) -> set:
    """Build a keyword set from tool name (camelCase-split) and description."""
    name_tokens = _TOKEN_RE.findall(_CAMEL_RE.sub(" ", name).lower())
    desc_tokens = _tokenise(description)
    return set(name_tokens) | set(desc_tokens)


def _tool_summary(name: str, description: str, server: str = "") -> str:
    """The text embedded to represent a tool.

    Retrieval quality is bounded by what goes in this string. A bare name
    and one-line description leave tools that do unrelated jobs sitting on
    top of each other, which is what makes a similarity cutoff useless.

    So the server is named too. People ask for tools by the product they
    belong to ("use higgsfield to make a clip"), and that word appears
    nowhere in the tool's own name or description. MCP tools are
    registered as ``server__tool``, so it can be recovered from the name
    when the caller does not supply it.
    """
    origin = server
    bare_name = name
    if _MCP_NAME_SEP in name:
        derived, bare_name = name.split(_MCP_NAME_SEP, 1)
        origin = origin or derived

    readable_name = _CAMEL_RE.sub(" ", bare_name).replace("_", " ").lower()
    subject = f"{readable_name} from {origin}" if origin else readable_name
    return f"{subject}: {description}"


def _embed_tool_text(
    embedding_backend: LLMBackend,
    text: str,
    model: str,
    timeout_sec: float,
    expect_dim: int = 0,
) -> Optional[List[float]]:
    """Embed a tool summary, reusing the vector when the text is unchanged.

    Tool text is fixed for the life of a catalogue while queries are not,
    so embedding it per turn makes routing cost scale with tools times
    turns. Keying on the summary means an edited description re-embeds by
    itself, without anything having to remember to invalidate.

    ``expect_dim`` guards the case the key cannot see. Two backends can
    serve the same model name and return different dimensions, so pointing
    ``embedding_provider`` somewhere new would otherwise score this turn's
    query against the previous provider's vectors. A cached vector of the
    wrong width is treated as a miss.
    """
    key = (model, text)
    cached = _EMBED_CACHE.get(key)
    if cached is not None:
        if expect_dim and len(cached) != expect_dim:
            debug_log(
                "Embedding tool selection: cached vectors have the wrong width, "
                "re-embedding the catalogue",
                "planning",
            )
            _EMBED_CACHE.clear()
        else:
            return cached

    vector = embedding_backend.embed(text, model, timeout_sec=timeout_sec)
    if vector is None:
        return None

    # Summaries are stable, so this bound is reached only by catalogues
    # churning through servers. Clearing wholesale beats tracking ages.
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        _EMBED_CACHE.clear()
    _EMBED_CACHE[key] = vector
    return vector


def _ensure_always_included(
    selected: List[str],
    builtin_tools: Dict[str, "Tool"],
    mcp_tools: Dict[str, "ToolSpec"],
) -> List[str]:
    """Append always-included tools if missing."""
    for t in _ALWAYS_INCLUDED:
        if t not in selected and (t in builtin_tools or t in mcp_tools):
            selected.append(t)
    return selected


def _all_tool_names(
    builtin_tools: Dict[str, "Tool"],
    mcp_tools: Dict[str, "ToolSpec"],
) -> List[str]:
    return list(builtin_tools.keys()) + list(mcp_tools.keys())


# ---------------------------------------------------------------------------
# Strategy: keyword
# ---------------------------------------------------------------------------

def _select_keyword(
    query: str,
    builtin_tools: Dict[str, "Tool"],
    mcp_tools: Dict[str, "ToolSpec"],
) -> List[str]:
    """Score tools by keyword overlap; return those with score > 0."""
    query_tokens = set(_tokenise(query))
    if not query_tokens:
        return _all_tool_names(builtin_tools, mcp_tools)

    scored: List[tuple] = []

    for name, tool in builtin_tools.items():
        kw = _build_tool_keywords(name, tool.description)
        score = len(query_tokens & kw)
        scored.append((name, score))

    for name, spec in mcp_tools.items():
        kw = _build_tool_keywords(name, spec.description)
        score = len(query_tokens & kw)
        scored.append((name, score))

    matched = [name for name, score in scored if score > 0]
    matched = _ensure_always_included(matched, builtin_tools, mcp_tools)

    if len(matched) <= len(_ALWAYS_INCLUDED):
        debug_log("Keyword tool selection found no matches, falling back to all tools", "planning")
        return _all_tool_names(builtin_tools, mcp_tools)

    debug_log(f"Keyword tool selection: {len(matched)}/{len(builtin_tools) + len(mcp_tools)} tools selected", "planning")
    return matched


# ---------------------------------------------------------------------------
# Strategy: embedding
# ---------------------------------------------------------------------------

def _select_embedding(
    query: str,
    builtin_tools: Dict[str, "Tool"],
    mcp_tools: Dict[str, "ToolSpec"],
    embedding_backend: LLMBackend,
    embed_model: str,
    embed_timeout_sec: float,
    limit: int = _MAX_SELECTED,
    floor: int = _MIN_SELECTED,
) -> List[str]:
    """Rank tools by cosine similarity between query and tool description embeddings.

    ``limit`` caps how many survive. It is the final answer when this is
    the selection strategy, and a candidate shortlist when retrieval is
    feeding the router, which wants more to choose from than a user's
    prompt should carry.

    ``floor`` is how many to return when the threshold keeps fewer. The two
    callers want opposite things from a decisive result. As a selection
    strategy, one clear winner should stay a short list. As a shortlist for
    the router, it must still be ``limit`` wide, because the router's job
    is to overrule a ranking built on similarity alone and it cannot
    overrule candidates it was never shown.
    """
    import numpy as np

    # Embed the query.
    query_vec = embedding_backend.embed(query, embed_model, timeout_sec=embed_timeout_sec)
    if query_vec is None:
        debug_log("Embedding tool selection: failed to embed query, falling back to all tools", "planning")
        return _all_tool_names(builtin_tools, mcp_tools)

    query_arr = np.array(query_vec, dtype=np.float32)
    q_norm = np.linalg.norm(query_arr)
    if q_norm > 0:
        query_arr = query_arr / q_norm

    # Embed each tool description and compute cosine similarity.
    similarities: List[tuple] = []

    all_tools: Dict[str, str] = {}
    for name, tool in builtin_tools.items():
        if name in _ALWAYS_INCLUDED:
            continue
        all_tools[name] = _tool_summary(name, tool.description)
    for name, spec in mcp_tools.items():
        all_tools[name] = _tool_summary(name, spec.description)

    for name, summary in all_tools.items():
        tool_vec = _embed_tool_text(
            embedding_backend,
            summary,
            embed_model,
            embed_timeout_sec,
            expect_dim=len(query_arr),
        )
        if tool_vec is None or len(tool_vec) != len(query_arr):
            continue
        tool_arr = np.array(tool_vec, dtype=np.float32)
        t_norm = np.linalg.norm(tool_arr)
        if t_norm > 0:
            tool_arr = tool_arr / t_norm
        sim = float(np.dot(query_arr, tool_arr))
        similarities.append((name, sim))

    if not similarities:
        debug_log("Embedding tool selection: no tool embeddings produced, falling back to all tools", "planning")
        return _all_tool_names(builtin_tools, mcp_tools)

    # Sort by similarity descending.
    similarities.sort(key=lambda x: x[1], reverse=True)

    # Select tools using a relative threshold: keep tools whose similarity is
    # within _RELATIVE_THRESHOLD of the best match.  This adapts to the actual
    # score distribution — a flat 0.3 cutoff lets everything through because
    # nomic-embed-text gives high baseline similarity across all tools.
    top_sim = similarities[0][1]
    cutoff = top_sim * _RELATIVE_THRESHOLD
    selected = [name for name, sim in similarities if sim >= cutoff]

    # Always return at least `floor` tools (the top-N by similarity).
    if len(selected) < floor:
        selected = [name for name, _ in similarities[:floor]]

    # Hard ceiling, and the reason this strategy is usable on a large
    # catalogue at all. Any relative cutoff is permissive when the scores
    # are tightly clustered, which is exactly what a general-purpose
    # embedding model does across tools that all sound like software. The
    # threshold above sharpens a good distribution; this bounds a bad one,
    # so the worst case is a handful of mediocre candidates rather than
    # every tool installed.
    selected = selected[:limit]

    selected = _ensure_always_included(selected, builtin_tools, mcp_tools)

    debug_log(
        f"Embedding tool selection: {len(selected)}/{len(builtin_tools) + len(mcp_tools)} tools "
        f"(top sim={top_sim:.3f}, cutoff={cutoff:.3f})",
        "planning",
    )
    return selected


# ---------------------------------------------------------------------------
# Strategy: llm
# ---------------------------------------------------------------------------

def _select_llm(
    query: str,
    builtin_tools: Dict[str, "Tool"],
    mcp_tools: Dict[str, "ToolSpec"],
    llm_backend: LLMBackend,
    llm_model: str,
    llm_timeout_sec: float,
    context_hint: Optional[str] = None,
) -> List[str]:
    """Ask a lightweight LLM call which tools are relevant.

    ``context_hint`` is an optional compact summary of what the main assistant
    can already see at reply time (current local time, user's resolved
    location, recent dialogue). When provided, the router is told that any
    fact visible in that block needs no tool — a query fully answerable from
    the hint should return 'none'. This avoids enumerating specific cases
    ("time is known", "location is known") in the prompt: the router sees the
    actual data and judges for itself. Gracefully degrades when the hint is
    missing or partial (e.g. location failed to resolve) — the router simply
    has less context and falls back to tool-selection on content.
    """
    catalogue_lines: List[str] = []
    for name, tool in builtin_tools.items():
        if name in _ALWAYS_INCLUDED:
            continue
        catalogue_lines.append(f"- {name}: {tool.description[:120]}")
    for name, spec in mcp_tools.items():
        catalogue_lines.append(f"- {name}: {spec.description[:120]}")
    catalogue = "\n".join(catalogue_lines)

    sys_prompt = (
        "You are a tool router. Given a user query and a list of available tools, "
        "pick AT MOST the 5 most relevant tools for the query and return ONLY a "
        "comma-separated list of their exact names. Prefer fewer (1-3) when the "
        "query is clearly about one thing; never return more than 5. "
        "Return 'none' ONLY for pure greetings/small talk OR when the exact "
        "fact needed is already visible in the KNOWN FACTS block below. If "
        "the query depends on data NOT in KNOWN FACTS — the user's logs, "
        "current conditions, web info, files, screen — pick a tool, even "
        "when the phrasing is indirect ('should I order pizza?' → needs the "
        "meal log; 'do I need a jacket?' → needs the weather). Do NOT pick a "
        "tool merely because its domain is loosely adjacent. "
        "If the query asks for DETAILED information on a topic (articles, "
        "explanations, write-ups), include BOTH a search tool AND a page-fetch "
        "tool so the model can follow the chain. "
        "If a RECENT DIALOGUE block is present, read the current query as a "
        "continuation of that dialogue: a short follow-up (e.g. naming a "
        "place, confirming an option, answering a clarifying question the "
        "assistant just asked) should route to the tool that answers the "
        "COMBINED intent across turns, not to 'none'. "
        "Output nothing else — no explanations, no prose, no code fences."
    )
    hint_section = ""
    if context_hint and context_hint.strip():
        raw_hint = context_hint.strip()
        # The hint builder emits two optional subsections: a time/location
        # fact line, and a "Recent dialogue (short-term memory):" block.
        # Surface them under router-specific labels so the prompt above can
        # refer to them by name without the caller having to know.
        dialogue_marker = "Recent dialogue (short-term memory):"
        if dialogue_marker in raw_hint:
            facts_part, _, dialogue_part = raw_hint.partition(dialogue_marker)
            facts_part = facts_part.strip()
            dialogue_part = dialogue_part.strip()
            blocks: list[str] = []
            if facts_part:
                blocks.append(
                    "KNOWN FACTS (the main assistant can already see these at "
                    "reply time, so no tool is needed to surface them):\n"
                    f"{facts_part}"
                )
            if dialogue_part:
                blocks.append(
                    "RECENT DIALOGUE (most recent last — interpret the current "
                    "query as a continuation of this exchange):\n"
                    f"{dialogue_part}"
                )
            hint_section = "\n\n".join(blocks) + "\n\n"
        else:
            hint_section = (
                "KNOWN FACTS (the main assistant can already see these at "
                "reply time, so no tool is needed to surface them):\n"
                f"{raw_hint}\n\n"
            )
    # KV-cache discipline: the mostly-static tool catalogue opens the user
    # prompt (it changes only on an MCP refresh), the dynamic hint (time +
    # dialogue) rides after it, and the query stays the final token. The
    # dynamic hint first would push the divergence to token 1 of the user
    # message and defeat prefix reuse across consecutive router calls.
    user_prompt = (
        f"Available tools:\n{catalogue}\n\n"
        f"{hint_section}"
        f"User query: {query}\n\n"
        "Top tools (comma-separated, max 5, or 'none'):"
    )

    try:
        resp = llm_backend.direct(
            llm_model, sys_prompt, user_prompt,
            timeout_sec=llm_timeout_sec,
            max_tokens=50,
        )
    except Exception as e:
        debug_log(f"LLM tool selection failed: {e}, falling back to keyword strategy", "planning")
        return _select_keyword(query, builtin_tools, mcp_tools)

    if not resp or not isinstance(resp, str):
        debug_log("LLM tool selection returned empty, falling back to keyword strategy", "planning")
        return _select_keyword(query, builtin_tools, mcp_tools)

    resp_lower = resp.strip().lower()
    if resp_lower == "none":
        debug_log("LLM tool selection returned 'none' — including only mandatory tools", "planning")
        return [t for t in _ALWAYS_INCLUDED if t in builtin_tools or t in mcp_tools]

    known = set(builtin_tools.keys()) | set(mcp_tools.keys())
    selected: List[str] = []
    # Chatty routers wrap names in backticks, bullet them, or emit bracketed
    # JSON-ish lists. Strip every punctuation char that can't appear in a tool
    # name before matching, so the extraction is robust to formatting drift.
    _STRIP_CHARS = "'\"`*-_[](){}<>,.:;!?\\ "
    for token in re.split(r"[,\s]+", resp):
        clean = token.strip(_STRIP_CHARS)
        if clean in known and clean not in selected:
            selected.append(clean)

    # Hard cap — a chatty router that ignores the prompt cap must not bloat
    # the downstream tool list. Preserve order (model's ranking).
    if len(selected) > _LLM_MAX_SELECTED:
        selected = selected[:_LLM_MAX_SELECTED]

    selected = _ensure_always_included(selected, builtin_tools, mcp_tools)

    if len(selected) <= len(_ALWAYS_INCLUDED):
        debug_log("LLM tool selection matched nothing, falling back to keyword strategy", "planning")
        return _select_keyword(query, builtin_tools, mcp_tools)

    debug_log(f"LLM tool selection: {len(selected)}/{len(known)} tools selected", "planning")
    return selected


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _shortlist_for_router(
    query: str,
    builtin_tools: Dict[str, "Tool"],
    mcp_tools: Dict[str, "ToolSpec"],
    embedding_backend: Optional[LLMBackend],
    embed_model: str,
    embed_timeout_sec: float,
) -> tuple:
    """Narrow a large catalogue to the candidates worth routing over.

    The router's weakness is the size of what it reads, not the size of
    what it returns, so retrieval reads the catalogue and the router reads
    a shortlist. Below ``_RERANK_CANDIDATES`` there is nothing to remove
    and the embedding call would buy nothing.

    Fails open in every direction: no backend, a dead backend, or a
    retrieval that cannot separate anything all return the catalogue
    untouched, which is the behaviour that existed before retrieval did.
    The cost of narrowing is that a tool retrieval misses is invisible for
    that turn; ``toolSearchTool`` is the mid-loop escape hatch for it.
    """
    catalogue_size = len(builtin_tools) + len(mcp_tools)
    if embedding_backend is None or catalogue_size <= _RERANK_CANDIDATES:
        return builtin_tools, mcp_tools

    candidates = set(
        _select_embedding(
            query, builtin_tools, mcp_tools,
            embedding_backend, embed_model, embed_timeout_sec,
            limit=_RERANK_CANDIDATES,
            floor=_RERANK_CANDIDATES,
        )
    )

    shortlisted_builtin = {n: t for n, t in builtin_tools.items() if n in candidates}
    shortlisted_mcp = {n: s for n, s in mcp_tools.items() if n in candidates}
    shortlisted_size = len(shortlisted_builtin) + len(shortlisted_mcp)

    if not shortlisted_size:
        debug_log("Tool routing: retrieval returned nothing, routing over the full catalogue", "planning")
        return builtin_tools, mcp_tools

    debug_log(
        f"Tool routing: retrieval narrowed {catalogue_size} tools to {shortlisted_size} "
        f"for the router",
        "planning",
    )
    return shortlisted_builtin, shortlisted_mcp


def select_tools(
    query: str,
    builtin_tools: Dict[str, "Tool"],
    mcp_tools: Dict[str, "ToolSpec"],
    strategy: ToolSelectionStrategy = ToolSelectionStrategy.ALL,
    *,
    llm_backend: Optional[LLMBackend] = None,
    llm_model: str = "",
    llm_timeout_sec: float = 8.0,
    embedding_backend: Optional[LLMBackend] = None,
    embed_model: str = "",
    embed_timeout_sec: float = 10.0,
    context_hint: Optional[str] = None,
) -> List[str]:
    """
    Return a list of tool names relevant to *query*.

    Args:
        query:              User's text query.
        builtin_tools:      Registry of builtin Tool instances.
        mcp_tools:          Registry of discovered MCP ToolSpec entries.
        strategy:           ToolSelectionStrategy enum value.
        llm_backend:        Chat backend (needed for "llm" strategy). Construct
                            from settings via ``get_llm_backend(cfg)``.
        llm_model:          Chat model name (needed for "llm" strategy).
        llm_timeout_sec:    Timeout for the LLM call.
        embedding_backend:  Embedding backend (needed for "embedding" strategy).
                            Construct via ``get_embedding_backend(cfg)``.
        embed_model:        Embedding model name (needed for "embedding" strategy).
        embed_timeout_sec:  Timeout for embedding calls.
        context_hint:       Optional facts/dialogue surface for the LLM router.

    Returns:
        List of tool name strings.
    """
    if strategy == ToolSelectionStrategy.KEYWORD:
        return _select_keyword(query, builtin_tools, mcp_tools)
    elif strategy == ToolSelectionStrategy.EMBEDDING:
        if embedding_backend is None:
            debug_log("Embedding tool selection: no backend supplied, falling back to all tools", "planning")
            return _all_tool_names(builtin_tools, mcp_tools)
        return _select_embedding(
            query, builtin_tools, mcp_tools,
            embedding_backend, embed_model, embed_timeout_sec,
        )
    elif strategy == ToolSelectionStrategy.LLM:
        if llm_backend is None:
            debug_log("LLM tool selection: no backend supplied, falling back to keyword strategy", "planning")
            return _select_keyword(query, builtin_tools, mcp_tools)

        routed_builtin, routed_mcp = _shortlist_for_router(
            query, builtin_tools, mcp_tools,
            embedding_backend, embed_model, embed_timeout_sec,
        )
        return _select_llm(
            query, routed_builtin, routed_mcp,
            llm_backend, llm_model, llm_timeout_sec,
            context_hint=context_hint,
        )
    else:
        return _all_tool_names(builtin_tools, mcp_tools)
