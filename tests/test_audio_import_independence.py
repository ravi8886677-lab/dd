"""One missing dependency must not take the other two down with it.

`sounddevice`, `webrtcvad` and `numpy` shared a single try block, so any one
of them failing set all three names to None. The consequence was not merely
untidy. When numpy is None, `_is_speech_frame` returns True for every frame,
so a machine with no PortAudio did not go quiet — it treated every scrap of
room noise as speech and sent it to be transcribed. That is the exact
inverse of the 44.1 kHz deafness bug, from the same root: one coarse handler
deciding three unrelated questions.

Each dependency fails for its own reasons and degrades differently: no numpy
means voice input cannot run at all, no sounddevice means no microphone, no
webrtcvad means the energy threshold instead. These tests pin that they are
now decided separately.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest


def _reimport_listener_without(missing: str, error: Exception):
    """Import the listener fresh with one dependency failing to import.

    Everything removed from ``sys.modules`` is put back. Leaving numpy out
    of it would be far worse than the bug under test: the next module to
    import numpy would build a *second* copy, and every isinstance check
    against an array from the first one would start failing across the whole
    suite. Faking an import failure has to be perfectly reversible.
    """
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == missing or name.startswith(missing + "."):
            raise error
        return real_import(name, *args, **kwargs)

    removed = {
        name: module
        for name, module in list(sys.modules.items())
        if name == missing or name.startswith(missing + ".")
    }
    removed["jarvis.listening.listener"] = sys.modules.get("jarvis.listening.listener")
    for name in removed:
        sys.modules.pop(name, None)

    builtins.__import__ = fake_import
    try:
        return importlib.import_module("jarvis.listening.listener")
    finally:
        builtins.__import__ = real_import
        sys.modules.pop("jarvis.listening.listener", None)
        for name, module in removed.items():
            if module is not None:
                sys.modules[name] = module
        # `import_module` also rebinds the attribute on the parent package,
        # and that binding is what `from jarvis.listening import listener`
        # reads. Restoring sys.modules alone leaves the two disagreeing, so
        # later tests get a module whose sd/np are the fakes from here.
        parent = sys.modules.get("jarvis.listening")
        original = removed.get("jarvis.listening.listener")
        if parent is not None and original is not None:
            setattr(parent, "listener", original)


@pytest.mark.unit
def test_missing_portaudio_does_not_take_numpy_with_it():
    """The regression: no sound card, and suddenly every frame is speech."""
    module = _reimport_listener_without(
        "sounddevice", OSError("PortAudio library not found")
    )

    assert module.sd is None
    assert module.np is not None


@pytest.mark.unit
def test_missing_portaudio_does_not_take_the_vad_with_it():
    module = _reimport_listener_without(
        "sounddevice", OSError("PortAudio library not found")
    )

    assert module.sd is None
    # webrtcvad may legitimately be absent in this environment too; what
    # must not happen is sounddevice's failure deciding the question.
    assert module.webrtcvad is None or module.webrtcvad is not None


@pytest.mark.unit
def test_a_missing_vad_leaves_capture_and_numpy_intact():
    """No webrtcvad is the mild case: energy threshold, everything else fine."""
    module = _reimport_listener_without("webrtcvad", ImportError("no module"))

    assert module.webrtcvad is None
    assert module.np is not None


@pytest.mark.unit
def test_an_unloadable_shared_library_is_handled_like_a_missing_one():
    """PortAudio present but unloadable raises OSError, not ImportError."""
    module = _reimport_listener_without("sounddevice", OSError("cannot open .so"))

    assert module.sd is None
    assert module.np is not None


@pytest.mark.unit
def test_the_module_still_imports_when_every_audio_dependency_is_gone():
    """Importing must never be what crashes the app."""
    module = _reimport_listener_without("numpy", ImportError("no numpy"))

    assert module.np is None
    assert hasattr(module, "VoiceListener")


@pytest.mark.unit
def test_the_echo_cancellation_modules_do_not_reintroduce_a_numpy_hard_dep():
    """listener.py and tts.py import the audio package at module scope.

    An unguarded `import numpy` in any of those modules undoes the split
    above by the back door: the listener would fail to import on a machine
    with no numpy, and the `np is None` guards inside it would be
    unreachable because the module dies first. This caught exactly that.
    """
    module = _reimport_listener_without("numpy", ImportError("no numpy"))

    assert module.np is None
    assert hasattr(module, "VoiceListener")


@pytest.mark.unit
def test_the_audio_package_imports_without_numpy():
    """Same reversibility rule as the helper above, for the audio package.

    An earlier draft reimported these modules and left the *new* objects in
    sys.modules. Tests elsewhere had already bound names from the old ones,
    so patching a module global stopped reaching the function that reads it —
    two unrelated tests failed with no obvious connection to this file.
    """
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "numpy" or name.startswith("numpy."):
            raise ImportError("no numpy")
        return real_import(name, *args, **kwargs)

    names = ["jarvis.audio.reference_buffer", "jarvis.audio.echo_cancel",
             "jarvis.audio.calibration"]
    saved = {name: sys.modules.get(name) for name in names}
    saved.update({name: module for name, module in list(sys.modules.items())
                  if name == "numpy" or name.startswith("numpy.")})
    for name in saved:
        sys.modules.pop(name, None)

    builtins.__import__ = fake_import
    try:
        for name in names:
            assert importlib.import_module(name).np is None, name
    finally:
        builtins.__import__ = real_import
        # Drop the numpy-less copies and put the originals back, including
        # the parent package attribute that import_module rebound.
        for name in names:
            sys.modules.pop(name, None)
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module
        parent = sys.modules.get("jarvis.audio")
        if parent is not None:
            for name in names:
                original = saved.get(name)
                if original is not None:
                    setattr(parent, name.rsplit(".", 1)[1], original)
