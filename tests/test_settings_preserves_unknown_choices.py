"""
Tests that opening Settings never rewrites a value the user did not touch.

A `choice` field renders as a combo built from the choices the metadata
declares. When the value on disk is not one of them — a provider renamed in a
later release, a config edited by hand — `findData` returns -1, the combo
stays on index 0, and saving writes whatever that first option happens to be.

The user opened Settings to change something else entirely, pressed Save, and
a working setting became the default silently. `stt_provider: "groq"` is the
instance that shipped; the shape applies to every renamed enum value.
"""

import pytest

# CI runs `pytest -m unit`. Without this every test in this file is
# deselected there and the guards below only fire on a local pre-push hook.
pytestmark = pytest.mark.unit


def _combo_for(stored):
    """Build the stt_provider combo as Settings would, with `stored` on disk."""
    from desktop_app.settings_window import SettingsWindow, _build_field_metadata

    field = next(f for f in _build_field_metadata() if f.key == "stt_provider")
    window = SettingsWindow.__new__(SettingsWindow)
    window._merged = {"stt_provider": stored}
    return SettingsWindow._create_widget(window, field)


class TestAnUnrecognisedStoredValueSurvives:
    """A value the combo cannot display must not be silently replaced."""

    def test_the_combo_shows_the_stored_value_rather_than_the_first_option(self, qapp):
        """Sitting on index 0 is what turns a stale name into a lost setting."""
        widget = _combo_for("groq")

        assert widget.currentData() == "groq", (
            f"stored 'groq' displayed as {widget.currentData()!r}; pressing Save "
            "would write that over the user's setting"
        )

    def test_a_recognised_value_still_selects_normally(self, qapp):
        widget = _combo_for("openai_compatible")
        assert widget.currentData() == "openai_compatible"

    def test_an_empty_value_falls_to_the_first_option(self, qapp):
        """Nothing stored is not the same as something unrecognised."""
        widget = _combo_for("")
        assert widget.currentData() == "local"
