"""
Tests for what a plain `pip install -r requirements.txt` actually costs.

The base install is what every user pays for, including the ones who will
never touch the feature that dragged a dependency in. Optional features live
in their own requirements files, and this pins that split so a heavyweight
dependency cannot drift back into the base install unnoticed.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Packages that pull a deep-learning runtime (torch, and through it the CUDA
# libraries, triton and transformers). Measured: they are the difference
# between a 1.4 GB install and a 6.8 GB one.
_HEAVY_PACKAGES = {"chatterbox-tts", "torch", "torchaudio", "transformers"}


def _requirement_names(path: Path) -> set:
    """Distribution names listed in a requirements file, lowercased."""
    names = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!\[;\s]", line, 1)[0].strip().lower()
        if name:
            names.add(name)
    return names


class TestBaseInstallStaysLight:
    """Everyone pays for requirements.txt, so it carries no optional weight."""

    def test_the_base_install_pulls_no_deep_learning_runtime(self):
        """A default install uses Piper for speech, which needs none of this."""
        listed = _requirement_names(REPO_ROOT / "requirements.txt")
        assert not (listed & _HEAVY_PACKAGES), (
            f"requirements.txt lists {sorted(listed & _HEAVY_PACKAGES)}, which pulls "
            "torch and the CUDA stack into every install"
        )

    def test_the_chat_subset_stays_a_subset(self):
        """The audio-free set must not grow past the full one."""
        base = _requirement_names(REPO_ROOT / "requirements.txt")
        chat = _requirement_names(REPO_ROOT / "requirements-chat.txt")
        assert not (chat & _HEAVY_PACKAGES)
        assert chat - base == set() or chat.issubset(base | {"webrtcvad-wheels"})


class TestOptionalFilesExist:
    """A requirements file the docs name has to be there to install."""

    @pytest.mark.parametrize("filename", [
        "requirements-chat.txt",
        "requirements-dev.txt",
        "requirements-chatterbox.txt",
        "requirements-aec.txt",
    ])
    def test_the_file_exists(self, filename):
        assert (REPO_ROOT / filename).is_file(), f"{filename} is referenced but missing"

    def test_every_referenced_requirements_file_exists(self):
        """Catches a comment pointing at a file nobody ever created."""
        referenced = set()
        for path in REPO_ROOT.glob("requirements*.txt"):
            referenced |= set(re.findall(r"requirements-[a-z0-9]+\.txt", path.read_text()))
        missing = {n for n in referenced if not (REPO_ROOT / n).is_file()}
        assert not missing, f"referenced but missing: {sorted(missing)}"

    def test_the_optional_tts_file_carries_the_heavy_dependency(self):
        """Moving it out of the base install only works if it lands somewhere."""
        listed = _requirement_names(REPO_ROOT / "requirements-chatterbox.txt")
        assert "chatterbox-tts" in listed
