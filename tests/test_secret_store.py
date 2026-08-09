"""Behavioural tests for credential storage.

Jarvis holds API keys for whatever LLM endpoint the user points it at.
Those sat in `config.json` in plaintext, readable by anything running as
the user — including the MCP server subprocesses Jarvis spawns. This
moves them into the OS credential store where one exists, and is honest
about it when one does not.
"""

import pytest

from jarvis.utils import secret_store


class _FakeKeyring:
    """Stand-in for the ``keyring`` module, including its error type."""

    class errors:  # noqa: N801 - mirrors the real module's shape
        class KeyringError(Exception):
            pass

    def __init__(self, working=True):
        self._store = {}
        self._working = working

    def get_password(self, service, name):
        if not self._working:
            raise self.errors.KeyringError("no backend")
        return self._store.get((service, name))

    def set_password(self, service, name, value):
        if not self._working:
            raise self.errors.KeyringError("no backend")
        self._store[(service, name)] = value

    def delete_password(self, service, name):
        if not self._working:
            raise self.errors.KeyringError("no backend")
        self._store.pop((service, name), None)


@pytest.fixture
def keyring_backend(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store, "_load_keyring", lambda: fake)
    secret_store.reset_cache()
    yield fake
    secret_store.reset_cache()


@pytest.fixture
def no_keyring(monkeypatch):
    monkeypatch.setattr(secret_store, "_load_keyring", lambda: None)
    secret_store.reset_cache()
    yield
    secret_store.reset_cache()


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_a_stored_secret_comes_back(keyring_backend):
    assert secret_store.set_secret("llm_api_key", "sk-abc123") is True
    assert secret_store.get_secret("llm_api_key") == "sk-abc123"


def test_an_unset_secret_is_none(keyring_backend):
    assert secret_store.get_secret("llm_api_key") is None


def test_a_deleted_secret_is_gone(keyring_backend):
    secret_store.set_secret("llm_api_key", "sk-abc123")
    secret_store.delete_secret("llm_api_key")
    assert secret_store.get_secret("llm_api_key") is None


def test_secrets_do_not_collide_across_names(keyring_backend):
    secret_store.set_secret("llm_api_key", "one")
    secret_store.set_secret("embedding_api_key", "two")
    assert secret_store.get_secret("llm_api_key") == "one"
    assert secret_store.get_secret("embedding_api_key") == "two"


def test_storing_an_empty_value_clears_the_secret(keyring_backend):
    secret_store.set_secret("llm_api_key", "sk-abc123")
    secret_store.set_secret("llm_api_key", "")
    assert secret_store.get_secret("llm_api_key") is None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def test_availability_is_reported_when_a_backend_works(keyring_backend):
    assert secret_store.is_available() is True


def test_no_backend_is_reported_not_faked(no_keyring):
    """A headless Linux box may genuinely have nowhere to put a secret."""
    assert secret_store.is_available() is False
    assert secret_store.set_secret("llm_api_key", "sk-abc123") is False
    assert secret_store.get_secret("llm_api_key") is None


def test_a_broken_backend_is_treated_as_absent(monkeypatch):
    monkeypatch.setattr(secret_store, "_load_keyring", lambda: _FakeKeyring(working=False))
    secret_store.reset_cache()
    try:
        assert secret_store.is_available() is False
        assert secret_store.set_secret("llm_api_key", "x") is False
    finally:
        secret_store.reset_cache()


def test_a_backend_that_silently_drops_writes_is_not_trusted(monkeypatch):
    """Some keyring backends accept a write and return nothing on read.

    Reporting success there would mean blanking the key out of
    config.json and losing it, so the write is read back before it
    counts as stored.
    """

    class Amnesiac(_FakeKeyring):
        def set_password(self, service, name, value):
            pass  # accepts, stores nothing

    monkeypatch.setattr(secret_store, "_load_keyring", lambda: Amnesiac())
    secret_store.reset_cache()
    try:
        assert secret_store.set_secret("llm_api_key", "sk-abc123") is False
    finally:
        secret_store.reset_cache()


# ---------------------------------------------------------------------------
# Migration out of config.json
# ---------------------------------------------------------------------------

def test_migration_moves_a_plaintext_key_into_the_keychain(keyring_backend):
    cfg = {"llm_api_key": "sk-plaintext", "llm_chat_model": "x"}
    changed = secret_store.migrate_plaintext_secrets(cfg)

    assert changed is True
    assert secret_store.get_secret("llm_api_key") == "sk-plaintext"
    assert not cfg.get("llm_api_key"), "the plaintext copy is still in the config"
    assert cfg["llm_chat_model"] == "x", "migration touched unrelated settings"


