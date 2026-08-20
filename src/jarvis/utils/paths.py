"""Where Jarvis keeps what it owns on this machine.

One definition, because seven modules need the same answer: the diary
and knowledge graph, the location caches, the GeoLite2 database, Piper
voice models, dictation history and prompt dumps. Seven copies of
``Path.home() / ".local" / "share" / "jarvis"`` drift, and the drift is
invisible until a user's data is split across two directories.

Resolving a path and creating it are separate calls on purpose.
``data_dir`` answers where something lives and touches nothing;
``ensure_data_dir`` creates, and belongs at the point where something is
about to be written. Reading settings resolves paths, and a great many
imports read settings: if resolving created directories, a data
directory would appear on a machine that has only ever imported a
module.
"""

from __future__ import annotations

from pathlib import Path


def data_dir() -> Path:
    """The Jarvis data directory. Does not create it."""
    return Path.home() / ".local" / "share" / "jarvis"


def ensure_data_dir(*parts: str) -> Path:
    """The data directory, or a subdirectory of it, created if absent.

    Call this where a write is about to happen rather than where a path
    is merely resolved.
    """
    path = data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
