#!/usr/bin/env python3
"""Show the confirmation dialog on a real screen, without a daemon.

The dialog is the one piece of the confirmation fix that unit tests
cannot judge: it has only ever run headless. This puts it on screen in
about ten seconds so the things that only fail visually can fail visibly.

    python scripts/preview_confirm_dialog.py

Run it on each OS you ship to. What to look for, in rough order of how
likely it is to be wrong:

1. It appears at all, top-right, without stealing your keyboard. Click
   into another app first, then trigger it — if it vanishes when this
   app loses focus, the window flags are wrong for that platform.
2. The code is readable across the room. That is the actual use case:
   someone reads four digits aloud to a voice assistant.
3. The countdown runs down and the window closes on its own.
4. Take a screenshot of your whole desktop while it is up, then look at
   the file. On Windows the dialog should be missing or blank. On macOS
   it may still appear — ``sharingType`` does not cover every capture
   path, which is why the screenshot tool also refuses while a code is
   live. On Linux it will appear; there is no OS mechanism.
5. Windows only: check the window is not rendering black on screen.
   ``WDA_EXCLUDEFROMCAPTURE`` behaves badly on some virtualised display
   drivers and over remote desktop.

Nothing here touches the gates, the daemon, or your config.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from desktop_app.confirm_dialog import ConfirmationDialog  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)

    dialog = ConfirmationDialog()
    dialog.present(
        "4821",
        "delete_file on filesystem with path=\"/Users/you/Documents/contracts\"",
        45.0,
    )

    print("Dialog is up for 45s. Checklist is in this file's docstring.")
    print("Click into another application now — it must stay visible.")

    # Exercise the dismiss path too, so the wrong-code appearance gets
    # looked at rather than assumed.
    QTimer.singleShot(
        20_000,
        lambda: (
            print("Simulating a wrong code — the dialog should close now."),
            dialog.expire("Wrong code — cancelled."),
            QTimer.singleShot(1500, app.quit),
        ),
    )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