def test_migration_moves_every_credential_field(keyring_backend):
    cfg = {
        "llm_api_key": "one",
        "embedding_api_key": "two",
        "brave_search_api_key": "three",
    }
    secret_store.migrate_plaintext_secrets(cfg)

    assert secret_store.get_secret("llm_api_key") == "one"
    assert secret_store.get_secret("embedding_api_key") == "two"
    assert secret_store.get_secret("brave_search_api_key") == "three"
    assert not any(cfg.values())


def test_migration_keeps_the_plaintext_key_when_there_is_no_keychain(no_keyring):
    """Losing the user's only copy of their key is worse than storing it badly."""
    cfg = {"llm_api_key": "sk-plaintext"}
    changed = secret_store.migrate_plaintext_secrets(cfg)

    assert changed is False
    assert cfg["llm_api_key"] == "sk-plaintext"


def test_migration_is_a_no_op_when_there_is_nothing_to_move(keyring_backend):
    cfg = {"llm_api_key": "", "llm_chat_model": "x"}
    assert secret_store.migrate_plaintext_secrets(cfg) is False


def test_migration_runs_clean_a_second_time(keyring_backend):
    cfg = {"llm_api_key": "sk-plaintext"}
    secret_store.migrate_plaintext_secrets(cfg)
    assert secret_store.migrate_plaintext_secrets(cfg) is False
    assert secret_store.get_secret("llm_api_key") == "sk-plaintext"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_resolution_prefers_an_explicit_config_value(keyring_backend):
    """A hand-edited config.json must still win, or it looks broken."""
    secret_store.set_secret("llm_api_key", "from-keychain")
    assert secret_store.resolve_secret("llm_api_key", "from-config") == "from-config"


def test_resolution_falls_back_to_the_keychain(keyring_backend):
    secret_store.set_secret("llm_api_key", "from-keychain")
    assert secret_store.resolve_secret("llm_api_key", "") == "from-keychain"


def test_resolution_returns_empty_when_nothing_is_set(keyring_backend):
    assert secret_store.resolve_secret("llm_api_key", "") == ""


def test_resolution_never_raises_without_a_backend(no_keyring):
    assert secret_store.resolve_secret("llm_api_key", "") == ""


# ---------------------------------------------------------------------------
# Backends that fail outside the Exception hierarchy
# ---------------------------------------------------------------------------

class _PanicLike(BaseException):
    """Mimics ``pyo3_runtime.PanicException``: derives from BaseException.

    keyring's Secret Service backend raises this on a Linux box with a
    half-installed ``cryptography``. It is not an ``Exception``, so an
    ``except Exception`` guard does not stop it.
    """


@pytest.fixture
def panicking_keyring(monkeypatch):
    class Panicker:
        def get_password(self, service, name):
            raise _PanicLike("Python API call failed")

        def set_password(self, service, name, value):
            raise _PanicLike("Python API call failed")

        def delete_password(self, service, name):
            raise _PanicLike("Python API call failed")

    monkeypatch.setattr(secret_store, "_load_keyring", lambda: Panicker())
    secret_store.reset_cache()
    yield
    secret_store.reset_cache()


def test_a_panicking_backend_is_treated_as_absent(panicking_keyring):
    assert secret_store.is_available() is False
    assert secret_store.get_secret("llm_api_key") is None
    assert secret_store.set_secret("llm_api_key", "x") is False


def test_a_panicking_backend_does_not_break_config_resolution(panicking_keyring):
    """Config load must survive a broken keyring, not die on import."""
    assert secret_store.resolve_secret("llm_api_key", "") == ""
    assert secret_store.resolve_secret("llm_api_key", "from-config") == "from-config"


def test_a_panicking_backend_keeps_the_plaintext_key(panicking_keyring):
    cfg = {"llm_api_key": "sk-plaintext"}
    assert secret_store.migrate_plaintext_secrets(cfg) is False
    assert cfg["llm_api_key"] == "sk-plaintext"


def test_a_user_interrupt_still_propagates(monkeypatch):
    """Swallowing everything would also swallow Ctrl-C."""

    class Interrupting:
        def get_password(self, service, name):
            raise KeyboardInterrupt

    monkeypatch.setattr(secret_store, "_load_keyring", lambda: Interrupting())
    secret_store.reset_cache()
    try:
        with pytest.raises(KeyboardInterrupt):
            secret_store.is_available()
    finally:
        secret_store.reset_cache()


