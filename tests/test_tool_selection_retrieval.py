"""Semantic tool retrieval at catalogue sizes worth having.

`selection.spec.md` records why the embedding strategy was benched: bare
name-and-description summaries embedded with a model that scores everything
0.6–0.8 leave nothing to threshold against, so a relative cutoff passes the
whole catalogue. The published work on this points at both halves. Enriching
what gets embedded separates tools that bare descriptions cluster together,
and taking a fixed top-k stops a flat distribution from defeating the filter.

The third property here is cost rather than accuracy: tool text does not
change between queries, so embedding it once per catalogue instead of once
per turn is what makes a large catalogue affordable at all.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from jarvis.tools.selection import (
    _EMBED_CACHE,
    _MAX_SELECTED,
    _select_embedding,
    _tool_summary,
)


@pytest.fixture(autouse=True)
def _clean_embedding_cache():
    """The cache outlives a call by design, so each test starts cold."""
    _EMBED_CACHE.clear()
    yield
    _EMBED_CACHE.clear()


class _Tool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


class _Spec:
    def __init__(self, name: str, description: str, server: str = ""):
        self.name = name
        self.description = description
        self.server = server


class CountingBackend:
    """Embeds on a keyword axis so similarity is predictable.

    Records every text it was asked to embed, because how often the
    catalogue is embedded is the cost question this suite exists for.
    """

    def __init__(self, axes: List[str], *, flat: bool = False):
        self.axes = axes
        self.flat = flat
        self.embedded: List[str] = []

    def embed(self, text: str, model: str, timeout_sec: float = 10.0) -> Optional[List[float]]:
        self.embedded.append(text)
        lowered = text.lower()
        if self.flat:
            # Every tool lands in one tight cluster, which is the failure
            # the spec documents for nomic-embed-text.
            return [1.0] + [0.01 if axis in lowered else 0.0 for axis in self.axes]
        return [0.0] + [1.0 if axis in lowered else 0.0 for axis in self.axes]


def _catalogue(size: int) -> Dict[str, _Spec]:
    return {
        f"tool{i}": _Spec(f"tool{i}", f"does thing number {i}") for i in range(size)
    }


# ── What gets embedded ────────────────────────────────────────────────


@pytest.mark.unit
def test_the_summary_carries_the_tool_name_and_description():
    summary = _tool_summary("webSearch", "Search the web.")

    assert "web search" in summary.lower()
    assert "Search the web." in summary


@pytest.mark.unit
def test_a_camel_case_name_is_split_so_its_words_are_searchable():
    """`webSearch` must match a query about "search", not the token websearch."""
    assert "web search" in _tool_summary("webSearch", "d").lower()


@pytest.mark.unit
def test_an_mcp_tool_carries_its_server_so_the_server_name_is_searchable():
    """Users ask for tools by the product they belong to.

    "use higgsfield to make a clip" names the server, not the tool, and a
    summary built only from the tool's own name cannot match it.
    """
    summary = _tool_summary("generate_video", "Create a clip.", server="higgsfield")

    assert "higgsfield" in summary.lower()


# ── Narrowing ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_a_clear_winner_does_not_drag_the_whole_catalogue_in_with_it():
    backend = CountingBackend(["weather", "search"])
    tools = _catalogue(20)
    tools["weather"] = _Spec("weather", "Get the weather forecast.")
    tools["webSearch"] = _Spec("webSearch", "Search the web.")

    selected = _select_embedding("what is the weather", {}, tools, backend, "m", 10.0)

    assert "weather" in selected
    assert len(selected) <= _MAX_SELECTED, (
        f"returned {len(selected)} of {len(tools)} tools; the filter is not filtering"
    )


@pytest.mark.unit
def test_a_tightly_clustered_catalogue_is_still_narrowed():
    """The documented nomic-embed-text failure, asserted directly.

    When every tool scores within a hair of the top match, a relative
    threshold keeps them all. A fixed top-k does not, which is the whole
    point of the change.
    """
    backend = CountingBackend(["weather"], flat=True)
    tools = _catalogue(30)
    tools["weather"] = _Spec("weather", "Get the weather forecast.")

    selected = _select_embedding("weather", {}, tools, backend, "m", 10.0)

    assert len(selected) <= _MAX_SELECTED, (
        f"a flat similarity distribution let {len(selected)} tools through"
    )


@pytest.mark.unit
def test_a_small_catalogue_is_not_padded_out_to_the_cap():
    backend = CountingBackend(["weather"])
    tools = {"weather": _Spec("weather", "Get the weather forecast.")}

    selected = _select_embedding("weather", {}, tools, backend, "m", 10.0)

    assert selected == ["weather"]


# ── Cost ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_catalogue_is_embedded_once_across_repeated_queries():
    """Tool text does not change between turns, so re-embedding it is waste.

    Embedding the catalogue per turn makes routing cost scale with tools
    times turns, which is what makes a large catalogue unaffordable.
    """
    backend = CountingBackend(["weather", "search"])
    tools = _catalogue(10)

    _select_embedding("weather", {}, tools, backend, "m", 10.0)
    first_pass = list(backend.embedded)
    _select_embedding("search the web", {}, tools, backend, "m", 10.0)

    tool_texts = [t for t in backend.embedded if t not in ("weather", "search the web")]
    assert len(tool_texts) == len(set(tool_texts)), (
        "the catalogue was embedded more than once"
    )
    assert len(first_pass) > 1, "sanity: the first pass must embed the catalogue"


@pytest.mark.unit
def test_changing_a_description_re_embeds_that_tool():
    """A stale cache would route on a description the tool no longer has."""
    backend = CountingBackend(["weather"])
    tools = {"weather": _Spec("weather", "Get the weather forecast.")}
    _select_embedding("weather", {}, tools, backend, "m", 10.0)

    tools["weather"] = _Spec("weather", "Now does something else entirely.")
    _select_embedding("weather", {}, tools, backend, "m", 10.0)

    embedded_summaries = [t for t in backend.embedded if "weather" in t.lower()]
    assert any("something else" in t for t in embedded_summaries)


# ── Failure ───────────────────────────────────────────────────────────


@pytest.mark.unit
def test_switching_embedding_provider_does_not_score_against_stale_vectors():
    """Two backends can serve one model name and return different widths.

    The cache key cannot see that, so without a width check a provider
    switch scores the new query against the old provider's vectors, which
    at best ranks nonsense and at worst cannot be compared at all.
    """
    tools = {"weather": _Spec("weather", "Get the weather forecast.")}
    narrow = CountingBackend(["weather"])
    _select_embedding("weather", {}, tools, narrow, "same-model-name", 10.0)

    wide = CountingBackend(["weather", "search", "video"])
    selected = _select_embedding("weather", {}, tools, wide, "same-model-name", 10.0)

    assert selected == ["weather"]
    assert any("weather" in t.lower() for t in wide.embedded), (
        "the new provider was never asked to embed the catalogue"
    )


@pytest.mark.unit
def test_an_unembeddable_query_falls_back_to_the_whole_catalogue():
    """Routing failing closed would leave the model with no tools at all."""

    class DeadBackend:
        def embed(self, text, model, timeout_sec=10.0):
            return None

    tools = _catalogue(5)
    selected = _select_embedding("anything", {}, tools, DeadBackend(), "m", 10.0)

    assert set(selected) >= set(tools)
