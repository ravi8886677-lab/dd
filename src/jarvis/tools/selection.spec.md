## Tool Selection Spec

Selects a subset of available tools relevant to a given user query, so the LLM receives only tools it is likely to need. Reduces noise for smaller models and lowers token cost.

### ToolSelectionStrategy Enum

```python
class ToolSelectionStrategy(Enum):
    ALL = "all"
    KEYWORD = "keyword"
    EMBEDDING = "embedding"
    LLM = "llm"
```

### Strategies

Controlled by `tool_selection_strategy` in config:

| Value         | Behaviour                                                           | LLM call? | Extra dependency |
|---------------|---------------------------------------------------------------------|-----------|------------------|
| `"all"`       | Pass every registered tool.                                         | No        | None             |
| `"keyword"`   | Score tools by keyword overlap with the query; return top matches.  | No        | None             |
| `"embedding"` | Rank tools by cosine similarity between the query and enriched tool summaries. Tool vectors are cached and embedded in one batch, so both the per-turn and the cold cost are one embedding request regardless of catalogue size. | No | numpy |
| `"llm"`       | Ask a lightweight LLM call to pick the top 3–5 relevant tool names (default). | Yes | None |

### Always-included Tools

Regardless of strategy, these tools are **always** included:
- `stop` — needed so the user can dismiss the assistant at any time.

### Keyword Strategy

1. Build a keyword index per tool from its `name` (camelCase split) and `description` (lowercased, stop-words removed).
2. Tokenise the user query (lowercase, split on whitespace/punctuation).
3. Score each tool: count of query tokens that appear in the tool's keyword set.
4. Return tools with score > 0, plus always-included tools.
5. If no tools score > 0, fall back to returning all tools (query is too vague to filter).

### Embedding Strategy

1. Embed the user query **and every tool summary not already cached, in one `embed_many` request**. The query rides in the same batch because it is needed every turn regardless, so sending it separately would make a warm catalogue cost two round trips where one does. Cold cost is one round trip whatever the catalogue size; this is what lets the catalogue grow. A backend that cannot serve a batch returns `None`, and the summaries fall through to the per-text path below.
2. For each tool (excluding always-included), build a summary string and read its vector — from the cache the batch just warmed, or by embedding it alone if the batch did not cover it. The summary names the tool (camelCase and `snake_case` split into words), the **server it comes from**, and its description. The server matters because people ask for tools by the product they belong to ("use higgsfield to make a clip"), a word that appears nowhere in the tool's own name or description. MCP tools are registered as `server__tool`, so the server is recovered from the name.
3. Compute cosine similarity between the query embedding and each tool embedding.
4. Select tools using a **relative threshold**: keep tools whose similarity >= `top_score * _RELATIVE_THRESHOLD` (0.97).
5. If fewer than `_MIN_SELECTED` (3) tools pass the threshold, take the top 3 by similarity.
6. Truncate to `_MAX_SELECTED` (8). Any relative cutoff is permissive when scores are tightly clustered, which is what a general-purpose embedding model does across tools that all sound like software. The threshold sharpens a good distribution; this bounds a bad one, so the worst case is a handful of mediocre candidates rather than every tool installed.
7. Append always-included tools.
8. If the query embedding fails, fall back to returning all tools. Routing that failed closed would leave the model with no tools at all.

**Tool embeddings are cached** in `_EMBED_CACHE`, keyed by `(model, summary)`. Tool text is fixed for the life of a catalogue while queries are not, so embedding it per turn makes routing cost scale with tools times turns. Keying on the summary means an edited description re-embeds by itself. The key cannot see which backend produced a vector, so a cached vector whose width does not match the current query's is treated as a miss: two providers can serve the same model name and return different dimensions, and scoring a query against another provider's vectors ranks nonsense.

Cosine similarity is only meaningful between vectors of equal width, so a tool whose embedding does not match the query's width is skipped rather than compared.

### LLM Strategy (default)

Routing runs in two stages once the catalogue is large. The router's weakness is the size of what it reads, not the size of what it returns, so retrieval reads the catalogue and the router reads a shortlist.

