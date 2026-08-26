"""
Cold-Start Greeting Evaluations (Live)

The cold-start failure mode: on a fresh install the memory graph is empty, so
no stored-facts section reaches the system message. The persona prompt licenses
inventing an observation whenever that section is absent, and the only concrete
data left in the prompt is the `[Context: ...]` line. So "hi" gets a time
report, on every turn, because the material never changes.

It is also the first impression for every new user, since everyone starts with
an empty memory.

These evals pin the behaviour with a real model and an empty database: a
greeting must be answered with something other than the clock.

Run: ./scripts/run_evals.sh test_cold_start
"""

import pytest
from unittest.mock import patch

from conftest import requires_judge_llm
from helpers import (
    JUDGE_MODEL,
    ToolCallCapture,
    assert_not_fallback_reply,
    call_judge_llm,
    create_mock_tool_run,
)


# A pinned context line, so the assertions can look for the exact tokens the
# model would have to have copied out of it. Anchoring on the injected values
# rather than on a list of English time words keeps the check language-neutral
# and immune to however the model phrases the leak.
PINNED_CONTEXT = "Tuesday, 26 August 2026 at 15:40 UTC, Location: Testville"

# Distinctive tokens from PINNED_CONTEXT. A reply containing any of these has
# read the clock back to the user.
CONTEXT_TOKENS = ("tuesday", "august", "2026", "15:40", "3:40", "testville")


def _leaked_context_tokens(response: str) -> list[str]:
    lowered = (response or "").lower()
    return [tok for tok in CONTEXT_TOKENS if tok in lowered]


_TIME_ANCHOR_JUDGE_SYSTEM = """You are an evaluation judge for a voice assistant.

You will be shown a user's message and the assistant's reply. Decide whether the
reply is ANCHORED ON THE CLOCK OR CALENDAR: that is, whether the time of day, the
day of the week, the date, the season, or the user's location is the subject of
the reply or the thing the assistant chose to remark on.

Examples of ANCHORED (the failure being tested):
- "Good afternoon. A fine Tuesday to be indoors."
- "It's 3:40 in the afternoon here in Testville, a quiet hour."
- "Mid-afternoon already. Where does the time go?"

Examples of NOT ANCHORED (acceptable):
- "Hello. What are you working on at the moment?"
- "Evening. I can look things up, check the weather, or remember what you tell me about yourself."
- "Hello. Tell me something you would like me to remember about you."

The assistant did have the current time and location available. Using them is
only ANCHORED when the reply is ABOUT them, or opens by remarking on them. A
reply that merely says hello and asks the user a question is NOT anchored.

Output in this EXACT format:
VERDICT: [ANCHORED/NOT_ANCHORED]
REASONING: [one sentence]"""


def _judge_says_anchored_on_time(query: str, response: str):
    """Return (is_anchored, reasoning) or (None, reason) when the judge is unusable."""
    verdict = call_judge_llm(
        _TIME_ANCHOR_JUDGE_SYSTEM,
        f"User message: {query}\n\nAssistant reply: {response}",
    )
    if not verdict:
        return None, "judge LLM returned nothing"
    upper = verdict.upper()
    if "NOT_ANCHORED" in upper:
        return False, verdict.strip()
    if "ANCHORED" in upper:
        return True, verdict.strip()
    return None, f"unparseable judge verdict: {verdict.strip()}"


_DIARY_AGENT_JUDGE_SYSTEM = """You are an evaluation judge for a voice assistant.

You will be shown a user's message and the assistant's reply. Decide whether the
reply READS AS A MEMORY SYSTEM rather than as an assistant: that is, whether it
talks about what the assistant does or does not remember, whether it recognises
the user, that this is their first conversation, or asks the user to supply facts
for it to store.

Examples of MEMORY_SYSTEM (the failure being tested):
- "I don't have anything stored about you yet. What would you like me to remember?"
- "We haven't spoken before. Tell me a bit about yourself so I can remember it."
- "My memory of you is empty at the moment."
- "I don't recognise you yet, but I'll remember what you tell me."

Examples of ASSISTANT (acceptable):
- "Hello. What are you working on?"
- "Hi. I can look things up, check the weather, or take notes, whatever is useful."
- "Morning. What do you need?"

Asking what the user is working on or needs is an ASSISTANT reply, not a memory
system, even though it is a question. The distinction is whether the reply is
about the assistant's memory or about helping.

Output in this EXACT format:
VERDICT: [MEMORY_SYSTEM/ASSISTANT]
REASONING: [one sentence]"""

# Cheap pre-check before the judge: wording that is almost always the failure.
MEMORY_TALK_TOKENS = (
    "remember about you", "like me to remember", "stored about you",
    "don't have anything stored", "do not have anything stored",
    "my memory", "haven't spoken before", "have not spoken before",
    "don't recognise you", "do not recognise you", "don't recognize you",
    "first conversation",
)


