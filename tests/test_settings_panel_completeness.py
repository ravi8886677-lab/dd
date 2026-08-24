"""The settings panel must be able to finish every choice it offers.

The dashboard let you pick an `embedding_provider` and an embedding
model, and gave you nowhere to put that provider's endpoint or its key.
Choosing anything other than the chat provider therefore produced a
configuration that could not work, and it failed silently: tool
selection falls open to the whole catalogue and memory enrichment
degrades, so the symptom is an assistant that is quietly worse at
remembering rather than an error anyone could trace back to a blank
field.

Speech recognition was worse - absent from the dashboard entirely, while
`settings_window.py` has had the fields all along. The dashboard was the
odd one out, which is only visible to someone configuring Jarvis without
a desktop session, which is exactly who needs the dashboard.

Both are the same defect as a button that does not do what it says: a
control implying a capability that is not there. So the rule asserted
here is structural rather than a list of fields - if the panel lets you
choose a provider for something, it must also let you say where that
provider is and what credential it takes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src/desktop_app/dashboard/templates/index.html"
SCRIPT = ROOT / "src/desktop_app/dashboard/static/dashboard.js"

#: Every provider the settings panel can point somewhere else.
PROVIDER_PREFIXES = ("llm", "embedding", "stt")


def _payload_fields() -> set[str]:
    """Config keys the panel's Save button actually sends."""
    body = SCRIPT.read_text(encoding="utf-8")
    start = body.index("function settingsPayload()")
    end = body.index("function setStatus(", start)
    return set(re.findall(r"^\s*(\w+):", body[start:end], re.M))


class TestEveryProviderChoiceCanBeCompleted:
    """Offering a provider without an endpoint and a key is a dead end."""

    @pytest.mark.parametrize("prefix", PROVIDER_PREFIXES)
    def test_provider_endpoint_and_key_travel_together(self, prefix: str) -> None:
        sent = _payload_fields()
        if f"{prefix}_provider" not in sent:
            pytest.skip(f"the panel does not offer a {prefix} provider")

        for companion in (f"{prefix}_base_url", f"{prefix}_api_key"):
            assert companion in sent, (
                f"the panel lets you choose {prefix}_provider but never sends "
                f"{companion}, so any provider other than the default cannot "
                "be reached. Add the field or remove the choice."
            )

    @pytest.mark.parametrize("prefix", PROVIDER_PREFIXES)
    def test_each_field_has_an_input_to_type_it_into(self, prefix: str) -> None:
        """A payload key with no control behind it always sends empty."""
        html = TEMPLATE.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        sent = _payload_fields()
        if f"{prefix}_provider" not in sent:
            pytest.skip(f"the panel does not offer a {prefix} provider")

        for field in (f"{prefix}_provider", f"{prefix}_base_url", f"{prefix}_api_key"):
            ids = re.findall(
                rf"document\.getElementById\('([\w-]+)'\)[^;]*?\n?\s*"
                rf"|{field}:\s*document\.getElementById\('([\w-]+)'\)",
                script,
            )
            match = re.search(
                rf"{field}:\s*document\.getElementById\('([\w-]+)'\)", script)
            assert match, f"{field} is sent but read from no element"
            element_id = match.group(1)
            assert f'id="{element_id}"' in html, (
                f"{field} reads #{element_id}, which the template does not "
                "contain, so it silently sends an empty value."
            )


