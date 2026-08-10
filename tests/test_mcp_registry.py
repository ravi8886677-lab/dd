"""Tests for the MCP registry client.

The curated catalogue is nine hand-picked servers. The official registry at
registry.modelcontextprotocol.io is a metadata API listing thousands. It is
metadata only: no user data is sent, and the results are cached so the
dashboard still works with no network, which is the supported way to run
Jarvis.
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from src.jarvis.tools.external import mcp_registry
from src.jarvis.tools.external.mcp_supply_chain import validate_server_launch

pytestmark = pytest.mark.unit


def _payload(*servers):
    return {"servers": list(servers), "metadata": {"count": len(servers)}}


def _server(name="io.github.acme/thing", version="1.2.3", packages=None, remotes=None):
    entry = {
        "server": {
            "name": name,
            "title": "Thing",
            "description": "Does a thing",
            "version": version,
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active", "isLatest": True,
            }
        },
    }
    if packages is not None:
        entry["server"]["packages"] = packages
    if remotes is not None:
        entry["server"]["remotes"] = remotes
    return entry


def _response(payload):
    resp = Mock(status_code=200, is_redirect=False, is_permanent_redirect=False,
                headers={}, raise_for_status=Mock())
    resp.json.return_value = payload
    return resp


class TestEntriesAreNormalised:

    def test_the_publisher_namespace_is_split_out(self):
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server()))):
            entries = mcp_registry.fetch()

        assert entries[0]["namespace"] == "io.github.acme"
        assert entries[0]["name"] == "io.github.acme/thing"

    def test_how_the_namespace_was_proven_is_recorded(self):
        """This is the registry's only trust signal, and it is about who
        published, not about whether the code is safe."""
        payload = _payload(
            _server(name="io.github.acme/thing"),
            _server(name="com.example/other"),
        )
        with patch.object(mcp_registry, "guarded_get", return_value=_response(payload)):
            entries = mcp_registry.fetch()

        by_name = {e["name"]: e for e in entries}
        assert by_name["io.github.acme/thing"]["namespace_proof"] == "github"
        assert by_name["com.example/other"]["namespace_proof"] == "dns"

    def test_an_npm_package_becomes_a_pinned_launch(self):
        packages = [{"registryType": "npm", "identifier": "thing-mcp",
                     "version": "1.2.3", "transport": {"type": "stdio"}}]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(packages=packages)))):
            entries = mcp_registry.fetch()

        install = entries[0]["install"]
        assert install["command"] == "npx"
        validate_server_launch("thing", install)

    def test_a_pypi_package_becomes_a_pinned_launch(self):
        packages = [{"registryType": "pypi", "identifier": "thing-mcp",
                     "version": "0.4.0", "transport": {"type": "stdio"}}]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(packages=packages)))):
            entries = mcp_registry.fetch()

        install = entries[0]["install"]
        assert install["command"] == "uvx"
        validate_server_launch("thing", install)

    def test_a_package_without_a_version_is_not_offered_for_install(self):
        """An unpinned entry would be refused at spawn time, so offering an
        Add button for it would only produce a broken config."""
        packages = [{"registryType": "npm", "identifier": "thing-mcp",
                     "transport": {"type": "stdio"}}]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(packages=packages)))):
            entries = mcp_registry.fetch()

        assert entries[0]["install"] is None

    def test_an_unknown_registry_type_is_not_offered_for_install(self):
        packages = [{"registryType": "cargo", "identifier": "thing",
                     "version": "1.0.0", "transport": {"type": "stdio"}}]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(packages=packages)))):
            entries = mcp_registry.fetch()

        assert entries[0]["install"] is None

    def test_a_remote_only_server_records_its_url(self):
        remotes = [{"type": "streamable-http", "url": "https://example.com/mcp"}]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(remotes=remotes)))):
            entries = mcp_registry.fetch()

        assert entries[0]["remote_url"] == "https://example.com/mcp"

    def test_superseded_versions_are_dropped(self):
        """The registry returns every published version; a directory wants
        the current one."""
        old = _server(version="1.0.0")
        old["_meta"]["io.modelcontextprotocol.registry/official"]["isLatest"] = False
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(old, _server(version="2.0.0")))):
            entries = mcp_registry.fetch()

        assert [e["version"] for e in entries] == ["2.0.0"]


class TestTheCacheKeepsItUsableOffline:

    def test_results_are_written_to_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_registry, "_cache_path", lambda: tmp_path / "reg.json")
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server()))):
            mcp_registry.refresh()

        cached = json.loads((tmp_path / "reg.json").read_text())
        assert cached["entries"][0]["name"] == "io.github.acme/thing"
        assert cached["fetched_at"]

    def test_cached_entries_are_read_without_touching_the_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_registry, "_cache_path", lambda: tmp_path / "reg.json")
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server()))):
            mcp_registry.refresh()

        with patch.object(mcp_registry, "guarded_get",
                          side_effect=AssertionError("network touched")) as never:
            entries, fetched_at = mcp_registry.load_cached()

        never.assert_not_called()
        assert entries[0]["name"] == "io.github.acme/thing"
        assert fetched_at

    def test_no_cache_reads_as_empty_rather_than_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_registry, "_cache_path", lambda: tmp_path / "absent.json")

        entries, fetched_at = mcp_registry.load_cached()

        assert entries == []
        assert fetched_at is None

    def test_a_corrupt_cache_reads_as_empty(self, tmp_path, monkeypatch):
        path = tmp_path / "reg.json"
        path.write_text("{not json")
        monkeypatch.setattr(mcp_registry, "_cache_path", lambda: path)

        assert mcp_registry.load_cached() == ([], None)

    def test_a_failed_refresh_leaves_the_previous_cache_intact(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_registry, "_cache_path", lambda: tmp_path / "reg.json")
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server()))):
            mcp_registry.refresh()

        with patch.object(mcp_registry, "guarded_get", side_effect=OSError("offline")):
            with pytest.raises(mcp_registry.RegistryUnavailableError):
                mcp_registry.refresh()

        entries, _ = mcp_registry.load_cached()
        assert entries[0]["name"] == "io.github.acme/thing"


class TestTheRegistryIsNotAVettingSignal:

    def test_no_entry_claims_the_server_is_safe(self):
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server()))):
            entries = mcp_registry.fetch()

        for field in ("verified", "trusted", "safe", "audited", "approved"):
            assert field not in entries[0], f"registry entry exposes {field!r}"


class TestOnlyGenuinePinsGetAnAddButton:
    """`install` is the config a click will write. If the supply-chain guard
    would refuse it, offering the button produces a server that cannot start
    — the failure the connections directory exists to prevent."""

    @pytest.mark.parametrize("version", [
        "latest", "next", "1.0", "^1.2.3", "~1.2.3", ">=1.0.0", "*", "",
    ])
    def test_an_unpinned_npm_version_is_not_offered(self, version):
        packages = [{"registryType": "npm", "identifier": "thing-mcp",
                     "version": version, "transport": {"type": "stdio"}}]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(packages=packages)))):
            entries = mcp_registry.fetch()

        assert entries[0]["install"] is None, f"{version!r} was offered as a pin"

    @pytest.mark.parametrize("version", ["v1.2.3", "1.2.*", "latest", ">=0.4"])
    def test_an_unpinned_pypi_version_is_not_offered(self, version):
        packages = [{"registryType": "pypi", "identifier": "thing-mcp",
                     "version": version, "transport": {"type": "stdio"}}]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(packages=packages)))):
            entries = mcp_registry.fetch()

        assert entries[0]["install"] is None, f"{version!r} was offered as a pin"

    def test_anything_offered_is_accepted_by_the_real_guard(self):
        """The guard itself is the judge, not a second opinion about pinning."""
        packages = [
            {"registryType": "npm", "identifier": "a", "version": "latest"},
            {"registryType": "npm", "identifier": "b", "version": "2.3.4"},
        ]
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response(_payload(_server(packages=packages)))):
            entries = mcp_registry.fetch()

        install = entries[0]["install"]
        assert install is not None, "a pinned package later in the list was skipped"
        validate_server_launch("thing", install)


class TestMalformedRegistryDataDoesNotEscape:
    """A public API's response shape is not ours to trust."""

    @pytest.mark.parametrize("record", [
        {"server": {"name": "io.github.a/b"}, "_meta": [1]},
        {"server": {"name": "io.github.a/b", "remotes": ["not-a-dict"]}},
        {"server": {"name": "io.github.a/b", "remotes": [None]}},
        {"server": {"name": "io.github.a/b", "packages": ["not-a-dict"]}},
    ])
    def test_malformed_sub_fields_do_not_lose_a_usable_server(self, record):
        """A broken `_meta` or `remotes` is no reason to hide a server whose
        name and description are perfectly readable."""
        payload = _payload(record, _server())
        with patch.object(mcp_registry, "guarded_get", return_value=_response(payload)):
            entries = mcp_registry.fetch()

        names = [e["name"] for e in entries]
        assert "io.github.acme/thing" in names
        assert "io.github.a/b" in names
        assert all(e["install"] is None or e["install"]["command"] for e in entries)

    @pytest.mark.parametrize("record", [
        {"server": "not-a-dict"},
        {"server": {}},
        "not-a-dict-at-all",
        None,
        42,
    ])
    def test_a_record_with_no_usable_server_is_dropped(self, record):
        payload = _payload(record, _server())
        with patch.object(mcp_registry, "guarded_get", return_value=_response(payload)):
            entries = mcp_registry.fetch()

        assert [e["name"] for e in entries] == ["io.github.acme/thing"]

    def test_a_wholly_unusable_response_raises_the_documented_error(self):
        with patch.object(mcp_registry, "guarded_get",
                          return_value=_response({"servers": "nonsense"})):
            with pytest.raises(mcp_registry.RegistryUnavailableError):
                mcp_registry.fetch()


class TestACorruptCacheEntryIsSurvivable:

    def test_entries_missing_fields_are_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_registry, "_cache_path", lambda: tmp_path / "reg.json")
        (tmp_path / "reg.json").write_text(json.dumps({
            "fetched_at": 1.0,
            "entries": [{}, "not-a-dict", {"name": "io.github.a/b"}],
        }))

        entries, _ = mcp_registry.load_cached()

        assert [e["name"] for e in entries] == ["io.github.a/b"]
