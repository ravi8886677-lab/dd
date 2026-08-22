"""Jarvis may not say it did something the record does not support.

This is one of the two non-negotiables the project was failing. A tool
returning without raising is not evidence that the world changed, and
until a tool actually checks, the honest words are "I tried" rather than
"done". The prompt has to say so, because the model's default is to
narrate the happy path.

The live version of this is `evals/test_completion_claims.py`, which
puts the question to a real model. This is the mocked equivalent the
repo's convention asks for: it pins that the instruction is present and
says what it must forbid.
"""

from __future__ import annotations

import pytest

from jarvis.system_prompt import build_system_prompt

pytestmark = pytest.mark.unit


class TestThePromptForbidsFabricatedCompletion:
    def test_it_tells_the_model_not_to_claim_an_unverified_success(self):
        prompt = build_system_prompt().lower()

        assert "did not confirm" in prompt or "could not confirm" in prompt

    def test_it_names_the_honest_wording_rather_than_only_banning_the_dishonest(self):
        """A ban with no alternative gets ignored under pressure."""
        prompt = build_system_prompt().lower()

        assert "tried" in prompt or "attempted" in prompt

    def test_it_says_a_refusal_is_not_a_completion(self):
        """`NOT DONE` is what the boundary returns when it refuses."""
        prompt = build_system_prompt()

        assert "NOT DONE" in prompt

    def test_the_rule_survives_a_renamed_assistant(self):
        prompt = build_system_prompt("Friday")

        assert "NOT DONE" in prompt
