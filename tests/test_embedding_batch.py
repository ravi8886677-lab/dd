"""
Tests for what a cold tool catalogue costs in round trips.

Retrieval reads the whole catalogue, so the number of network calls it takes
to embed that catalogue is what caps how many tools Jarvis can carry. One
call per tool made the ceiling a latency ceiling: fine against localhost,
untenable against a hosted endpoint where every tool is an internet round
trip. These tests pin the cost in calls, not in seconds, so they mean the
same thing on any machine.
"""

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from jarvis.llm.backend import LLMBackend
from jarvis.llm.ollama import OllamaBackend
from jarvis.llm.openai_compatible import OpenAICompatibleBackend
from jarvis.tools import selection


class _CountingBackend(LLMBackend):
    """Counts calls and hands back a deterministic vector per text."""

    def __init__(self, supports_batch: bool = True, dim: int = 8):
        self.embed_calls: List[str] = []
        self.batch_calls: List[List[str]] = []
        self._supports_batch = supports_batch
        self._dim = dim

    def _vector(self, text: str) -> List[float]:
        # Deterministic, and different per text, so ranking is meaningful.
        seed = sum(ord(c) for c in text)
        return [float((seed + i) % 97) / 97.0 for i in range(self._dim)]

    def embed(self, text: str, model: str, timeout_sec: float = 15.0):
        self.embed_calls.append(text)
        return self._vector(text)

    def embed_many(self, texts, model: str, timeout_sec: float = 15.0):
        self.batch_calls.append(list(texts))
        if not self._supports_batch:
            return None
        return [self._vector(t) for t in texts]

    # Unused abstract surface.
    def chat(self, *a, **k): return None
    def chat_messages(self, *a, **k): return None
    def streaming(self, *a, **k): return None
    def direct(self, *a, **k): return None
    def list_models(self, timeout_sec: float = 5.0): return []


def _tools(count: int) -> Dict[str, Any]:
    """A builtin-shaped catalogue of `count` tools."""
    class _T:
        def __init__(self, desc): self.description = desc
    return {f"tool{i}": _T(f"does job number {i} for the user") for i in range(count)}


@pytest.fixture(autouse=True)
def _clear_cache():
    selection._EMBED_CACHE.clear()
    yield
    selection._EMBED_CACHE.clear()


class TestCatalogueCostsOneRoundTrip:
    """The whole point: cold cost stops scaling with catalogue size."""

    def test_a_cold_catalogue_does_not_cost_one_call_per_tool(self):
        """60 tools must not mean 60 sequential requests."""
        backend = _CountingBackend()
        selection._select_embedding(
            "what is the weather", _tools(60), {}, backend, "embed-model", 5.0,
        )
        assert len(backend.embed_calls) == 0, (
            f"fell back to {len(backend.embed_calls)} single-text calls"
        )
        assert len(backend.batch_calls) == 1

    def test_the_cost_is_flat_as_the_catalogue_grows(self):
        """Ten tools and a hundred cost the same number of round trips."""
        small, large = _CountingBackend(), _CountingBackend()
        selection._select_embedding("q", _tools(10), {}, small, "m", 5.0)
        selection._EMBED_CACHE.clear()
        selection._select_embedding("q", _tools(100), {}, large, "m", 5.0)
        assert len(small.batch_calls) == len(large.batch_calls) == 1

    def test_a_warm_catalogue_costs_nothing_extra(self):
        """Only the query is unknown on the second turn."""
        backend = _CountingBackend()
        tools = _tools(30)
        selection._select_embedding("first query", tools, {}, backend, "m", 5.0)
        first = len(backend.batch_calls) + len(backend.embed_calls)
        selection._select_embedding("second query", tools, {}, backend, "m", 5.0)
        second = len(backend.batch_calls) + len(backend.embed_calls) - first
        assert second == 1, f"a warm catalogue cost {second} calls"


class TestBatchingIsNotLoadBearing:
    """A server that will not take a list must still get correct routing."""

    def test_a_backend_without_batching_still_routes(self):
        """Falls back to one call per tool rather than returning nothing."""
        backend = _CountingBackend(supports_batch=False)
        picked = selection._select_embedding(
            "what is the weather", _tools(20), {}, backend, "m", 5.0,
        )
        assert backend.embed_calls, "no fallback happened"
        assert picked, "routing produced nothing"

    def test_batching_and_falling_back_agree(self):
        """The optimisation must not change which tools are selected."""
        tools = _tools(40)
        batched = selection._select_embedding(
            "send a message to the team", tools, {}, _CountingBackend(), "m", 5.0,
        )
        selection._EMBED_CACHE.clear()
        sequential = selection._select_embedding(
            "send a message to the team", tools, {},
            _CountingBackend(supports_batch=False), "m", 5.0,
        )
        assert batched == sequential

    def test_the_default_contract_loops(self):
        """A third-party backend that implements only embed() still works."""
        class _Minimal(_CountingBackend):
            embed_many = LLMBackend.embed_many  # inherit the default

        backend = _Minimal()
        got = LLMBackend.embed_many(backend, ["a", "b", "c"], "m")
        assert got == [backend._vector(t) for t in ("a", "b", "c")]
        assert backend.embed_calls == ["a", "b", "c"]


class TestBackendsSendOneRequest:
    """The batch has to reach the wire as one request, not N."""

    def test_openai_compatible_sends_one_request_and_keeps_order(self):
        """`data` comes back with an index; order must follow it, not arrival."""
        backend = OpenAICompatibleBackend("http://x/v1")
        posts = []

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                # Deliberately out of order to catch order-by-arrival.
                return {"data": [
                    {"index": 1, "embedding": [2.0, 2.0]},
                    {"index": 0, "embedding": [1.0, 1.0]},
                ]}

        def _post(url, **kwargs):
            posts.append(kwargs.get("json"))
            return _Resp()

        with patch("jarvis.llm.openai_compatible.requests.post", _post):
            got = backend.embed_many(["first", "second"], "m")

        assert len(posts) == 1, f"sent {len(posts)} requests"
        assert posts[0]["input"] == ["first", "second"]
        assert got == [[1.0, 1.0], [2.0, 2.0]]

    def test_ollama_sends_one_request(self):
        """Ollama's batch endpoint takes the list in a single call."""
        backend = OllamaBackend("http://x")
        posts = []

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"embeddings": [[1.0], [2.0], [3.0]]}

        def _post(url, **kwargs):
            posts.append((url, kwargs.get("json")))
            return _Resp()

        with patch("jarvis.llm.ollama.requests.post", _post):
            got = backend.embed_many(["a", "b", "c"], "m")

        assert len(posts) == 1, f"sent {len(posts)} requests"
        assert got == [[1.0], [2.0], [3.0]]

    def test_a_batch_refusal_reports_a_miss_rather_than_wrong_vectors(self):
        """A server rejecting the list must not yield silently bad data."""
        backend = OpenAICompatibleBackend("http://x/v1")

        def _post(url, **kwargs):
            raise RuntimeError("400 Bad Request")

        with patch("jarvis.llm.openai_compatible.requests.post", _post):
            assert backend.embed_many(["a", "b"], "m") is None

    def test_a_short_batch_response_is_a_miss(self):
        """Fewer vectors than texts must not silently misalign tools."""
        backend = OllamaBackend("http://x")

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"embeddings": [[1.0]]}

        with patch("jarvis.llm.ollama.requests.post", lambda url, **k: _Resp()):
            assert backend.embed_many(["a", "b", "c"], "m") is None
