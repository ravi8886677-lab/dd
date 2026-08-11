"""Two-stage tool routing: retrieve, then re-rank.

`selection.spec.md` records the ceiling: past 30–40 tools, handing the whole
catalogue to a small routing model "overwhelms tool selection, producing
empty replies". Raising the cap does not help, because the problem is the
size of what the router reads, not what it returns.

So retrieval narrows the catalogue first and the router re-ranks what
survives. The router keeps the job it is good at, discriminating between a
few plausible candidates, and stops doing the job it is bad at, reading
hundreds of descriptions. Retrieval failing must leave the old behaviour
intact rather than routing on nothing.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from jarvis.tools.selection import (
    _EMBED_CACHE,
    _RERANK_CANDIDATES,
    ToolSelectionStrategy,
    select_tools,
)


@pytest.fixture(autouse=True)
def _clean_embedding_cache():
    _EMBED_CACHE.clear()
    yield
    _EMBED_CACHE.clear()


class _Spec:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


class RecordingRouter:
    """A chat backend that records the catalogue it was asked to route over."""

    def __init__(self, reply: str = "weather"):
        self.reply = reply
        self.prompts: List[str] = []

    def direct(self, *args, **kwargs) -> str:
        self.prompts.append(" ".join(str(a) for a in args) + str(kwargs))
        return self.reply

    def offered_tools(self) -> List[str]:
        """Tool names visible in the catalogue the router was given."""
        blob = "\n".join(self.prompts)
        return [line.split(":")[0].strip("- ").strip()
                for line in blob.splitlines() if line.startswith("- ")]


class KeywordEmbedder:
    def __init__(self, axes: List[str]):
        self.axes = axes
        self.calls = 0

    def embed(self, text: str, model: str, timeout_sec: float = 10.0) -> Optional[List[float]]:
        self.calls += 1
        lowered = text.lower()
        return [0.0] + [1.0 if axis in lowered else 0.0 for axis in self.axes]


class DeadEmbedder:
    def embed(self, text, model, timeout_sec=10.0):
        return None


def _big_catalogue(size: int) -> Dict[str, _Spec]:
    tools = {f"filler{i}": _Spec(f"filler{i}", f"unrelated capability {i}") for i in range(size)}
    tools["weather"] = _Spec("weather", "Get the weather forecast.")
    return tools


def _route(tools, *, router, embedder):
    return select_tools(
        "what is the weather",
        {},
        tools,
        ToolSelectionStrategy.LLM,
        llm_backend=router,
        llm_model="m",
        embedding_backend=embedder,
        embed_model="e",
    )


# ── Narrowing before the router ───────────────────────────────────────


@pytest.mark.unit
def test_a_large_catalogue_reaches_the_router_already_narrowed():
    """The router must never read the whole catalogue once it is large."""
    router = RecordingRouter()
    tools = _big_catalogue(60)

    _route(tools, router=router, embedder=KeywordEmbedder(["weather"]))

    offered = router.offered_tools()
    assert offered, "the router was given no catalogue at all"
    assert len(offered) <= _RERANK_CANDIDATES, (
        f"router was shown {len(offered)} tools out of {len(tools)}"
    )


@pytest.mark.unit
def test_the_relevant_tool_survives_the_narrowing():
    """Narrowing that drops the answer is worse than not narrowing."""
    router = RecordingRouter()
    tools = _big_catalogue(60)

    _route(tools, router=router, embedder=KeywordEmbedder(["weather"]))

    assert "weather" in router.offered_tools()


@pytest.mark.unit
def test_a_small_catalogue_is_handed_over_whole():
    """Below the candidate count there is nothing to narrow.

    Pre-filtering anyway would spend an embedding call to remove nothing.
    """
    router = RecordingRouter()
    tools = {f"t{i}": _Spec(f"t{i}", f"capability {i}") for i in range(4)}
    embedder = KeywordEmbedder(["weather"])

    _route(tools, router=router, embedder=embedder)

    assert embedder.calls == 0, "a small catalogue should not be pre-filtered"
    assert len(router.offered_tools()) == len(tools)


# ── Failing open ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_retrieval_failing_leaves_the_router_working_on_everything():
    """A dead embedding backend must degrade to the old behaviour."""
    router = RecordingRouter()
    tools = _big_catalogue(60)

    _route(tools, router=router, embedder=DeadEmbedder())

    assert len(router.offered_tools()) == len(tools)


@pytest.mark.unit
def test_no_embedding_backend_leaves_the_router_working_on_everything():
    router = RecordingRouter()
    tools = _big_catalogue(60)

    select_tools(
        "what is the weather",
        {},
        tools,
        ToolSelectionStrategy.LLM,
        llm_backend=router,
        llm_model="m",
        embedding_backend=None,
    )

    assert len(router.offered_tools()) == len(tools)


# ── The result ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_router_still_decides_the_final_answer():
    """Retrieval proposes; the router disposes. Its pick must be honoured."""
    router = RecordingRouter(reply="weather")
    tools = _big_catalogue(60)

    selected = _route(tools, router=router, embedder=KeywordEmbedder(["weather"]))

    assert "weather" in selected


@pytest.mark.unit
def test_a_tool_the_retrieval_dropped_cannot_be_selected():
    """The router can only choose from what it was shown.

    Asserted because it is the cost of this design: a tool retrieval
    misses is invisible for that turn, and `toolSearchTool` is the
    mid-loop escape hatch that exists to recover from it.
    """
    router = RecordingRouter(reply="filler7")
    tools = _big_catalogue(60)

    selected = _route(tools, router=router, embedder=KeywordEmbedder(["weather"]))

    if "filler7" not in router.offered_tools():
        assert "filler7" not in selected
