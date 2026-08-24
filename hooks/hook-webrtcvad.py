"""Shadow PyInstaller's bundled hook for webrtcvad.

The contrib hook is one line, `copy_metadata('webrtcvad')`, and it runs at
module import. `requirements.txt` installs `webrtcvad-wheels`, which is
the same source published as prebuilt wheels so that a clean machine does
not need a C compiler. The *import* name is unchanged, which is why every
test and every runtime path is unaffected - but the *distribution* name
is not, so the lookup raises, the hook fails to import, and PyInstaller
aborts the whole build:

    PyInstaller.exceptions.ImportErrorWhenRunningHook: Failed to import
    module __PyInstaller_hooks_0_webrtcvad required by hook for module
    _pyinstaller_hooks_contrib/stdhooks/hook-webrtcvad.py

Naming either distribution works, so this asks for both and takes
whichever is present. Anyone reverting to the sdist keeps a working
build, and so does anyone installing the wheels fork.
"""

from PyInstaller.utils.hooks import copy_metadata

datas = []
for _distribution in ("webrtcvad-wheels", "webrtcvad"):
    try:
        datas = copy_metadata(_distribution)
        break
    except Exception:
        # Not this one. Only both failing is a real problem, and then the
        # build should carry on and fail on the missing module rather than
        # here, where the message would blame the hook.
        continue