def _memory_talk(response: str) -> list[str]:
    lowered = (response or "").lower()
    return [t for t in MEMORY_TALK_TOKENS if t in lowered]


def _judge_says_memory_system(query: str, response: str):
    """Return (is_memory_system, reasoning) or (None, reason) when unusable."""
    verdict = call_judge_llm(
        _DIARY_AGENT_JUDGE_SYSTEM,
        f"User message: {query}\n\nAssistant reply: {response}",
    )
    if not verdict:
        return None, "judge LLM returned nothing"
    upper = verdict.upper()
    if "MEMORY_SYSTEM" in upper:
        return True, verdict.strip()
    if "ASSISTANT" in upper:
        return False, verdict.strip()
    return None, f"unparseable judge verdict: {verdict.strip()}"


class TestColdStartGreeting:
    """Empty database, greeting-shaped input: the reply must not be about the clock."""

    @pytest.mark.eval
    @requires_judge_llm
    @pytest.mark.parametrize("query", [
        pytest.param("hi", id="Cold start: hi"),
        pytest.param("hi bro", id="Cold start: hi bro"),
        pytest.param("say something", id="Cold start: say something"),
    ])
    def test_cold_start_reply_does_not_narrate_the_clock(
        self, query, mock_config, eval_db, eval_dialogue_memory
    ):
        """Fresh install + greeting must not produce a time report.

        ``eval_db`` is empty, so this is exactly the state every new user is in.
        """
        from jarvis.reply import engine as engine_mod

        mock_config.ollama_base_url = "http://localhost:11434"
        mock_config.ollama_chat_model = JUDGE_MODEL
        mock_config.llm_chat_model = JUDGE_MODEL

        capture = ToolCallCapture()

        with patch.object(
            engine_mod, "_live_time_location_string", return_value=PINNED_CONTEXT
        ), patch.object(
            engine_mod, "run_tool_with_retries", side_effect=create_mock_tool_run(capture)
        ):
            response = engine_mod.run_reply_engine(
                db=eval_db, cfg=mock_config, tts=None,
                text=query, dialogue_memory=eval_dialogue_memory,
            )

        print(f"\n  Live Cold-Start Test ({JUDGE_MODEL}):")
        print(f"  Query: '{query}'")
        print(f"  Response: {response}")
        print(f"  Tools called: {capture.tool_names() or 'none'}")

        assert response and response.strip(), (
            f"Cold start on '{query}' produced no reply at all."
        )
        assert_not_fallback_reply(response, context=f"cold start '{query}'")

        leaked = _leaked_context_tokens(response)
        assert not leaked, (
            f"Cold-start reply to '{query}' read the [Context: ...] line back to the "
            f"user: it contains {leaked} straight out of '{PINNED_CONTEXT}'. On a "
            f"fresh install the clock is the only concrete material in the prompt, "
            f"so an unguarded open-ended rule makes every greeting a time report. "
            f"Response: {response}"
        )

        anchored, reasoning = _judge_says_anchored_on_time(query, response)
        if anchored is None:
            print(f"  Judge unusable ({reasoning}) — token check alone applied.")
            return
        assert not anchored, (
            f"Cold-start reply to '{query}' is anchored on the clock/calendar even "
            f"though it did not quote the context line verbatim. Judge: {reasoning}. "
            f"Response: {response}"
        )

    @pytest.mark.eval
    @requires_judge_llm
    @pytest.mark.parametrize("query", [
        pytest.param("hi", id="Diary agent: hi"),
        pytest.param("say something", id="Diary agent: say something"),
    ])
    def test_cold_start_reply_is_an_assistant_not_a_memory_system(
        self, query, mock_config, eval_db, eval_dialogue_memory
    ):
        """A greeting must not be answered by talking about the empty diary.

        The diary is for things to remember in the background. An assistant that
        opens by reporting it has nothing stored, or by asking the user to supply
        facts for it to keep, has made its memory the subject of the first thing
        a new user ever reads.
        """
        from jarvis.reply import engine as engine_mod

        mock_config.ollama_base_url = "http://localhost:11434"
        mock_config.ollama_chat_model = JUDGE_MODEL
        mock_config.llm_chat_model = JUDGE_MODEL

        capture = ToolCallCapture()

        with patch.object(
            engine_mod, "_live_time_location_string", return_value=PINNED_CONTEXT
        ), patch.object(
            engine_mod, "run_tool_with_retries", side_effect=create_mock_tool_run(capture)
        ):
            response = engine_mod.run_reply_engine(
                db=eval_db, cfg=mock_config, tts=None,
                text=query, dialogue_memory=eval_dialogue_memory,
            )

        print(f"\n  Live Cold-Start Diary-Agent Test ({JUDGE_MODEL}):")
        print(f"  Query: '{query}'")
        print(f"  Response: {response}")

        assert_not_fallback_reply(response, context=f"cold start '{query}'")

        talk = _memory_talk(response)
        assert not talk, (
            f"Cold-start reply to '{query}' made the assistant's own memory the "
            f"subject: it contains {talk}. The diary is for things to remember in "
            f"the background, not for the assistant to talk about, and this is the "
            f"first thing a new user reads. Response: {response}"
        )

        is_memory_system, reasoning = _judge_says_memory_system(query, response)
        if is_memory_system is None:
            print(f"  Judge unusable ({reasoning}) — token check alone applied.")
            return
        assert not is_memory_system, (
            f"Cold-start reply to '{query}' reads as a memory system introducing "
            f"itself rather than an assistant. Judge: {reasoning}. "
            f"Response: {response}"
        )

    @pytest.mark.eval
    @requires_judge_llm
    @pytest.mark.parametrize("query", [
        pytest.param("what time is it", id="Cold start: what time is it"),
        pytest.param("what is today's date", id="Cold start: what is the date"),
    ])
    def test_cold_start_still_answers_a_direct_time_question(
        self, query, mock_config, eval_db, eval_dialogue_memory
    ):
        """The counterpart guard: the clock is removed as material, not as data.

        The cold-start guidance tells the model not to build a reply out of the
        `[Context: ...]` line. Scoped wrongly, that reads as "never use the
        line" and breaks the persona's standing promise to answer time and date
        questions from it. This is the regression the fix could introduce, so it
        is tested in the same file as the bug it fixes.
        """
        from jarvis.reply import engine as engine_mod

        mock_config.ollama_base_url = "http://localhost:11434"
        mock_config.ollama_chat_model = JUDGE_MODEL
        mock_config.llm_chat_model = JUDGE_MODEL

        capture = ToolCallCapture()

        with patch.object(
            engine_mod, "_live_time_location_string", return_value=PINNED_CONTEXT
        ), patch.object(
            engine_mod, "run_tool_with_retries", side_effect=create_mock_tool_run(capture)
        ):
            response = engine_mod.run_reply_engine(
                db=eval_db, cfg=mock_config, tts=None,
                text=query, dialogue_memory=eval_dialogue_memory,
            )

        print(f"\n  Live Cold-Start Time-Question Test ({JUDGE_MODEL}):")
        print(f"  Query: '{query}'")
        print(f"  Response: {response}")

        assert_not_fallback_reply(response, context=f"cold start '{query}'")
        assert _leaked_context_tokens(response), (
            f"Cold start asked '{query}' directly and the reply carries nothing from "
            f"'{PINNED_CONTEXT}'. The cold-start guidance removes the clock as reply "
            f"material, not as data: a direct time or date question must still be "
            f"answered from the context line. Response: {response}"
        )

    @pytest.mark.eval
    @requires_judge_llm
    def test_cold_start_replies_vary_across_turns(
        self, mock_config, eval_db, eval_dialogue_memory
    ):
        """The persona prompt requires a varied response each time.

        With the clock as the only material that rule is unsatisfiable: the
        material never changes, so neither does the reply. Three fresh cold
        starts must not collapse onto one answer.
        """
        from jarvis.reply import engine as engine_mod
        from jarvis.memory.conversation import DialogueMemory

        mock_config.ollama_base_url = "http://localhost:11434"
        mock_config.ollama_chat_model = JUDGE_MODEL
        mock_config.llm_chat_model = JUDGE_MODEL

        responses = []
        for _ in range(3):
            # A fresh dialogue memory each time: this is three separate first
            # impressions, not one conversation.
            memory = DialogueMemory(inactivity_timeout=300, max_interactions=20)
            capture = ToolCallCapture()
            with patch.object(
                engine_mod, "_live_time_location_string", return_value=PINNED_CONTEXT
            ), patch.object(
                engine_mod, "run_tool_with_retries", side_effect=create_mock_tool_run(capture)
            ):
                responses.append(engine_mod.run_reply_engine(
                    db=eval_db, cfg=mock_config, tts=None,
                    text="hi", dialogue_memory=memory,
                ) or "")

        print(f"\n  Live Cold-Start Variety Test ({JUDGE_MODEL}):")
        for i, r in enumerate(responses, 1):
            print(f"  [{i}] {r}")

        normalised = {r.strip().lower() for r in responses if r.strip()}
        assert len(normalised) > 1, (
            "Three separate cold starts on 'hi' produced one identical reply. The "
            "persona prompt requires a varied response each time, which is "
            "unsatisfiable while the clock is the only material available. "
            f"Replies: {responses}"
        )
