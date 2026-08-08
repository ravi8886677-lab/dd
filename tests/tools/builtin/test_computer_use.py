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
def _clear_pending():
    cu._pending = None
    yield
    cu._pending = None


def _ctx():
    ctx = Mock(spec=ToolContext)
    ctx.user_print = Mock()
    return ctx


def _shown(ctx) -> str:
    return "\n".join(str(c.args[0]) for c in ctx.user_print.call_args_list)


@pytest.mark.unit
class TestNothingHappensWithoutAHuman:
    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_bare_click_only_proposes(self, mock_pg):
        result = ComputerUseTool().run(
            {"action": "click", "x": 100, "y": 200, "target": "Play"}, _ctx(),
        )

        assert result.success is True
        assert "PROPOSED" in result.reply_text
        mock_pg.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_the_code_is_shown_to_the_user_not_returned_to_the_model(self, mock_pg):
        """The gate only works if the model cannot read the code.

        If it appeared in the tool result, the model could quote it back
        to itself and approve its own action.
        """
        ctx = _ctx()
        result = ComputerUseTool().run({"action": "click", "x": 5, "y": 6}, ctx)

        code = cu._pending[0]
        assert code in _shown(ctx), "user was never shown the code"
        assert code not in (result.reply_text or ""), "code leaked into the model's context"

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_wrong_code_does_nothing(self, mock_pg):
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 5, "y": 6}, _ctx())

        result = tool.run(
            {"action": "click", "x": 5, "y": 6, "confirmation_code": "0000"}, _ctx(),
        )

        assert result.success is False
        mock_pg.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_code_with_nothing_pending_does_nothing(self, mock_pg):
        result = ComputerUseTool().run(
            {"action": "click", "x": 5, "y": 6, "confirmation_code": "1234"}, _ctx(),
        )

        assert result.success is False
        mock_pg.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_an_expired_code_does_nothing(self, mock_pg):
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 5, "y": 6}, _ctx())
        code, desc, act, _issued = cu._pending
        cu._pending = (code, desc, act, 0.0)  # issued long ago

        result = tool.run(
            {"action": "click", "x": 5, "y": 6, "confirmation_code": code}, _ctx(),
        )

        assert result.success is False
        assert "expired" in (result.error_message or "").lower()
        mock_pg.assert_not_called()


@pytest.mark.unit
class TestApprovalCannotBeRedirected:
    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_code_for_one_click_cannot_authorise_another(self, mock_pg):
        """The user approved a specific action, not a licence to click.

        Without this, "click Play at (100,200)" could be approved and
        then spent on "click Delete at (900,50)".
        """
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 100, "y": 200, "target": "Play"}, _ctx())
        code = cu._pending[0]

        result = tool.run(
            {"action": "click", "x": 900, "y": 50, "target": "Delete",
             "confirmation_code": code}, _ctx(),
        )

        assert result.success is False
        mock_pg.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_click_code_cannot_authorise_typing(self, mock_pg):
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 10, "y": 10}, _ctx())
        code = cu._pending[0]

        result = tool.run(
            {"action": "type", "text": "rm -rf /", "confirmation_code": code}, _ctx(),
        )

        assert result.success is False
        mock_pg.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_code_is_single_use(self, mock_pg):
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 10, "y": 10}, _ctx())
        code = cu._pending[0]

        first = tool.run({"action": "click", "x": 10, "y": 10, "confirmation_code": code}, _ctx())
        second = tool.run({"action": "click", "x": 10, "y": 10, "confirmation_code": code}, _ctx())

        assert first.success is True
        assert second.success is False


@pytest.mark.unit
class TestConfirmedActions:
    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_confirmed_click_actually_clicks(self, mock_pg):
        pg = Mock()
        mock_pg.return_value = pg
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 42, "y": 99}, _ctx())
        code = cu._pending[0]

        result = tool.run(
            {"action": "click", "x": 42, "y": 99, "confirmation_code": code}, _ctx(),
        )

        assert result.success is True
        pg.click.assert_called_once_with(42, 99)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_confirmed_type_types(self, mock_pg):
        pg = Mock()
        mock_pg.return_value = pg
        tool = ComputerUseTool()
        tool.run({"action": "type", "text": "hello"}, _ctx())
        code = cu._pending[0]

        tool.run({"action": "type", "text": "hello", "confirmation_code": code}, _ctx())

        assert pg.write.call_args[0][0] == "hello"


@pytest.mark.unit
class TestHeadlessAndUnknownInput:
    def test_screenshot_explains_itself_when_there_is_no_display(self):
        """Over SSH or in a container this cannot work; say so plainly."""
        with patch("src.jarvis.tools.builtin.computer_use._pyautogui",
                   side_effect=Exception("no display")):
            result = ComputerUseTool().run({"action": "screenshot"}, _ctx())

        assert result.success is False
        assert "desktop session" in (result.error_message or "")

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_an_unknown_action_does_nothing(self, mock_pg):
        result = ComputerUseTool().run({"action": "format_disk"}, _ctx())

        assert result.success is False
        mock_pg.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_destructive_key_is_flagged_to_the_user(self, mock_pg):
        """The human is the safeguard, so tell them what they are approving."""
        ctx = _ctx()
        ComputerUseTool().run({"action": "key", "key": "delete"}, ctx)

        assert "⚠️" in _shown(ctx)