def test_a_failing_backend_is_consulted_only_once(monkeypatch):
    """Config is loaded often; a broken keyring must not be retried each time.

    The Secret Service backend panics in Rust, which writes to stderr
    before Python can catch anything, so a retry per config load is a
    wall of noise rather than one warning.
    """
    attempts = {"n": 0}

    class Panicker:
        def get_password(self, service, name):
            attempts["n"] += 1
            raise _PanicLike("boom")

    monkeypatch.setattr(secret_store, "_load_keyring", lambda: Panicker())
    secret_store.reset_cache()
    try:
        for _ in range(10):
            secret_store.resolve_secret("llm_api_key", "")
        assert attempts["n"] == 1, f"backend was retried {attempts['n']} times"
    finally:
        secret_store.reset_cache()


def test_logging_from_a_failing_backend_does_not_recurse(monkeypatch):
    """``debug_log`` loads config, and config resolves secrets through here.

    Without a re-entrancy guard a failing credential store logs, which
    loads config, which resolves a secret, which fails, which logs.
    """
    depth = {"max": 0, "current": 0}

    def tracking_debug(message, category="debug"):
        depth["current"] += 1
        depth["max"] = max(depth["max"], depth["current"])
        try:
            # Stand in for load_settings() reaching back into this module.
            secret_store.resolve_secret("llm_api_key", "")
        finally:
            depth["current"] -= 1

    import jarvis.debug

    monkeypatch.setattr(jarvis.debug, "debug_log", tracking_debug)

    class Panicker:
        def get_password(self, service, name):
            raise _PanicLike("boom")

    monkeypatch.setattr(secret_store, "_load_keyring", lambda: Panicker())
    secret_store.reset_cache()
    try:
        secret_store.resolve_secret("llm_api_key", "")
        assert depth["max"] <= 1, f"recursed {depth['max']} levels deep"
    finally:
        secret_store.reset_cache()


# ---------------------------------------------------------------------------
# A write that cannot be confirmed must not leave a copy behind
# ---------------------------------------------------------------------------

class _DeniedOnRead(_FakeKeyring):
    """Writes land silently; reads prompt and are refused.

    This is macOS's actual shape: storing an item usually needs no
    approval, while reading one back does. Denying that prompt fails the
    verification *after* the value is already in the keychain.
    """

    def get_password(self, service, name):
        raise RuntimeError("User denied keychain access")


def test_an_unconfirmable_write_is_rolled_back(monkeypatch):
    backend = _DeniedOnRead()
    monkeypatch.setattr(secret_store, "_load_keyring", lambda: backend)
    secret_store.reset_cache()
    try:
        assert secret_store.set_secret("llm_api_key", "sk-abc123") is False
        assert backend._store == {}, (
            "a copy of the key was left in a store the user was never told about"
        )
    finally:
        secret_store.reset_cache()


def test_migration_leaves_nothing_behind_when_the_read_back_is_denied(monkeypatch):
    """The plaintext must survive *and* no orphan may be created.

    Both copies existing is worse than either alone: `resolve_secret`
    falls back to the store when config.json is empty, so an orphan
    silently resurrects a key the user later thought they had removed.
    """
    backend = _DeniedOnRead()
    monkeypatch.setattr(secret_store, "_load_keyring", lambda: backend)
    secret_store.reset_cache()
    try:
        cfg = {"llm_api_key": "sk-LLM", "embedding_api_key": "sk-EMB"}
        assert secret_store.migrate_plaintext_secrets(cfg) is False
        assert cfg["llm_api_key"] == "sk-LLM"
        assert cfg["embedding_api_key"] == "sk-EMB"
        assert backend._store == {}
    finally:
        secret_store.reset_cache()


def test_rollback_survives_a_delete_that_also_fails(monkeypatch):
    """Best effort: a store refusing deletes must not raise out of set_secret."""

    class RefusesEverything(_DeniedOnRead):
        def delete_password(self, service, name):
            raise RuntimeError("no")

    monkeypatch.setattr(secret_store, "_load_keyring", lambda: RefusesEverything())
    secret_store.reset_cache()
    try:
        assert secret_store.set_secret("llm_api_key", "sk-abc123") is False
    finally:
        secret_store.reset_cache()
