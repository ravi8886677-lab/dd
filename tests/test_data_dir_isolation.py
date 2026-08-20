"""The suite must not touch the user's own data directory.

A test run that reads and writes `~/.local/share/jarvis/jarvis.db` is
wrong twice over. It edits the diary of whoever runs it, and it hides
failures: the first run on a clean machine creates the database as a
side effect, so a defect that only shows on a fresh install is red once
in CI and never reproducible afterwards.

`tests/conftest.py` already redirects the config path for the same
reason. These tests hold the data directory to the same rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _user_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "jarvis"


def _is_isolated(path: str | Path) -> bool:
    return _user_data_dir() not in Path(path).resolve().parents


@pytest.mark.unit
class TestTheUserDataDirectoryIsOffLimits:
    def test_settings_resolve_a_database_outside_it(self):
        from jarvis.config import load_settings

        assert _is_isolated(load_settings().db_path)

    def test_the_default_database_path_is_redirected(self):
        from jarvis.config import _default_db_path

        assert _is_isolated(_default_db_path())

    @staticmethod
    def _run_in_a_fresh_home(code: str, home: Path) -> None:
        import os
        import subprocess
        import sys

        env = {
            **os.environ,
            "HOME": str(home),
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        env.pop("JARVIS_CONFIG_PATH", None)
        env.pop("JARVIS_DATA_DIR", None)  # or the child never resolves from HOME
        subprocess.run(
            [sys.executable, "-c", code], env=env, check=True, capture_output=True,
        )

    def test_importing_the_reply_stack_creates_no_data_directory(self, tmp_path):
        """Importing is not using.

        The location module loads its caches from disk on import. Reading
        a cache that is not there yet must not bring the directory into
        existence.
        """
        self._run_in_a_fresh_home("import jarvis.reply", tmp_path)

        assert not (tmp_path / ".local" / "share" / "jarvis").exists()

    def test_the_run_itself_is_pointed_at_a_sandbox(self):
        """Checked from inside the run, because this is the property that
        makes every other writer safe.

        Asserted as "the directory is redirected" rather than "the real
        one is empty": on a developer's own machine the real one is full
        of their diary, and a test that failed for that would be telling
        them off for using the software.
        """
        import os

        from jarvis.utils import paths

        assert os.environ.get(paths.DATA_DIR_ENV_VAR), "the suite must redirect it"
        assert _is_isolated(paths.data_dir())

    def test_asking_whether_geoip_is_available_creates_nothing(self, tmp_path):
        """Three callers ask `does the GeoLite2 database exist?`. None of
        them is a download, so none should bring a directory into being."""
        self._run_in_a_fresh_home(
            "import jarvis.utils.location as loc; loc._get_database_path()", tmp_path,
        )

        assert not (tmp_path / ".local" / "share" / "jarvis").exists()

    def test_reading_dictation_history_creates_nothing(self, tmp_path):
        """Constructing the store reads it. A user who has never dictated
        must not gain a history file by something asking for the count."""
        self._run_in_a_fresh_home(
            "from jarvis.dictation.history import DictationHistory;"
            " DictationHistory().get_all()",
            tmp_path,
        )

        assert not (tmp_path / ".local" / "share" / "jarvis").exists()

    def test_resolving_the_default_voice_creates_nothing(self, tmp_path):
        """A config default resolver, reached without any intent to speak."""
        self._run_in_a_fresh_home(
            "import jarvis.output.tts as tts; tts._get_default_piper_model_path()",
            tmp_path,
        )

        assert not (tmp_path / ".local" / "share" / "jarvis").exists()

    def test_reading_settings_does_not_create_a_data_directory(self, tmp_path):
        """Resolving a path must not create it.

        `get_default_config` runs on any call that reads settings, and a
        great many imports do. If resolving the default creates the
        directory, one appears on disk merely because something imported
        the config module: before the user has agreed to anything, and
        before the daemon has decided where its data lives.

        Run in a subprocess against a throwaway home, because the whole
        point is what happens on a machine where nothing exists yet.
        """
        self._run_in_a_fresh_home(
            "import jarvis.config as c; c.get_default_config()", tmp_path,
        )

        assert not (tmp_path / ".local" / "share" / "jarvis").exists()

    def test_the_dashboard_resolves_a_database_outside_it(self):
        pytest.importorskip("flask")
        from src.desktop_app import memory_viewer

        assert _is_isolated(memory_viewer._get_db_path())

    def test_writing_through_the_dashboard_stays_outside_it(self):
        """The strongest form: follow an actual write to where it lands."""
        pytest.importorskip("flask")
        from src.desktop_app import memory_viewer

        memory_viewer.app.config["TESTING"] = True
        with memory_viewer.app.test_client() as client:
            client.environ_base["HTTP_X_DASHBOARD_TOKEN"] = memory_viewer._SESSION_TOKEN
            assert client.get("/api/stats").status_code == 200

        db_path = Path(memory_viewer._get_db_path())
        assert _is_isolated(db_path)
        assert db_path.exists(), "the write landed, and it landed in the sandbox"


@pytest.mark.unit
class TestTheDataDirectoryHelper:
    """Resolving a path and creating it are separate calls."""

    def test_resolving_creates_nothing(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.delenv(paths.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)

        assert not paths.data_dir().exists()

    def test_ensuring_creates_the_directory(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.delenv(paths.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)

        assert paths.ensure_data_dir().is_dir()

    def test_ensuring_creates_a_named_subdirectory(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.delenv(paths.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
        created = paths.ensure_data_dir("geoip")

        assert created.is_dir()
        assert created.parent == paths.data_dir()

    def test_ensuring_an_existing_directory_is_not_an_error(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.delenv(paths.DATA_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
        paths.ensure_data_dir("models", "piper")

        assert paths.ensure_data_dir("models", "piper").is_dir()


@pytest.mark.unit
class TestTheDataDirectoryCanBePointedElsewhere:
    """`JARVIS_DATA_DIR` is the companion to `JARVIS_CONFIG_PATH`.

    It is what lets the suite guarantee it never writes the real one: the
    database was redirected on its own once, and dictation history, the
    GeoIP database and Piper voices went on landing in the user's home.
    One override covers every writer, present and future, whichever
    import path reaches it.
    """

    def test_the_override_is_honoured(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "elsewhere"))

        assert paths.data_dir() == tmp_path / "elsewhere"

    def test_a_tilde_in_the_override_is_expanded(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.setenv(paths.DATA_DIR_ENV_VAR, "~/somewhere-else")
        monkeypatch.setenv("HOME", str(tmp_path))

        assert paths.data_dir() == tmp_path / "somewhere-else"

    def test_an_empty_override_falls_back_to_the_default(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.setenv(paths.DATA_DIR_ENV_VAR, "")
        monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)

        assert paths.data_dir() == tmp_path / ".local" / "share" / "jarvis"

    def test_the_database_follows_the_override(self, tmp_path, monkeypatch):
        """What the fixture relies on: redirect the directory, and every
        path derived from it moves with it."""
        import jarvis.config

        monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "elsewhere"))

        assert jarvis.config._default_db_path() == str(
            tmp_path / "elsewhere" / "jarvis.db",
        )
