"""Cold-start replies must not anchor on the clock.

On a fresh install the memory graph is empty, so none of the memory blocks
(warm profile, diary, graph, digest) reach the system message. The persona
prompt licenses inventing a fresh observation whenever the stored-facts
section is absent, and the only concrete data left anywhere in the prompt is
the `[Context: ...]` line. The model does as instructed and narrates the time,
on every turn, because the material never changes. It is the first impression
for every new user, since everyone starts with an empty memory.

These tests pin the cold-start branch: when no memory is stored the system
message must supply material of its own and take the clock off the table, and
when memory *is* present that branch must stay out of the way so the "build
the reply around a stored fact" rule owns the turn.

The content assertions are anchored on what the cold path adds *over* the warm
path, not on the system message as a whole. The persona prompt already talks
about the context line and about answering when the user asks, so a whole-
message search matches that pre-existing text and passes against the bug.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# The persona prompt carries its own "Trenches Gym" example, so a sentinel
# using that phrase matches vacuously. These are unique to the tests.
MEMORY_QUERY = "what did I tell you about the club where I train on weekday evenings"
MEMORY_PLAN = ["searchMemory topic='fencing club'", "Reply to the user."]
WARM_SENTINEL = "Ravensworth Fencing Club"
WARM_PROFILE = f"What you know about the user:\n- Fences at {WARM_SENTINEL}"
DIARY_SENTINEL = "Ravensworth Fencing Club"
DIARY_ENTRY = f"[2026-08-20] The user mentioned they fence at {DIARY_SENTINEL}."


def _system_message(messages) -> str:
    for m in messages:
        if m.get("role") == "system" and not m.get("_is_tool_guidance"):
            return m.get("content", "")
    return ""


def _capture_system(
    mock_config, db, dialogue_memory, text="hi bro", keywords=(), plan=(), **patches
) -> str:
    """Run one reply against a stubbed chat model and return the system message.

    Everything except message construction is stubbed, so what comes back is
    exactly the prompt a fresh install would send for ``text``.

    Each call gets its own ``DialogueMemory``: the warm profile is cached for
    the lifetime of a conversation, so reusing one across a cold and a warm
    capture would serve the cold run's empty profile to both.
    """
    from jarvis.memory.conversation import DialogueMemory
    from jarvis.reply import engine as engine_mod

    dialogue_memory = DialogueMemory(inactivity_timeout=300, max_interactions=20)
    mock_config.evaluator_enabled = False
    captured: list[str] = []

    def fake_chat(*args, **kwargs):
        msgs = kwargs.get("messages") or (args[2] if len(args) > 2 else [])
        captured.append(_system_message(msgs))
        return {"message": {"content": "Something without a clock in it."}}

    with ExitStack() as stack:
        stack.enter_context(patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat))
        stack.enter_context(patch.object(engine_mod, "select_tools", return_value=["stop"]))
        stack.enter_context(patch.object(
            engine_mod, "extract_search_params_for_memory",
            return_value={"keywords": list(keywords), "questions": []},
        ))
        stack.enter_context(patch.object(engine_mod, "plan_query", return_value=list(plan)))
        stack.enter_context(patch.object(
            engine_mod, "_live_time_location_string",
            return_value="Tuesday, 26 August 2026 at 15:40 UTC, Location: Testville",
        ))
        for target, kwargs in patches.items():
            stack.enter_context(patch(target, **kwargs))
        engine_mod.run_reply_engine(
            db=db, cfg=mock_config, tts=None,
            text=text, dialogue_memory=dialogue_memory,
        )

    assert captured, "chat model should have been called"
    return captured[0]


def _warm_system(mock_config, db, dialogue_memory, text="hi bro") -> str:
    """The same prompt for a user the assistant already knows something about."""
    return _capture_system(
        mock_config, db, dialogue_memory, text=text,
        **{"jarvis.memory.graph_ops.format_warm_profile_block": {"return_value": WARM_PROFILE}},
    )


def _cold_start_segment(mock_config, db, dialogue_memory, text="hi bro") -> str:
    """Return the guidance the cold path adds that the warm path does not.

    Anchoring on the difference is what makes these assertions guards rather
    than decoration: the persona prompt already mentions the context line and
    already says "when the user asks", so searching the whole system message
    for those matches text that predates the bug.
    """
    cold = _capture_system(mock_config, db, dialogue_memory, text=text)
    warm = _warm_system(mock_config, db, dialogue_memory, text=text)
    assert WARM_SENTINEL in warm, "warm-profile patch did not take effect"
    warm_lines = set(warm.splitlines())
    return "\n".join(line for line in cold.splitlines() if line and line not in warm_lines)


_NO_BRANCH = (
    "The cold-start prompt adds nothing over the warm one, so there is no "
    "cold-start branch at all: with an empty memory the [Context: ...] line is "
    "the only concrete material in the prompt and the persona rule licenses "
    "inventing an observation from it. Every greeting on a fresh install "
    "becomes a time report."
)


class TestColdStartTakesTheClockOffTheTable:
    """The empty-memory branch must not leave the `[Context: ...]` line as the
    only material an open-ended reply can be built from."""

    def test_cold_start_forbids_the_context_line_as_reply_material(
        self, mock_config, db, dialogue_memory
    ):
        """With an empty database the prompt must rule out narrating the
        time/date/location back at the user."""
        segment = _cold_start_segment(mock_config, db, dialogue_memory)
        lowered = segment.lower()

        prohibits_it = any(term in lowered for term in (
            "do not make the time", "never make the time", "not the subject",
            "do not open with an observation about the time",
            "do not comment on the time", "not material for small talk",
            "do not narrate the time", "do not remark on the time",
        ))
        assert prohibits_it, (
            f"{_NO_BRANCH} The cold-start guidance must explicitly rule the "
            f"time/date/location out as reply material. "
            f"Cold-start-only guidance was: {segment!r}"
        )

    def test_cold_start_supplies_material_of_its_own(
        self, mock_config, db, dialogue_memory
    ):
        """Removing the clock is only half the fix.

        A bare "don't mention the time" leaves the model with nothing, and the
        persona prompt already bans bare greetings, so the cold-start branch
        must hand it something concrete to say instead.
        """
        segment = _cold_start_segment(mock_config, db, dialogue_memory)
        lowered = segment.lower()

        assert any(term in lowered for term in (
            "ask the user", "ask one question", "ask them",
            "what you can do", "offer one concrete", "something you can do",
            "name one concrete",
        )), (
            f"{_NO_BRANCH} The cold-start guidance must supply alternative reply "
            f"material (asking the user about themselves, or naming something "
            f"concrete the assistant can do). Banning the clock without offering a "
            f"replacement leaves only the bare greeting the persona prompt already "
            f"forbids. Cold-start-only guidance was: {segment!r}"
        )

    def test_cold_start_prohibition_is_scoped_to_unasked_questions(
        self, mock_config, db, dialogue_memory
    ):
        """The clock rule must not break "what time is it".

        The persona prompt promises the model can always answer from the
        context line. An unscoped ban contradicts it, and small models resolve
        that contradiction unpredictably.
        """
        segment = _cold_start_segment(mock_config, db, dialogue_memory)
        lowered = segment.lower()

        assert any(term in lowered for term in (
            "unless the user asked", "unless the user asks",
            "unless they asked", "unless they ask",
            "when the user asks", "if the user asks", "if the user asked",
        )), (
            f"{_NO_BRANCH} The cold-start clock prohibition must be scoped to the "
            f"case where the user did not ask about the time/date/location, or it "
            f"contradicts the persona rule that the context line answers time and "
            f"date questions. Cold-start-only guidance was: {segment!r}"
        )

    def test_context_line_still_reaches_the_prompt_on_cold_start(
        self, mock_config, db, dialogue_memory
    ):
        """The fix removes the clock as *material*, not as *data*.

        Scheduling suggestions and "what time is it" both still need the line
        present, so the cold-start branch must not suppress the injection.
        """
        system = _capture_system(mock_config, db, dialogue_memory)
        assert "[Context: Tuesday, 26 August 2026 at 15:40 UTC" in system, (
            "The cold-start branch must not suppress the [Context: ...] injection "
            "— the model still needs it to answer time, date and location questions."
        )


class TestColdStartBranchYieldsToStoredMemory:
    """When memory exists, the stored-fact rule owns the turn."""

    def test_guidance_present_when_cold_and_absent_when_warm(
        self, mock_config, db, dialogue_memory
    ):
        """A differential, so neither half can pass vacuously.

        The cold-start guidance is for the empty install only. Carrying it on a
        warm turn both wastes prompt budget on a small model and competes with
        the "lead with a concrete fact" rule.
        """
        from jarvis.system_prompt import COLD_START_GUIDANCE

        cold = _capture_system(mock_config, db, dialogue_memory)
        warm = _warm_system(mock_config, db, dialogue_memory)

        assert COLD_START_GUIDANCE in cold, (
            "Empty install must get the cold-start guidance — it is the only thing "
            "standing between a fresh user and a time report."
        )
        assert WARM_SENTINEL in warm, "warm-profile patch did not take effect"
        assert COLD_START_GUIDANCE not in warm, (
            "Cold-start guidance must not appear once stored memory is present — "
            "the 'build the reply around a stored fact' rule owns that turn."
        )

    def test_guidance_absent_when_diary_memory_present(
        self, mock_config, db, dialogue_memory
    ):
        """Diary enrichment counts as memory too, not just the warm profile."""
        from jarvis.system_prompt import COLD_START_GUIDANCE

        mock_config.memory_enrichment_source = "diary"
        mock_config.memory_digest_enabled = False
        system = _capture_system(
            mock_config, db, dialogue_memory, text=MEMORY_QUERY,
            keywords=["fencing"], plan=MEMORY_PLAN,
            **{"jarvis.memory.conversation.search_conversation_memory_by_keywords": {
                "return_value": [DIARY_ENTRY]
            }},
        )

        assert DIARY_ENTRY in system, "diary patch did not take effect"
        assert COLD_START_GUIDANCE not in system, (
            "Cold-start guidance must not appear when diary entries are present — "
            "the assistant has stored material to build the reply from."
        )

    def test_guidance_absent_when_only_the_digest_survives(
        self, mock_config, db, dialogue_memory
    ):
        """The small-model path is the one a naive check gets wrong.

        For a SMALL model the digest step replaces the raw diary and graph
        blocks with a distilled note, clearing both locals. A cold-start check
        that only looked at those two would re-enter cold start on a turn that
        has memory, and start telling the model to ignore the very material it
        was just given.
        """
        from jarvis.reply import engine as engine_mod
        from jarvis.system_prompt import COLD_START_GUIDANCE

        mock_config.memory_enrichment_source = "diary"
        mock_config.memory_digest_enabled = True
        digest_note = f"The user fences at {DIARY_SENTINEL}."

        with patch.object(engine_mod, "digest_memory_for_query", return_value=digest_note):
            system = _capture_system(
                mock_config, db, dialogue_memory, text=MEMORY_QUERY,
                keywords=["fencing"], plan=MEMORY_PLAN,
                **{"jarvis.memory.conversation.search_conversation_memory_by_keywords": {
                    "return_value": [DIARY_ENTRY]
                }},
            )

        assert digest_note in system, "digest patch did not take effect"
        assert DIARY_ENTRY not in system, (
            "the digest should have replaced the raw diary block"
        )
        assert COLD_START_GUIDANCE not in system, (
            "Cold-start guidance must not appear when the digest carries the "
            "memory — the raw diary and graph locals are empty by then, but the "
            "assistant still has stored material to build the reply from."
        )
