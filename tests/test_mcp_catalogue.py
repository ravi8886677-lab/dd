"""
Tests for the MCP server catalogue.

Verifies catalogue integrity, entry conversion, and wizard filtering.
"""

import pytest

from desktop_app.mcp_catalogue import (
    CATALOGUE,
    CATALOGUE_BY_NAME,
    MCPEntry,
    get_wizard_entries,
)
from jarvis.tools.external.mcp_supply_chain import (
    UnpinnedServerError,
    validate_server_launch,
)


class TestCatalogueIsPinned:
    """Every shipped entry must survive the spawn-time supply-chain guard.

    A catalogue entry that fails this would be offered to the user in the
    wizard and then refused at launch, so the two must agree.
    """

    @pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
    def test_entry_launches_without_installing_unpinned_code(self, entry):
        validate_server_launch(entry.name, entry.to_config())

    @pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
    def test_entry_does_not_opt_out_of_the_guard(self, entry):
        assert "allow_unpinned" not in entry.to_config(), (
            f"Catalogue entry '{entry.name}' disables the supply-chain guard; "
            "a curated entry should be pinned instead"
        )

    def test_guard_would_reject_an_unpinned_entry(self):
        """Guards the guard: a @latest entry must not slip through."""
        sloppy = MCPEntry(
            name="sloppy",
            display_name="Sloppy",
            description="unpinned",
            command="npx",
            args=["-y", "some-server@latest"],
        )
        with pytest.raises(UnpinnedServerError):
            validate_server_launch(sloppy.name, sloppy.to_config())


class TestCatalogueIntegrity:
    """Tests for catalogue data integrity."""

    def test_no_duplicate_names(self):
        """Every catalogue entry must have a unique name."""
        names = [e.name for e in CATALOGUE]
        assert len(names) == len(set(names)), (
            f"Duplicate names: {[n for n in names if names.count(n) > 1]}"
        )

    def test_all_entries_have_required_fields(self):
        """Every entry needs name, display_name, description, command."""
        for e in CATALOGUE:
            assert e.name.strip(), f"Entry missing name"
            assert e.display_name.strip(), f"Entry '{e.name}' missing display_name"
            assert e.description.strip(), f"Entry '{e.name}' missing description"
            assert e.command.strip(), f"Entry '{e.name}' missing command"

    def test_api_key_entries_have_env_var(self):
        """Entries that need an API key must specify the env var name."""
        for e in CATALOGUE:
            if e.needs_api_key:
                assert e.api_key_env_var, (
                    f"Entry '{e.name}' needs API key but has no api_key_env_var"
                )

    def test_by_name_matches_catalogue(self):
        """CATALOGUE_BY_NAME should contain exactly the same entries."""
        assert len(CATALOGUE_BY_NAME) == len(CATALOGUE)
        for e in CATALOGUE:
            assert e.name in CATALOGUE_BY_NAME
            assert CATALOGUE_BY_NAME[e.name] is e


class TestMCPEntryToConfig:
    """Tests for MCPEntry.to_config() conversion."""

    def test_basic_entry(self):
        entry = MCPEntry(
            name="test",
            display_name="Test",
            description="A test server",
            command="npx",
            args=["-y", "@test/server"],
        )
        cfg = entry.to_config()
        assert cfg["transport"] == "stdio"
        assert cfg["command"] == "npx"
        assert cfg["args"] == ["-y", "@test/server"]
        assert "env" not in cfg

    def test_entry_with_env(self):
        entry = MCPEntry(
            name="test",
            display_name="Test",
            description="A test server",
            command="npx",
            args=[],
            env={"API_KEY": "secret"},
        )
        cfg = entry.to_config()
        assert cfg["env"] == {"API_KEY": "secret"}

    def test_to_config_returns_independent_copy(self):
        """Calling to_config twice should return separate dicts."""
        entry = CATALOGUE[0]
        a = entry.to_config()
        b = entry.to_config()
        assert a == b
        a["args"].append("extra")
        assert a != b  # mutating one shouldn't affect the other


class TestWizardEntries:
    """Tests for get_wizard_entries() filtering."""

    def test_only_returns_featured(self):
        """get_wizard_entries() should only return wizard_featured entries."""
        entries = get_wizard_entries()
        assert len(entries) > 0
        for e in entries:
            assert e.wizard_featured is True

    def test_no_api_key_required(self):
        """Wizard entries should not require API keys (they're meant for quick setup)."""
        for e in get_wizard_entries():
            assert not e.needs_api_key, (
                f"Wizard entry '{e.name}' requires an API key — "
                "wizard entries should be zero-config"
            )

    def test_wizard_entries_are_subset_of_catalogue(self):
        """Every wizard entry must also exist in the full catalogue."""
        wizard_names = {e.name for e in get_wizard_entries()}
        catalogue_names = {e.name for e in CATALOGUE}
        assert wizard_names.issubset(catalogue_names)
