"""
Tests that the voice/text toggle survives being clicked more than once.

The two mode buttons are independently checkable, so nothing stops a user
clicking the one already selected. The handler snapshots each speech control's
visibility before hiding it, and restores from that snapshot on the way back —
so a second click on "Text only" snapshots the *hidden* state over the real
one, and switching back to voice leaves every control hidden for good.

The snapshot exists for the MLX section, which has its own platform rule and
must stay hidden on a machine that cannot use it. So it cannot simply be
replaced with "show everything".
"""

import pytest

# CI runs `pytest -m unit`. Without this every test in this file is
# deselected there and the guards below only fire on a local pre-push hook.
pytestmark = pytest.mark.unit


def _page():
    from desktop_app.setup_wizard import WhisperSetupPage

    return WhisperSetupPage()


class TestTheToggleIsIdempotent:
    """Clicking a mode twice must mean the same as clicking it once."""

    def test_text_only_twice_then_voice_restores_the_controls(self, qapp):
        """The bug: the second click overwrites the saved visibility."""
        page = _page()
        before = [w.isHidden() for w in page._voice_only_widgets]

        page._on_voice_mode_changed(False)
        page._on_voice_mode_changed(False)
        page._on_voice_mode_changed(True)

        assert page.voice_wanted() is True
        after = [w.isHidden() for w in page._voice_only_widgets]
        assert after == before, (
            "toggling text-only twice and back did not restore the speech "
            f"controls: before={before} after={after}"
        )

    def test_voice_twice_is_also_harmless(self, qapp):
        page = _page()
        before = [w.isHidden() for w in page._voice_only_widgets]

        page._on_voice_mode_changed(True)
        page._on_voice_mode_changed(True)

        assert page.voice_wanted() is True
        assert [w.isHidden() for w in page._voice_only_widgets] == before

    def test_a_single_round_trip_still_works(self, qapp):
        """The behaviour that already worked must not regress."""
        page = _page()
        before = [w.isHidden() for w in page._voice_only_widgets]

        page._on_voice_mode_changed(False)
        assert all(w.isHidden() for w in page._voice_only_widgets), (
            "text-only must hide every speech control"
        )

        page._on_voice_mode_changed(True)
        assert [w.isHidden() for w in page._voice_only_widgets] == before


class TestPlatformHiddenSectionsStayHidden:
    """The snapshot's whole purpose: not un-hiding what the platform hid."""

    def test_a_section_hidden_before_the_toggle_is_not_revealed_by_it(self, qapp):
        """MLX is hidden on non-Apple hardware and must stay that way."""
        page = _page()
        section = page._voice_only_widgets[-1]
        section.setVisible(False)

        page._on_voice_mode_changed(False)
        page._on_voice_mode_changed(False)
        page._on_voice_mode_changed(True)

        assert section.isHidden(), (
            "a section the platform hid was revealed by toggling voice back on"
        )
