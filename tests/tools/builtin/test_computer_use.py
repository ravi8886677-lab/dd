"""Behaviour of the computerUse tool.

This one moves a real mouse on a real desktop. A wrong coordinate does
not produce a wrong sentence — it clicks something. So most of what
matters is that nothing executes without a human in the loop.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.builtin import computer_use as cu
from src.jarvis.tools.builtin.computer_use import ComputerUseTool


@pytest.fixture(autouse=True)
def _clear_state():
    """The YOLO window is process state and must not leak between tests."""
    cu.approval.revoke()
    yield
    cu.approval.revoke()


def _no_actions(mock_pg) -> None:
    """Nothing was clicked, typed, or scrolled.

    `_pyautogui` is also used read-only to look up the screen size when
    validating coordinates, so its being called proves nothing. What
    matters is that no action method fired.
    """
    pg = mock_pg.return_value
    for method in ("click", "doubleClick", "rightClick", "moveTo", "write", "press", "scroll"):
        getattr(pg, method).assert_not_called()


def _ctx(mode="risky"):
    ctx = Mock(spec=ToolContext)
    ctx.user_print = Mock()
    ctx.cfg = Mock()
    ctx.cfg.computer_use_confirm = mode
    return ctx


def _shown(capsys) -> str:
    """What actually reached the user's screen.

    The tool announces on stderr rather than through context.user_print,
    which is silenced under voice_debug.
    """
    return capsys.readouterr().err


@pytest.mark.unit
class TestNothingHappensWithoutYolo:
    """A real mouse move needs the user to have opened the window."""

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_click_does_nothing_while_yolo_is_off(self, mock_pg):
        result = ComputerUseTool().run(
            {"action": "click", "x": 10, "y": 10}, _ctx()
        )
        _no_actions(mock_pg)
        assert result.success is False
        assert "YOLO" in (result.error_message or "")

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_typing_does_nothing_while_yolo_is_off(self, mock_pg):
        result = ComputerUseTool().run({"action": "type", "text": "hello"}, _ctx())
        _no_actions(mock_pg)
        assert result.success is False

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_the_model_is_told_it_cannot_enable_it_itself(self, mock_pg):
        result = ComputerUseTool().run(
            {"action": "click", "x": 10, "y": 10}, _ctx()
        )
        assert "cannot turn it on yourself" in (result.error_message or "").lower()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_no_argument_can_stand_in_for_the_grant(self, mock_pg):
        """There is no per-call escape hatch for the model to invent."""
        result = ComputerUseTool().run(
            {"action": "click", "x": 10, "y": 10, "confirmation_code": "1234",
             "yolo": True, "approved": True},
            _ctx(),
        )
        _no_actions(mock_pg)
        assert result.success is False

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_the_user_is_told_on_screen_what_was_refused(self, mock_pg, capsys):
        ComputerUseTool().run({"action": "click", "x": 10, "y": 10}, _ctx())
        assert "YOLO" in _shown(capsys)


@pytest.mark.unit
class TestWithYoloOn:
    """Inside the window, Jarvis gets on with it."""

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_click_actually_clicks(self, mock_pg):
        cu.approval.grant(15)
        result = ComputerUseTool().run(
            {"action": "click", "x": 10, "y": 20}, _ctx()
        )
        mock_pg.return_value.click.assert_called_once()
        assert result.success is True

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_typing_actually_types(self, mock_pg):
        cu.approval.grant(15)
        ComputerUseTool().run({"action": "type", "text": "hello"}, _ctx())
        mock_pg.return_value.write.assert_called_once()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_the_window_closing_stops_it_again(self, mock_pg, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(cu.approval.time, "time", lambda: clock["now"])
        cu.approval.grant(15)
        clock["now"] += 16 * 60

        result = ComputerUseTool().run(
            {"action": "click", "x": 10, "y": 10}, _ctx()
        )
        _no_actions(mock_pg)
        assert result.success is False


@pytest.mark.unit
class TestConfirmModes:
    """`computer_use_confirm: never` opts out of the gate entirely."""

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_never_executes_immediately(self, mock_pg):
        result = ComputerUseTool().run(
            {"action": "click", "x": 10, "y": 10}, _ctx(mode="never")
        )
        mock_pg.return_value.click.assert_called_once()
        assert result.success is True

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_never_also_covers_typing(self, mock_pg):
        ComputerUseTool().run({"action": "type", "text": "hi"}, _ctx(mode="never"))
        mock_pg.return_value.write.assert_called_once()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_an_unknown_mode_still_gates(self, mock_pg):
        result = ComputerUseTool().run(
            {"action": "click", "x": 10, "y": 10}, _ctx(mode="banana")
        )
        _no_actions(mock_pg)
        assert result.success is False


@pytest.mark.unit
class TestHeadlessAndUnknownInput:
    def test_screenshot_explains_itself_when_there_is_no_display(self):
        with patch("src.jarvis.tools.builtin.computer_use._pyautogui") as pg:
            pg.side_effect = RuntimeError("no display")
            result = ComputerUseTool().run({"action": "screenshot"}, _ctx())
        assert result.success is False

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_an_unknown_action_does_nothing(self, mock_pg):
        result = ComputerUseTool().run({"action": "levitate"}, _ctx())
        _no_actions(mock_pg)
        assert result.success is False


@pytest.mark.unit
class TestCoordinatesAreChecked:
    """YOLO cannot catch a bad coordinate: inside the window every click
    runs, so an unusable one has to be refused on its own merits."""

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_click_without_coordinates_is_refused(self, mock_pg):
        """pyautogui treats a missing coordinate as "wherever the pointer
        already is", so this would click whatever is under the cursor."""
        result = ComputerUseTool().run(
            {"action": "click", "target": "Send"}, _ctx(),
        )

        assert result.success is False
        assert "needs both x and y" in (result.error_message or "")
        _no_actions(mock_pg)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_partial_coordinate_is_refused(self, mock_pg):
        """x without y is as unusable as neither, and just as silent."""
        result = ComputerUseTool().run({"action": "click", "x": 100}, _ctx())

        assert result.success is False
        _no_actions(mock_pg)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_negative_coordinates_are_refused(self, mock_pg):
        result = ComputerUseTool().run({"action": "click", "x": -5, "y": 10}, _ctx())

        assert result.success is False
        _no_actions(mock_pg)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_coordinate_past_the_screen_edge_is_refused(self, mock_pg):
        """pyautogui silently clamps to an edge, turning a wildly wrong
        coordinate into a plausible-looking edge click."""
        mock_pg.return_value.size.return_value = (1920, 1080)

        result = ComputerUseTool().run({"action": "click", "x": 99999, "y": 5}, _ctx())

        assert result.success is False
        assert "outside" in (result.error_message or "").lower()
        _no_actions(mock_pg)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_typing_needs_no_coordinates(self, mock_pg):
        """Only pointer actions have a target; typing goes to focus."""
        cu.approval.grant(15)
        result = ComputerUseTool().run({"action": "type", "text": "hi"}, _ctx())

        assert result.success is True

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_valid_coordinate_is_accepted(self, mock_pg):
        """The refusals above are worthless if everything is refused."""
        mock_pg.return_value.size.return_value = (1920, 1080)

        cu.approval.grant(15)
        result = ComputerUseTool().run({"action": "click", "x": 500, "y": 400}, _ctx())

        assert result.success is True

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_validation_runs_before_the_gate(self, mock_pg):
        """A bad coordinate is a bad coordinate, whatever YOLO is doing.

        Reporting "YOLO is off" here would send the user to switch it on and
        hit the same wall, and asking someone to approve an action that
        cannot execute trains them to approve without reading.
        """
        cu.approval.revoke()
        result = ComputerUseTool().run({"action": "click", "target": "Send"}, _ctx())

        assert result.success is False
        assert "needs both x and y" in (result.error_message or "")
        _no_actions(mock_pg)