**Stage 1 — retrieval.** When an embedding backend is available and the catalogue holds more than `_RERANK_CANDIDATES` (15) tools, the embedding ranking above selects that many candidates. The shortlist is wider than the 5 tools the router may return, because retrieval ranks on similarity alone and the router needs room to overrule it. At or below that size the catalogue is handed over whole: there is nothing to remove, and the embedding call would buy nothing.

Retrieval fails open in every direction. No backend, a dead backend, or a retrieval that separates nothing all route over the full catalogue, which is the behaviour that existed before this stage did. The cost of narrowing is that a tool retrieval misses is invisible for that turn; `toolSearchTool` is the mid-loop escape hatch for exactly that.

**Stage 2 — the router**, over whatever survived:

1. Build a catalogue of `- name: description` lines (descriptions truncated to 120 chars) for every registered tool except always-included ones.
2. Send to `call_llm_direct` with a system prompt asking for the **top 5 most relevant** tool names as a comma-separated list. The prompt instructs the router to prefer 1–3 tools for narrow queries and to return `"none"` for greetings/small talk.
3. Parse the response, matching tokens against known tool names (unknowns are dropped silently).
4. Apply a hard `_LLM_MAX_SELECTED` (5) cap regardless of what the router returned, to guard against chatty routers that echo the whole catalogue.
5. Append always-included tools.
6. If the router replies `"none"`, return only the always-included tools.
7. On timeout, empty response, or parse failure (no token in the response matched a known tool name), fall back to the **keyword strategy** rather than to the full catalogue. Reasoning: the catalogue can grow to 30–40 tools once an MCP server like `chrome-devtools` is enabled, and exposing all of them to a small chat model (gemma4:e2b class) overwhelms tool selection, producing empty replies. Keyword scoring narrows on query/name overlap deterministically, and the engine's `toolSearchTool` escape hatch still lets the chat model widen mid-loop if the keyword pick missed.

#### Context-aware routing

When the reply engine passes a `context_hint`, it is split into two labelled semantic slots in the router system prompt:

- **KNOWN FACTS** — things the assistant can already see (current time, detected location). If the query is answerable purely from these, the router should return `none`.
- **RECENT DIALOGUE** — recent user/assistant turns. The router is instructed to read the current query as a continuation of this exchange, so short follow-ups (e.g. "I'm in London" after "which city?") route to the tool that answers the combined intent across turns rather than being treated as idle chatter.

The split is the exact marker `"Recent dialogue (short-term memory):"` — any content before it is known facts, content after it is recent dialogue. If no dialogue marker is present, the whole hint is treated as known facts.

### Interface

```python
def select_tools(
    query: str,
    builtin_tools: Dict[str, Tool],
    mcp_tools: Dict[str, ToolSpec],
    strategy: ToolSelectionStrategy = ToolSelectionStrategy.ALL,
    llm_base_url: str = "",
    llm_model: str = "",
    llm_timeout_sec: float = 8.0,
    embed_model: str = "",
    embed_timeout_sec: float = 10.0,
) -> List[str]:
    """Return list of tool names relevant to the query."""
```

### Integration

Called from the reply engine (Step 6) before `generate_tools_json_schema()` and `generate_tools_description()`. The returned list replaces the current `allowed_tools = list(BUILTIN_TOOLS.keys())`.

### Configuration

- Key: `tool_selection_strategy`
- Type: `str` (validated against `ToolSelectionStrategy` enum values)
- Default: `"llm"`
- Valid values: `"all"`, `"keyword"`, `"embedding"`, `"llm"`

- Key: `fast_model` (the shared fast tier)
- Type: `str`
- Default: `""` (empty string — automatic: the small Ollama default on the Ollama chat path, the chat model on an OpenAI-compatible provider)
- Effect: when `tool_selection_strategy == "llm"`, routing runs on the fast tier (`resolve_model(cfg, Tier.FAST)`): small, fast, already warm for wake-word paths, and structurally the same classification job as intent judging. Set `fast_model` to pin every fast-tier context (routing included) to a specific model.
