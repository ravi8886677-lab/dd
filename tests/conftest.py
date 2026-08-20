import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Robustly locate repository root (directory containing src/jarvis)
_this_file = Path(__file__).resolve()
ROOT = None
for parent in _this_file.parents:
    if (parent / "src" / "jarvis").exists():
        ROOT = parent
        break
if ROOT is None:
    # Fallback to two levels up
    ROOT = _this_file.parent.parent

SRC = ROOT / "src"
# Both ROOT and SRC are on sys.path so tests can write either
#   ``from src.jarvis.x import ...``  (older style, ``src.`` prefix)
# or
#   ``from jarvis.x import ...``      (newer style, no prefix)
# CAUTION: those two import paths resolve to *distinct module instances*.
# A monkeypatch on ``src.jarvis.memory.conversation.X`` does NOT take
# effect on ``jarvis.memory.conversation.X`` and vice versa. When a test
# stubs out a symbol the production code calls, you MUST patch the same
# module instance the production code resolves at runtime. Production code
# in ``src/`` imports without the ``src.`` prefix (e.g. inside endpoint
# handlers it's ``from jarvis.memory.conversation import ...``), so a test
# that monkeypatches a symbol used by production should also import
# without the prefix. This is the convention going forward; the older
# ``from src.X`` style is left in place to avoid a churn-only sweep, but
# do not adopt it for new tests that monkeypatch.
# Add repository root so that 'src' is a package prefix.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Also add the src directory (optional, for backwards compatibility with direct 'jarvis' imports)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass
class MockConfig:
    """Minimal config object for unit tests that need a config."""
    # Provider-aware fields. Default to Ollama at localhost so tests
    # that don't care about providers keep the historical behaviour.
    llm_provider: str = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    # ``llm_chat_model`` defaults to empty so tests that pin
    # ``ollama_chat_model = "gpt-oss:20b"`` to exercise the LARGE-model
    # branch get the legacy alias promoted into ``llm_chat_model`` by
    # ``__post_init__`` — same shape ``load_settings()`` produces.
    llm_chat_model: str = ""
    whisper_model: str = "small"
    embedding_provider: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "nomic-embed-text"
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "gemma4:e2b"
    ollama_embed_model: str = "nomic-embed-text"
    db_path: str = ":memory:"
    sqlite_vss_path: Optional[str] = None
    voice_debug: bool = True
    # Mirrors the real `Settings`. Defaults on, so every existing test keeps
    # the behaviour it was written against; a test that wants a text-only
    # install sets it false.
    voice_enabled: bool = True
    tts_enabled: bool = False
    tts_engine: str = "piper"
    tts_voice: Optional[str] = None
    tts_rate: int = 200
    tts_piper_model_path: Optional[str] = None
    tts_piper_speaker: Optional[int] = None
    tts_piper_length_scale: float = 1.0
    tts_piper_noise_scale: float = 0.667
    tts_piper_noise_w: float = 0.8
    tts_piper_sentence_silence: float = 0.2
    tts_chatterbox_device: str = "cpu"
    tts_chatterbox_audio_prompt: Optional[str] = None
    tts_chatterbox_exaggeration: float = 0.5
    tts_chatterbox_cfg_weight: float = 0.5
    web_search_enabled: bool = True
    brave_search_api_key: str = ""
    wikipedia_fallback_enabled: bool = True
    llm_tools_timeout_sec: float = 8.0
    llm_embedding_timeout_sec: float = 10.0
    llm_chat_timeout_sec: float = 45.0
    agentic_max_turns: int = 8
    tool_selection_strategy: str = "embedding"
    fast_model: str = ""
    memory_enrichment_max_results: int = 5
    memory_enrichment_source: str = "diary"
    location_enabled: bool = True
    location_ip_address: Optional[str] = None
    location_auto_detect: bool = False
    location_cgnat_resolve_public_ip: bool = False
    location_cache_minutes: int = 60
    dialogue_memory_timeout: int = 300
    llm_thinking_enabled: bool = False
    intent_judge_thinking_enabled: bool = False
    dictation_thinking_enabled: bool = False
    dictation_hotkey: str = "ctrl+alt+space"
    mcps: Dict[str, Any] = field(default_factory=dict)
    use_stdin: bool = True

    def __post_init__(self) -> None:
        # Mirror ``load_settings``: when the provider-aware fields are
        # left empty, promote the legacy ``ollama_*`` aliases. Tests can
        # set either pair and end up with consistent reads on either side.
        if not self.llm_chat_model:
            self.llm_chat_model = self.ollama_chat_model
        if not self.llm_base_url:
            self.llm_base_url = self.ollama_base_url
        if not self.embedding_model:
            self.embedding_model = self.ollama_embed_model


