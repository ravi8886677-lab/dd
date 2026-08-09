"""The window a confirmation code appears in.

The gates print their code to stderr, which in the packaged app lands in
the hidden Log Viewer. This puts it in front of the user instead.

Three things here are deliberate and worth not undoing:

*Not modal, and it does not take focus.* The user has to type or say the
code back to Jarvis, so a dialog that blocks the chat window makes the
gate harder to open rather than easier. ``WA_ShowWithoutActivating`` plus
stay-on-top gets it seen without stealing the keyboard.

*Excluded from screen capture where the OS can do it.* A code drawn on
screen is a code a screen-reading tool can OCR back to the model. Windows
can hide a window from capture outright; macOS can keep it out of the
window-list capture path. Linux cannot, which is why the real defence is
the screenshot tool refusing while a code is live — this is the second
layer, not the first.

*A tray balloon as well as the window.* Focus Assist on Windows and Do
Not Disturb on macOS silently swallow notifications, and a window can be
missed on a second monitor. Neither channel is reliable alone.
"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .themes import COLORS, JARVIS_THEME_STYLESHEET


class ConfirmSignals(QObject):
    """Marshals gate callbacks onto the GUI thread.

    In the packaged app the daemon runs in a QThread, so both callbacks
    arrive off the main thread. Emitting a signal is the only safe way to
    touch a widget from there; connections are queued automatically
    because the receiver lives in the main thread.
    """

    proposed = pyqtSignal(str, str, float)  # code, description, ttl
    dismissed = pyqtSignal(str)             # reason


class ConfirmationDialog(QDialog):
    """Shows one pending confirmation code with a countdown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._remaining = 0.0
        self._total = 1.0
        self._code = ""

        self.setWindowTitle("Jarvis needs your approval")
        self.setMinimumWidth(460)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        # Appear without grabbing the keyboard: the user's next action is
        # to type or speak the code into Jarvis, not to interact here.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(JARVIS_THEME_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 22, 24, 20)

        self._heading = QLabel("⚠️ Jarvis wants to do something")
        self._heading.setStyleSheet(
            f"color: {COLORS['warning']}; font-size: 15px; font-weight: 600;"
        )
        layout.addWidget(self._heading)

        self._what = QLabel("")
        self._what.setWordWrap(True)
        self._what.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 13px;"
        )
        layout.addWidget(self._what)

        hint = QLabel("Read this code to Jarvis to allow it. Ignore it to do nothing.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 12px;")
        layout.addWidget(hint)

        self._code_label = QLabel("----")
        code_font = QFont()
        code_font.setFamilies(["SF Mono", "Consolas", "DejaVu Sans Mono", "monospace"])
        code_font.setPointSize(40)
        code_font.setBold(True)
        self._code_label.setFont(code_font)
        self._code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._code_label.setStyleSheet(
            f"color: {COLORS['accent_secondary']};"
            f"background: {COLORS['bg_tertiary']};"
            f"border: 1px solid {COLORS['border']};"
            "border-radius: 10px; padding: 14px; letter-spacing: 10px;"
        )
        layout.addWidget(self._code_label)

        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(4)
        layout.addWidget(self._bar)

        self._countdown = QLabel("")
        self._countdown.setStyleSheet(
            f"color: {COLORS['text_muted']}; font-size: 11px;"
        )
        layout.addWidget(self._countdown)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy_btn = QPushButton("Copy code")
        copy_btn.clicked.connect(self._copy)
        buttons.addWidget(copy_btn)
        close_btn = QPushButton("Ignore")
        close_btn.clicked.connect(self.hide)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)

    # -- capture exclusion -------------------------------------------------

    def _exclude_from_capture(self) -> None:
        """Best effort: keep this window out of screen captures.

        Called after the native window exists. Every failure path here is
        silent and acceptable — the screenshot tool's refusal is the
        defence that has to work, this only narrows the window further.
        """
        try:
            handle = int(self.winId())
        except Exception:  # noqa: BLE001
            return

        if sys.platform.startswith("win"):
            try:
                import ctypes

                # WDA_EXCLUDEFROMCAPTURE (0x11), Windows 10 2004+. The
                # window renders as nothing in captures rather than black.
                ctypes.windll.user32.SetWindowDisplayAffinity(handle, 0x11)
            except Exception:  # noqa: BLE001
                pass
        elif sys.platform == "darwin":
            try:
                import objc  # type: ignore
                from AppKit import NSApp  # type: ignore

                for window in NSApp().windows():
                    if window.windowNumber() and int(window.windowNumber()) == handle:
                        window.setSharingType_(0)  # NSWindowSharingNone
                        break
                del objc
            except Exception:  # noqa: BLE001
                pass

    def showEvent(self, event):  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._exclude_from_capture()

    # -- content -----------------------------------------------------------

    def present(self, code: str, description: str, ttl_sec: float) -> None:
        self._code = code
        self._code_label.setText(code)
        self._what.setText(description)
        self._total = max(1.0, float(ttl_sec))
        self._remaining = self._total
        self._bar.setRange(0, int(self._total * 5))
        self._bar.setValue(int(self._total * 5))
        self._position()
        self.show()
        self.raise_()
        self._timer.start()

    def _position(self) -> None:
        """Top-right of the primary screen, clear of the orb and chat."""
        try:
            area = QGuiApplication.primaryScreen().availableGeometry()
            self.adjustSize()
            self.move(area.right() - self.width() - 32, area.top() + 48)
        except Exception:  # noqa: BLE001
            pass

    def _tick(self) -> None:
        self._remaining -= 0.2
        if self._remaining <= 0:
            self._timer.stop()
            self.expire("expired")
            return
        self._bar.setValue(int(self._remaining * 5))
        self._countdown.setText(f"Expires in {int(self._remaining) + 1}s")

    def _copy(self) -> None:
        try:
            QApplication.clipboard().setText(self._code)
        except Exception:  # noqa: BLE001
            pass

    def expire(self, reason: str = "") -> None:
        self._timer.stop()
        self._code = ""
        self._code_label.setText("----")
        self._countdown.setText(reason)
        self.hide()


def install_confirmation_presenter(tray_app) -> Optional[ConfirmationDialog]:
    """Wire the gates' confirmation codes to a real window.

    ``tray_app`` needs a ``tray_icon``. Returns the dialog so the caller
    can keep a reference — without one it is garbage collected and the
    codes go back to being invisible.
    """
    from jarvis import confirm_ui

    dialog = ConfirmationDialog()
    signals = ConfirmSignals()
    dialog._signals = signals  # keep alive alongside the dialog

    def _on_proposed(code: str, description: str, ttl: float) -> None:
        dialog.present(code, description, ttl)
        try:
            from PyQt6.QtWidgets import QSystemTrayIcon

            tray_app.tray_icon.showMessage(
                "Jarvis needs your approval",
                # The code is deliberately not in the balloon: notification
                # text is persisted by the OS notification centre, which is
                # one more place a short-lived secret should not live.
                "Open the approval window to see your code.",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )
        except Exception:  # noqa: BLE001
            pass

    signals.proposed.connect(_on_proposed)
    signals.dismissed.connect(dialog.expire)

    # These two run on the daemon thread; emitting is the handoff.
    confirm_ui.set_presenter(
        present=lambda code, desc, ttl: signals.proposed.emit(code, desc, ttl),
        dismiss=lambda reason: signals.dismissed.emit(reason),
    )
    return dialog
