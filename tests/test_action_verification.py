"""A tool that claims to have acted has to have looked.

`computerUse` used to report `Done: <description>` because pyautogui
returned, which is the shape of a fabricated completion claim: the words
say the world changed, the evidence says a function did not raise.

So a tool that can check, checks, and says what it found. A tool that
cannot check says so, and `not_checked` is what the log records - never
success by default. The distinction matters because the model is told it
may only claim what the record supports.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.audit import Verification
from jarvis.tools.types import ToolExecutionResult

pytestmark = pytest.mark.unit


def _run(tool, cfg, args, tmp_path=None):
    return tool.execute(
        db=None,
        cfg=cfg,
        tool_args=args,
        system_prompt="",
        original_prompt="",
        redacted_text="",
        max_retries=1,
        user_print=lambda _message: None,
    )


class TestWritingAFileIsChecked:
    def test_a_written_file_is_confirmed_by_looking_at_it(
        self, tmp_path, mock_config, monkeypatch,
    ):
        from jarvis.tools.builtin.local_files import LocalFilesTool

        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "note.txt"
        result = _run(
            LocalFilesTool(), mock_config,
            {"operation": "write", "path": str(target), "content": "hello"},
        )

        assert result.success is True
        assert result.verification == Verification.CONFIRMED.value

    def test_a_write_whose_content_did_not_land_is_not_confirmed(
        self, tmp_path, mock_config, monkeypatch,
    ):
        """The call returned cleanly and the bytes are not there."""
        from jarvis.tools.builtin.local_files import LocalFilesTool

        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "note.txt"
        real_write = Path.write_text

        def write_nothing(self, content, **kwargs):
            return real_write(self, "", **kwargs)

        monkeypatch.setattr(Path, "write_text", write_nothing)
        result = _run(
            LocalFilesTool(), mock_config,
            {"operation": "write", "path": str(target), "content": "hello"},
        )

        assert result.verification == Verification.FAILED.value

    def test_an_append_is_checked_by_size(self, tmp_path, mock_config, monkeypatch):
        from jarvis.tools.builtin.local_files import LocalFilesTool

        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "log.txt"
        target.write_text("one\n", encoding="utf-8")

        result = _run(
            LocalFilesTool(), mock_config,
            {"operation": "append", "path": str(target), "content": "two\n"},
        )

        assert result.verification == Verification.CONFIRMED.value

    def test_reading_claims_no_verification(self, tmp_path, mock_config, monkeypatch):
        """Reading changes nothing, so there is nothing to verify."""
        from jarvis.tools.builtin.local_files import LocalFilesTool

        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "note.txt"
        target.write_text("hello", encoding="utf-8")

        result = _run(
            LocalFilesTool(), mock_config,
            {"operation": "read", "path": str(target)},
        )

        assert result.verification is None


class TestOpeningAnAppIsCheckedWhereItCanBe:
    def test_a_launch_that_left_no_process_is_not_confirmed(self, mock_config):
        from jarvis.tools.builtin import open_app as module

        with patch.object(module, "_open_app", return_value="xdg-open"):
            with patch.object(module, "_process_is_running", return_value=False):
                result = _run(module.OpenAppTool(), mock_config, {"app": "browser"})

        assert result.success is True
        assert result.verification == Verification.FAILED.value

    def test_a_launch_with_a_running_process_is_confirmed(self, mock_config):
        from jarvis.tools.builtin import open_app as module

        with patch.object(module, "_open_app", return_value="xdg-open"):
            with patch.object(module, "_process_is_running", return_value=True):
                result = _run(module.OpenAppTool(), mock_config, {"app": "browser"})

        assert result.verification == Verification.CONFIRMED.value

    def test_no_way_to_check_reads_as_unchecked_not_as_success(self, mock_config):
        """`not_checked` is the honest answer, and it is not `confirmed`."""
        from jarvis.tools.builtin import open_app as module

        with patch.object(module, "_open_app", return_value="xdg-open"):
            with patch.object(module, "_process_is_running", return_value=None):
                result = _run(module.OpenAppTool(), mock_config, {"app": "browser"})

        assert result.verification is None


class TestTheBoundaryRecordsWhatTheToolFound:
    def test_a_confirmed_write_reaches_the_log(self, tmp_path, mock_config):
        from jarvis.audit import ActionLog, recorder
        from jarvis.tools import registry

        db_path = str(tmp_path / "jarvis.db")
        recorder.reset_for_tests()
        recorder.configure(db_path=db_path)
        log = ActionLog(db_path)
        try:
            with patch.object(
                registry.BUILTIN_TOOLS["localFiles"], "execute",
                return_value=ToolExecutionResult(
                    success=True, reply_text="wrote it",
                    verification=Verification.CONFIRMED.value,
                ),
            ):
                registry.run_tool_with_retries(
                    db=None, cfg=mock_config, tool_name="localFiles",
                    tool_args={"operation": "write"}, system_prompt="",
                    original_prompt="", redacted_text="",
                )

            assert log.get_actions()[0].verification == Verification.CONFIRMED
        finally:
            log.close()
            recorder.reset_for_tests()

    def test_a_tool_claiming_a_nonsense_verification_is_not_believed(
        self, tmp_path, mock_config,
    ):
        from jarvis.audit import ActionLog, recorder
        from jarvis.tools import registry

        db_path = str(tmp_path / "jarvis.db")
        recorder.reset_for_tests()
        recorder.configure(db_path=db_path)
        log = ActionLog(db_path)
        try:
            with patch.object(
                registry.BUILTIN_TOOLS["localFiles"], "execute",
                return_value=ToolExecutionResult(
                    success=True, reply_text="wrote it", verification="obviously",
                ),
            ):
                registry.run_tool_with_retries(
                    db=None, cfg=mock_config, tool_name="localFiles",
                    tool_args={}, system_prompt="", original_prompt="",
                    redacted_text="",
                )

            assert log.get_actions()[0].verification == Verification.NOT_CHECKED
        finally:
            log.close()
            recorder.reset_for_tests()
