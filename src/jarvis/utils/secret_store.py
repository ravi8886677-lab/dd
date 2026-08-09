"""Credential storage backed by the OS credential store.

Jarvis holds API keys for whatever LLM endpoint the user points it at.
Keeping them in `config.json` puts them in a plain file readable by
anything running as that user — which includes every MCP server
subprocess Jarvis spawns, since those inherit the environment and can
read the home directory. The OS credential store (Keychain on macOS,
Credential Manager on Windows, Secret Service on Linux) is the local,
offline place designed for this.

Not every machine has one. A headless Linux box with no Secret Service
has nowhere to put a secret, and pretending otherwise would mean
deleting the user's only copy. So availability is checked honestly, a
write is read back before it is believed, and config.json keeps the key
when there is nowhere better to put it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


_logging_reentrancy = False


def debug_log(message: str, category: str) -> None:
    """Log through ``jarvis.debug``, imported at call time.

    ``config`` imports this module and ``jarvis.debug`` imports
    ``config``, so binding the logger at import time would close a
    circular import.

    Re-entrancy is blocked because the cycle is not only an import one:
    ``debug_log`` asks ``load_settings()`` whether debug output is on,
    and ``load_settings()`` resolves credentials through this module. A
    credential store that fails would otherwise log, load config,
    fail, log, without bound.
    """
    global _logging_reentrancy
    if _logging_reentrancy:
        return
    _logging_reentrancy = True
    try:
        from ..debug import debug_log as _debug_log

        _debug_log(message, category)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _logging_reentrancy = False


# Namespace for Jarvis's entries in the OS credential store.
_SERVICE = "jarvis"

# Config keys holding a credential. These move to the credential store
# on migration and are resolved from it at load.
CREDENTIAL_KEYS = (
    "llm_api_key",
    "embedding_api_key",
    "brave_search_api_key",
)

_keyring_cache: Any = None
_keyring_probed = False
_backend_dead = False
# Resolved secrets, cached for the process. ``load_settings()`` runs on a
# hot path — ``debug_log`` calls it behind a 2-second TTL, and there are
# call sites all over the codebase — while each lookup is a synchronous
# DBus round trip on Linux and can raise a blocking "allow access"
# dialog on macOS. Reading the keychain once per key per process is the
# difference between that and doing it thousands of times.
_secret_cache: Dict[str, Optional[str]] = {}


class _Unavailable(Exception):
    """The credential store could not service a request."""


def _mark_dead(reason: str) -> None:
    """Stop using the credential store for the rest of this process.

    A keyring backend that fails once fails every time, and each attempt
    is expensive: the Secret Service backend on a box with a broken
    ``cryptography`` panics in Rust, which prints to stderr before
    Python ever sees it. Config is loaded often, so latching this off
    after the first failure is the difference between one stray warning
    and a wall of them.
    """
    global _backend_dead
    if not _backend_dead:
        _backend_dead = True
        debug_log(f"credential store disabled for this session: {reason}", "config")


def _guarded(operation: Any, description: str) -> Any:
    """Run a credential-store call, converting any failure to ``_Unavailable``.

    Catches ``BaseException`` rather than ``Exception`` on purpose. A
    Linux box with a half-installed ``cryptography`` makes keyring's
    Secret Service backend raise ``pyo3_runtime.PanicException``, which
    derives straight from ``BaseException`` — an ``except Exception``
    here would let it escape and take config load down on a machine whose
    only fault is having no working keyring. ``KeyboardInterrupt`` and
    ``SystemExit`` still propagate, because those are the user leaving.
    """
    try:
        return operation()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:  # noqa: BLE001
        _mark_dead(f"{description} failed ({e!r})")
        raise _Unavailable(description) from None


def reset_cache() -> None:
    """Forget the probed backend. Used by tests and after a config change."""
    global _keyring_cache, _keyring_probed, _backend_dead
    _keyring_cache = None
    _keyring_probed = False
    _backend_dead = False
    _secret_cache.clear()


def _load_keyring() -> Any:
    """Import ``keyring`` if it is installed, else ``None``.

    Isolated so tests can substitute a backend without touching the
    real credential store of whoever runs the suite.
    """
    try:
        import keyring  # type: ignore

        return keyring
    except Exception as e:  # noqa: BLE001
        debug_log(f"keyring unavailable: {e}", "config")
        return None


def _backend() -> Any:
    global _keyring_cache, _keyring_probed
    if _backend_dead:
        return None
    if not _keyring_probed:
        _keyring_cache = _load_keyring()
        _keyring_probed = True
    return _keyring_cache


def is_available() -> bool:
    """Whether a working credential store is reachable.

    Probes with a real read: ``keyring`` imports fine on a headless
    Linux box and then raises on first use, so importability alone
    proves nothing.
    """
    backend = _backend()
    if backend is None:
        return False
    try:
        _guarded(lambda: backend.get_password(_SERVICE, "__probe__"), "probe")
        return True
    except _Unavailable:
        return False


def get_secret(name: str, use_cache: bool = True) -> Optional[str]:
    """Return a stored secret, or ``None`` if unset or unavailable.

    Cached per process. ``use_cache=False`` forces a real read, which is
    what the write path needs to verify what it just stored.
    """
    if use_cache and name in _secret_cache:
        return _secret_cache[name]

    backend = _backend()
    if backend is None:
        if use_cache:
            _secret_cache[name] = None
        return None
    try:
        value = _guarded(lambda: backend.get_password(_SERVICE, name), f"read {name}")
    except _Unavailable:
        if use_cache:
            _secret_cache[name] = None
        return None

    resolved = value or None
    if use_cache:
        _secret_cache[name] = resolved
    return resolved


def set_secret(name: str, value: str) -> bool:
    """Store a secret, returning whether it is genuinely stored.

    The value is read back before this returns ``True``. Some keyring
    backends accept a write and keep nothing; believing them would mean
    clearing the plaintext copy and losing the key.
    """
    backend = _backend()
    if backend is None:
        return False

    if not value:
        return delete_secret(name)

    try:
        _guarded(lambda: backend.set_password(_SERVICE, name, value), f"write {name}")
    except _Unavailable:
        return False

    if get_secret(name, use_cache=False) != value:
        debug_log(
            f"credential store accepted '{name}' but did not return it; "
            "treating the write as failed",
            "config",
        )
        return False
    _secret_cache[name] = value
    return True


def delete_secret(name: str) -> bool:
    """Remove a secret. Returns whether the store is usable at all."""
    backend = _backend()
    if backend is None:
        return False
    try:
        _guarded(lambda: backend.delete_password(_SERVICE, name), f"delete {name}")
    except _Unavailable:
        # Deleting something absent is the normal case on most backends.
        pass
    _secret_cache[name] = None
    return True


def resolve_secret(name: str, config_value: str) -> str:
    """Return the credential to use, preferring an explicit config value.

    A value the user typed into config.json wins, so hand-editing the
    file still behaves the way it looks like it should. The credential
    store answers when the config is silent.
    """
    if config_value:
        return config_value
    return get_secret(name) or ""


def migrate_plaintext_secrets(cfg_json: Dict[str, Any]) -> bool:
    """Move plaintext credentials out of a config dict into the OS store.

    Mutates ``cfg_json`` in place and returns whether anything moved.
    A key is only cleared from the config once the credential store has
    confirmed it can hand the value back.
    """
    moved = False
    for key in CREDENTIAL_KEYS:
        value = str(cfg_json.get(key, "") or "").strip()
        if not value:
            continue
        if set_secret(key, value):
            cfg_json[key] = ""
            moved = True
            debug_log(f"moved '{key}' into the OS credential store", "config")
        else:
            debug_log(
                f"leaving '{key}' in config.json — no usable credential store",
                "config",
            )
    return moved