@pytest.fixture(autouse=True)
def _isolate_user_config_path(tmp_path_factory, monkeypatch):
    """Redirect ``default_config_path`` to a per-session tempfile so a test
    that calls ``load_settings`` (or any other code path that resolves the
    user's config) cannot read or overwrite ``~/.config/jarvis/config.json``.

    Tests that need to exercise the loader against specific JSON should
    monkey-patch ``_load_json`` (and ``_save_json`` if the migration would
    trigger a write) directly. This fixture is a belt-and-braces guard so
    a half-mocked test cannot reach the real config file.
    """
    sandbox = tmp_path_factory.mktemp("jarvis_config_sandbox")
    monkeypatch.setattr(
        "jarvis.config.default_config_path", lambda: sandbox / "config.json"
    )
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(sandbox / "config.json"))


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_data_dir(tmp_path_factory):
    """Redirect the database to a per-session tempfile so no test reads or
    writes ``~/.local/share/jarvis/jarvis.db``.

    The companion to ``_isolate_user_config_path``, and it matters for the
    same two reasons. A test run must not edit the diary of whoever runs
    it, and a suite that quietly creates the user's database on first run
    hides every defect that only appears on a fresh install: red once on a
    cold machine, green forever after, reproducible by nobody.

    Session-scoped because the dashboard caches its connection and its
    graph store in module globals. One database for the run keeps those
    valid, which is the arrangement the suite already had, only pointed
    somewhere harmless.
    """
    sandbox = tmp_path_factory.mktemp("jarvis_data_sandbox")
    db_path = sandbox / "jarvis.db"

    # ``jarvis.config`` and ``src.jarvis.config`` are separate module
    # objects with separate globals, and different test files reach for
    # different ones. Patch whichever are loaded, the same way
    # ``_clear_dashboard_rate_limits`` does below.
    import jarvis.config  # noqa: F401  (ensure at least one is present)

    targets = [
        module for name, module in list(sys.modules.items())
        if name.endswith("jarvis.config") and hasattr(module, "_default_db_path")
    ]
    originals = [(m, m._default_db_path) for m in targets]
    for module in targets:
        module._default_db_path = lambda: str(db_path)
    try:
        yield db_path
    finally:
        for module, original in originals:
            module._default_db_path = original


@pytest.fixture(autouse=True)
def _clear_dashboard_rate_limits():
    """Empty the dashboard's rate-limit buckets between tests.

    ``memory_viewer._rate_events`` is module-level state shared by every test
    that touches the dashboard. Several files deliberately provoke 401s and
    dozens of MCP writes, and without this those accumulate across files
    until an unrelated test trips a 429 that looks like a real failure.

    Both import paths are reset because ``desktop_app.memory_viewer`` and
    ``src.desktop_app.memory_viewer`` are separate module objects with
    separate buckets, and different test files reach for different ones.
    Only modules already imported are touched: importing either one here
    would pull in the Qt tray package for every test in the suite.
    """
    def _reset():
        for name in ("desktop_app.memory_viewer", "src.desktop_app.memory_viewer"):
            module = sys.modules.get(name)
            reset = getattr(module, "_reset_rate_limits", None)
            if reset is not None:
                reset()

    _reset()
    yield
    _reset()


@pytest.fixture
def mock_config():
    """Provide a mock configuration for unit tests."""
    return MockConfig()


@pytest.fixture
def db():
    """Provide an in-memory database for unit tests."""
    from jarvis.memory.db import Database
    database = Database(":memory:", sqlite_vss_path=None)
    yield database
    database.close()


@pytest.fixture
def dialogue_memory():
    """Provide a dialogue memory instance for unit tests."""
    from jarvis.memory.conversation import DialogueMemory
    return DialogueMemory(inactivity_timeout=300, max_interactions=20)


@pytest.fixture
def qapp():
    """Provide a shared QApplication for Qt-based UI tests.

    Qt requires exactly one QApplication per process.  Re-uses an existing
    instance when present so repeated test runs inside a single session
    don't error.
    """
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app



# ── Audio worker teardown ──────────────────────────────────────────────
#
# The tune player and the TTS engines run their work on daemon threads. A
# test that starts one and does not stop it leaves it running for the rest
# of the session, and the thread is still alive when the interpreter shuts
# down. That was survivable while everything it touched was pure Python;
# with `faster-whisper` installed, PyAV and ctranslate2 native extensions
# are loaded, and a thread inside native code during shutdown can take the
# process down with a segmentation fault instead of an error.
#
# Keyed on the worker's qualified name, because the thread is the only
# handle a teardown has — its `_target` is a bound method, so the owning
# object comes back through `__self__`.
_AUDIO_WORKERS = {
    "TunePlayer._play_tune": "stop_tune",
    "PiperTTS._run": "stop",
    "ChatterboxTTS._run": "stop",
}


@pytest.fixture(autouse=True)
def _stop_audio_workers():
    """Stop any audio worker a test left running."""
    yield

    import threading

    for thread in threading.enumerate():
        target = getattr(thread, "_target", None)
        owner = getattr(target, "__self__", None)
        stop_method = _AUDIO_WORKERS.get(getattr(target, "__qualname__", ""))
        if owner is None or stop_method is None:
            continue
        try:
            getattr(owner, stop_method)()
        except Exception:
            # A worker that cannot stop is this test's problem, not the
            # next test's. Never let teardown fail the run.
            pass
        thread.join(timeout=2.0)
