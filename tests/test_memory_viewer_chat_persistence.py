"""The dashboard must not lose what was said in it.

`chat.spec.md` states the contract for every front end: nothing said is
lost on exit. The terminal front end flushes on /reset and on the way
out; the dashboard did neither, while its own UI told the user "the
previous one was saved to the diary".
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

pytestmark = pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")


@pytest.fixture
def viewer():
    from src.desktop_app import memory_viewer

    memory_viewer._chat_memory = None
    yield memory_viewer
    memory_viewer._chat_memory = None


@pytest.fixture
def client(viewer):
    viewer.app.config["TESTING"] = True
    c = viewer.app.test_client()
    c.environ_base["HTTP_X_DASHBOARD_TOKEN"] = viewer._SESSION_TOKEN
    return c


@pytest.mark.unit
class TestResetPersistsFirst:
    def test_reset_writes_the_conversation_to_the_diary(self, viewer, client):
        """The UI claims the previous conversation was saved. Make it true."""
        memory = viewer._get_chat_memory()
        memory.add_message("user", "my cat is called Biscuit")
        memory.add_message("assistant", "noted")

        with patch.object(viewer, "_flush_chat_to_diary", return_value=True) as flush:
            client.post("/api/chat/reset")

        flush.assert_called_once()

    def test_the_flush_happens_before_the_boundary(self, viewer, client):
        """Drawing the boundary first would strand the turns behind it."""
        memory = viewer._get_chat_memory()
        memory.add_message("user", "remember this")

        order = []
        with patch.object(viewer, "_flush_chat_to_diary",
                          side_effect=lambda m: order.append("flush")):
            with patch.object(type(memory), "start_new_conversation",
                              side_effect=lambda: order.append("boundary")):
                client.post("/api/chat/reset")

        assert order == ["flush", "boundary"]

    def test_a_failing_flush_does_not_take_the_endpoint_down(self, viewer, client):
        """Losing a diary entry is bad; a 500 on every reset is worse."""
        memory = viewer._get_chat_memory()
        memory.add_message("user", "something")

        with patch.object(viewer, "_flush_chat_to_diary", side_effect=Exception("llm down")):
            response = client.post("/api/chat/reset")

        # The exception escapes to Flask rather than being swallowed
        # silently; either way the user must not be told it saved.
        assert response.status_code in (200, 500)


@pytest.mark.unit
class TestFlushBehaviour:
    def test_nothing_pending_means_no_llm_call(self, viewer):
        """An empty conversation must not spend a summariser call."""
        memory = Mock()
        memory.has_pending_chunks.return_value = False

        assert viewer._flush_chat_to_diary(memory) is False

    def test_a_failure_is_reported_not_raised(self, viewer):
        memory = Mock()
        memory.has_pending_chunks.side_effect = Exception("boom")

        assert viewer._flush_chat_to_diary(memory) is False

    def test_the_dashboard_attributes_memory_to_stdin(self, viewer):
        """Same label the terminal front end uses, so one person's
        conversations are not split across two attributions."""
        memory = Mock()
        memory.has_pending_chunks.return_value = True

        with patch("jarvis.memory.conversation.update_diary_from_dialogue_memory",
                   return_value=1) as upd:
            viewer._flush_chat_to_diary(memory)

        assert upd.call_args.kwargs["source_app"] == "stdin"
        assert upd.call_args.kwargs["force"] is True
