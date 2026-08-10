"""Tests for the dashboard's MCP catalogue endpoints.

The Connections tab used to be a config editor: three text boxes for name,
command and args. Since pinning became mandatory, typing a package without an
exact version produces a config the launcher then refuses, so the tab steered
people into a broken state. These endpoints back a directory instead.
"""

from __future__ import annotations

import json

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")]


def _catalogue_entries():
    """The catalogue as the source of truth, loaded the Qt-free way."""
    from src.jarvis.utils.mcp_catalogue import load_entries

    return load_entries()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.desktop_app import memory_viewer

    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    # _config_path() honours this, so the real resolution runs.
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    memory_viewer.app.config["TESTING"] = True
    test_client = memory_viewer.app.test_client()
    test_client.environ_base["HTTP_X_DASHBOARD_TOKEN"] = memory_viewer._SESSION_TOKEN
    test_client.config_path = config_path
    return test_client


class TestTheCatalogueIsServed:

    def test_every_curated_entry_is_listed(self, client):
        resp = client.get("/api/mcp/catalogue")

        assert resp.status_code == 200
        served = {e["name"] for e in resp.get_json()["entries"]}
        assert served == {e.name for e in _catalogue_entries()}

    def test_entries_carry_what_a_directory_needs_to_render(self, client):
        """Without these the tab cannot be anything but a config editor."""
        entries = client.get("/api/mcp/catalogue").get_json()["entries"]

        for entry in entries:
            assert entry["display_name"]
            assert entry["description"]
            assert entry["category"]
            assert isinstance(entry["needs_api_key"], bool)

    def test_entries_needing_a_key_say_how_to_get_one(self, client):
        entries = client.get("/api/mcp/catalogue").get_json()["entries"]

        for entry in entries:
            if entry["needs_api_key"]:
                assert entry["api_key_hint"], (
                    f"{entry['name']} asks for a key without saying where from"
                )

    def test_already_configured_servers_are_marked(self, client):
        """The grid shows Add or Added, so nothing is installed twice."""
        first = _catalogue_entries()[0]
        client.post(f"/api/mcp/catalogue/{first.name}")

        entries = client.get("/api/mcp/catalogue").get_json()["entries"]
        by_name = {e["name"]: e for e in entries}

        assert by_name[first.name]["configured"] is True
        others = [e for e in entries if e["name"] != first.name]
        assert all(e["configured"] is False for e in others)

    def test_the_endpoint_needs_the_session_token(self, client):
        client.environ_base.pop("HTTP_X_DASHBOARD_TOKEN")

        assert client.get("/api/mcp/catalogue").status_code == 401


class TestOneClickAddCannotProduceARefusedConfig:
    """The whole point: clicking Add writes the catalogue's pinned args, so a
    user cannot create an entry the launcher will then refuse to start."""

    def test_add_writes_the_pinned_args(self, client):
        entry = _catalogue_entries()[0]

        resp = client.post(f"/api/mcp/catalogue/{entry.name}")

        assert resp.status_code == 200
        written = json.loads(client.config_path.read_text())["mcps"][entry.name]
        assert written["args"] == entry.args
        assert written["command"] == entry.command

    def test_everything_added_survives_the_spawn_time_guard(self, client):
        """The guard that refuses unpinned servers is the real judge."""
        from src.jarvis.tools.external.mcp_supply_chain import validate_server_launch

        for entry in _catalogue_entries():
            client.post(f"/api/mcp/catalogue/{entry.name}")

        written = json.loads(client.config_path.read_text())["mcps"]
        for name, spec in written.items():
            validate_server_launch(name, spec)

    def test_an_unknown_name_is_refused(self, client):
        resp = client.post("/api/mcp/catalogue/not-a-real-server")

        assert resp.status_code == 404
        assert "mcps" not in json.loads(client.config_path.read_text())

    def test_a_supplied_api_key_is_stored_under_the_entrys_env_var(self, client):
        entry = next(e for e in _catalogue_entries() if e.needs_api_key)

        resp = client.post(
            f"/api/mcp/catalogue/{entry.name}",
            json={"api_key": "sekrit"},
        )

        assert resp.status_code == 200
        written = json.loads(client.config_path.read_text())["mcps"][entry.name]
        assert written["env"][entry.api_key_env_var] == "sekrit"

    def test_adding_does_not_disturb_servers_the_user_already_had(self, client):
        client.config_path.write_text(json.dumps({
            "mcps": {"mine": {"command": "node", "args": ["server.js"]}}
        }))
        entry = _catalogue_entries()[0]

        client.post(f"/api/mcp/catalogue/{entry.name}")

        mcps = json.loads(client.config_path.read_text())["mcps"]
        assert mcps["mine"] == {"command": "node", "args": ["server.js"]}
        assert entry.name in mcps


class TestTheCatalogueLoadsWithoutQt:
    """The dashboard runs headless. Importing the desktop_app package pulls in
    the tray application and therefore Qt, which need not be installed."""

    def test_loader_does_not_import_the_desktop_package(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        program = (
            "import sys\n"
            "class _Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in ('PyQt6', 'PyQt5', 'desktop_app'):\n"
            "            raise ImportError(name + ' is blocked')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "from jarvis.utils.mcp_catalogue import load_entries\n"
            "print(len(load_entries()))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=str(root),
            env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert int(result.stdout.strip()) == len(_catalogue_entries())


class TestTheConnectionsTabIsADirectory:
    """The tab must offer the curated list first and demote the raw config
    editor, because typing a package without a version produces a config the
    launcher refuses at spawn time."""

    @pytest.fixture
    def html(self):
        from src.desktop_app import memory_viewer
        return memory_viewer._INDEX_HTML

    def test_the_directory_grid_is_present(self, html):
        assert 'id="conn-catalogue"' in html

    def test_the_page_loads_the_catalogue_endpoint(self, html):
        assert "/api/mcp/catalogue" in html

    def test_the_manual_form_is_demoted_behind_a_disclosure(self, html):
        """It stays available; it stops being the first thing offered."""
        advanced = html.split('id="conn-advanced"', 1)
        assert len(advanced) == 2, "no advanced disclosure around the manual form"
        # The three raw inputs must live inside that disclosure.
        after = advanced[1]
        closing = after.find("</details>")
        assert closing != -1, "the disclosure is not closed"
        block = after[:closing]
        for field in ('id="conn-name"', 'id="conn-command"', 'id="conn-args"'):
            assert field in block, f"{field} is not inside the advanced disclosure"

    def test_the_markup_claims_no_vetting(self, html):
        """Jarvis vets nothing. A tick that means 'we checked this' would be a
        lie, and the security layer exists because a server that looks fine
        may not be."""
        pane = html[html.find('id="connections-content"'):]
        # The pane runs to the end of the markup; the scripts follow it.
        pane = pane[:pane.find("<script")]
        assert pane, "could not isolate the connections pane"
        for claim in ("verified", "Verified", "trusted", "safe", "official"):
            assert claim not in pane, f"connections markup claims {claim!r}"

    def test_the_api_offers_no_field_that_implies_vetting(self, client):
        """A field is what a future UI would render a badge from, so the
        contract is the place to keep the claim out."""
        entries = client.get("/api/mcp/catalogue").get_json()["entries"]

        for entry in entries:
            for field in ("verified", "trusted", "safe", "official", "audited"):
                assert field not in entry, f"catalogue entry exposes {field!r}"
