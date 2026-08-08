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
    """Both the pending proposal and the trust window are module state."""
    cu._pending = None
    cu._trusted_until = 0.0
    yield
    cu._pending = None
    cu._trusted_until = 0.0


def _ctx(mode="risky"):
    ctx = Mock(spec=ToolContext)
    ctx.user_print = Mock()
    ctx.cfg = Mock()
    ctx.cfg.computer_use_confirm = mode
    return ctx


def _shown(capsys) -> str:
    """What actually reached the user's screen.

    The gate announces on stderr rather than through context.user_print,
    which is silenced under voice_debug — a code nobody can read would
    lock the tool instead of failing loudly.
    """
    return capsys.readouterr().err


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
    def test_the_code_is_shown_to_the_user_not_returned_to_the_model(self, mock_pg, capsys):
        """The gate only works if the model cannot read the code.

        If it appeared in the tool result, the model could quote it back
        to itself and approve its own action.
        """
        result = ComputerUseTool().run({"action": "click", "x": 5, "y": 6}, _ctx())

        code = cu._pending[0]
        assert code in _shown(capsys), "user was never shown the code"
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
    def test_a_destructive_key_is_flagged_to_the_user(self, mock_pg, capsys):
        """The human is the safeguard, so tell them what they are approving."""
        ComputerUseTool().run({"action": "key", "key": "delete"}, _ctx())

        assert "⚠️" in _shown(capsys)


@pytest.mark.unit
class TestTrustWindow:
    """Confirming every scroll is unusable, and teaches people to approve
    without reading. One approval covers ordinary actions for a while;
    anything that can submit or destroy still asks."""

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_approving_once_covers_later_clicks(self, mock_pg):
        pg = Mock()
        mock_pg.return_value = pg
        tool = ComputerUseTool()

        tool.run({"action": "click", "x": 1, "y": 2}, _ctx())
        code = cu._pending[0]
        tool.run({"action": "click", "x": 1, "y": 2, "confirmation_code": code}, _ctx())

        # A different click, with no code at all.
        result = tool.run({"action": "click", "x": 300, "y": 400}, _ctx())

        assert result.success is True
        assert "PROPOSED" not in (result.reply_text or "")
        pg.click.assert_called_with(300, 400)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_scrolling_is_covered_too(self, mock_pg):
        pg = Mock()
        mock_pg.return_value = pg
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 1, "y": 2}, _ctx())
        tool.run({"action": "click", "x": 1, "y": 2, "confirmation_code": cu._pending[0]}, _ctx())

        tool.run({"action": "scroll", "amount": -5}, _ctx())

        pg.scroll.assert_called_once_with(-5)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_typing_still_asks_inside_the_window(self, mock_pg):
        """A wrong click is a wrong click; a wrong Enter sends an email."""
        pg = Mock()
        mock_pg.return_value = pg
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 1, "y": 2}, _ctx())
        tool.run({"action": "click", "x": 1, "y": 2, "confirmation_code": cu._pending[0]}, _ctx())

        result = tool.run({"action": "type", "text": "transfer everything"}, _ctx())

        assert "PROPOSED" in (result.reply_text or "")
        pg.write.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_key_presses_still_ask_inside_the_window(self, mock_pg):
        pg = Mock()
        mock_pg.return_value = pg
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 1, "y": 2}, _ctx())
        tool.run({"action": "click", "x": 1, "y": 2, "confirmation_code": cu._pending[0]}, _ctx())

        result = tool.run({"action": "key", "key": "enter"}, _ctx())

        assert "PROPOSED" in (result.reply_text or "")
        pg.press.assert_not_called()

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_an_expired_window_asks_again(self, mock_pg):
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 1, "y": 2}, _ctx())
        tool.run({"action": "click", "x": 1, "y": 2, "confirmation_code": cu._pending[0]}, _ctx())
        cu._trusted_until = 0.0  # walked away

        result = tool.run({"action": "click", "x": 5, "y": 5}, _ctx())

        assert "PROPOSED" in (result.reply_text or "")

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_approving_a_type_does_not_open_the_window(self, mock_pg):
        """Approving one risky action must not silently unlock clicking."""
        tool = ComputerUseTool()
        tool.run({"action": "type", "text": "hi"}, _ctx())
        tool.run({"action": "type", "text": "hi", "confirmation_code": cu._pending[0]}, _ctx())

        result = tool.run({"action": "click", "x": 9, "y": 9}, _ctx())

        assert "PROPOSED" in (result.reply_text or "")

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_the_user_is_told_the_window_opened(self, mock_pg):
        ctx = _ctx()
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 1, "y": 2}, _ctx())
        tool.run({"action": "click", "x": 1, "y": 2, "confirmation_code": cu._pending[0]}, ctx)

        assert "not ask again" in "\n".join(str(c.args[0]) for c in ctx.user_print.call_args_list)


@pytest.mark.unit
class TestConfirmModes:
    """`computer_use_confirm` chooses how much is gated."""

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_never_executes_immediately(self, mock_pg):
        """Opt-out for a trusted single-user machine."""
        pg = Mock()
        mock_pg.return_value = pg

        result = ComputerUseTool().run(
            {"action": "click", "x": 7, "y": 8}, _ctx("never"),
        )

        assert result.success is True
        assert "PROPOSED" not in (result.reply_text or "")
        pg.click.assert_called_once_with(7, 8)

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_never_also_covers_typing(self, mock_pg):
        pg = Mock()
        mock_pg.return_value = pg

        ComputerUseTool().run({"action": "type", "text": "hi"}, _ctx("never"))

        assert pg.write.call_args[0][0] == "hi"

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_always_asks_again_after_an_approval(self, mock_pg):
        """No trust window in this mode — every action is proposed."""
        pg = Mock()
        mock_pg.return_value = pg
        tool = ComputerUseTool()
        ctx = _ctx("always")

        tool.run({"action": "click", "x": 1, "y": 2}, ctx)
        tool.run({"action": "click", "x": 1, "y": 2, "confirmation_code": cu._pending[0]}, ctx)
        result = tool.run({"action": "click", "x": 3, "y": 4}, ctx)

        assert "PROPOSED" in (result.reply_text or "")

    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_an_unknown_mode_falls_back_to_gated(self, mock_pg):
        """A typo in config must not silently disable the gate."""
        result = ComputerUseTool().run(
            {"action": "click", "x": 1, "y": 2}, _ctx("nevr"),
        )

        assert "PROPOSED" in (result.reply_text or "")
        mock_pg.assert_not_called()


@pytest.mark.unit
class TestGateCannotBeBruteForced:
    @patch("src.jarvis.tools.builtin.computer_use._pyautogui")
    def test_a_wrong_code_burns_the_proposal(self, mock_pg):
        """9000 codes with unlimited retries inside the TTL is not a gate.

        A model acting on an injected "click here" could spend the whole
        space in one reply loop, so one wrong guess cancels the request.
        """
        tool = ComputerUseTool()
        tool.run({"action": "click", "x": 5, "y": 6}, _ctx())
        real_code = cu._pending[0]

        tool.run({"action": "click", "x": 5, "y": 6, "confirmation_code": "0000"}, _ctx())

        assert cu._pending is None, "proposal survived a wrong guess"

        # Even the genuine code is now worthless.
        result = tool.run(
            {"action": "click", "x": 5, "y": 6, "confirmation_code": real_code}, _ctx(),
        )
        assert result.success is False
        mock_pg.assert_not_called()