class TestTheApiCannotOfferAnIncompleteProvider:
    """The allow-list is where the dead end actually lived.

    Pre-fix the API accepted `embedding_provider` and neither
    `embedding_base_url` nor `embedding_api_key`, so the choice was
    writable and unusable. Anchoring the rule on what the page happens to
    send misses that: the page sent no provider at all, so a rule phrased
    that way skips instead of failing.
    """

    @pytest.mark.parametrize("prefix", PROVIDER_PREFIXES)
    def test_a_writable_provider_has_a_writable_endpoint_and_key(self, prefix: str) -> None:
        from src.desktop_app import memory_viewer

        # Read defensively so a tree without these constants fails with the
        # diagnostic below rather than an AttributeError, which says nothing
        # about what is wrong.
        fields = set(getattr(memory_viewer, "_SETTINGS_FIELDS", ()))
        keys = set(getattr(memory_viewer, "_SETTINGS_KEY_FIELDS", ()))
        if f"{prefix}_provider" not in fields:
            pytest.skip(f"the API does not accept a {prefix} provider")

        assert f"{prefix}_base_url" in fields, (
            f"the API accepts {prefix}_provider but not {prefix}_base_url, so "
            "selecting a different provider gives no way to say where it is."
        )
        assert f"{prefix}_api_key" in keys, (
            f"the API accepts {prefix}_provider but not {prefix}_api_key, so "
            "selecting a different provider gives no way to authenticate."
        )


class TestTheBackendAcceptsWhatThePanelSends:
    """A field the page sends and the API drops is the same dead end."""

    def test_every_sent_field_is_writable(self) -> None:
        from src.desktop_app import memory_viewer

        writable = (
            set(memory_viewer._SETTINGS_FIELDS)
            | set(memory_viewer._SETTINGS_NUMERIC_FIELDS)
            | set(memory_viewer._SETTINGS_KEY_FIELDS)
        )
        dropped = sorted(_payload_fields() - writable)
        assert not dropped, (
            f"the panel sends {dropped}, which the API's allow-list discards. "
            "Saving appears to work and changes nothing."
        )


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestSavingAndReadingBack:
    @pytest.fixture
    def auth_client(self):
        from src.desktop_app import memory_viewer

        memory_viewer.app.config["TESTING"] = True
        with memory_viewer.app.test_client() as c:
            c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = memory_viewer._SESSION_TOKEN
            yield c

    def test_a_speech_endpoint_survives_a_round_trip(self, auth_client, tmp_path, monkeypatch) -> None:
        from src.desktop_app import memory_viewer

        config = tmp_path / "config.json"
        monkeypatch.setattr(memory_viewer, "_config_path", lambda: config)

        saved = auth_client.post("/api/settings", json={
            "stt_provider": "openai_compatible",
            "stt_base_url": "https://api.groq.com/openai/v1",
            "stt_model": "whisper-large-v3-turbo",
            "stt_timeout_sec": "7.5",
            "stt_api_key": "secret-value",
        })
        assert saved.status_code == 200

        read = auth_client.get("/api/settings").get_json()
        assert read["stt_provider"] == "openai_compatible"
        assert read["stt_base_url"] == "https://api.groq.com/openai/v1"
        assert read["stt_model"] == "whisper-large-v3-turbo"
        assert read["stt_timeout_sec"] == 7.5, "a number must not become a string"

    def test_the_speech_key_is_never_returned_to_the_page(self, auth_client, tmp_path, monkeypatch) -> None:
        from src.desktop_app import memory_viewer

        config = tmp_path / "config.json"
        monkeypatch.setattr(memory_viewer, "_config_path", lambda: config)

        auth_client.post("/api/settings", json={"stt_api_key": "sk-live-do-not-leak"})
        read = auth_client.get("/api/settings").get_json()

        assert "sk-live-do-not-leak" not in str(read)
        assert read["has_stt_api_key"] is True
        assert read["hint_stt_api_key"] == "…leak"

    def test_a_blank_key_does_not_erase_the_saved_one(self, auth_client, tmp_path, monkeypatch) -> None:
        from src.desktop_app import memory_viewer

        config = tmp_path / "config.json"
        monkeypatch.setattr(memory_viewer, "_config_path", lambda: config)

        auth_client.post("/api/settings", json={"stt_api_key": "keep-me"})
        auth_client.post("/api/settings", json={"stt_api_key": "", "stt_model": "other"})

        read = auth_client.get("/api/settings").get_json()
        assert read["has_stt_api_key"] is True, "changing a model wiped the key"
        assert read["stt_model"] == "other"
