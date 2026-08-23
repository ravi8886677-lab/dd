"""Tests for the extended structural-redaction rules added so tool-output
carryover and recall-gate debug logs cannot leak credentials.
"""

import pytest

from src.jarvis.utils.redact import redact, scrub_secrets


@pytest.mark.unit
class TestVendorAccessKeys:
    def test_aws_akia_key_redacted(self):
        out = redact("key=AKIAIOSFODNN7EXAMPLE rest")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED_AWS_KEY]" in out

    def test_aws_asia_key_redacted(self):
        out = redact("ASIAIOSFODNN7EXAMPLE")
        assert "ASIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED_AWS_KEY]" in out

    def test_stripe_live_secret_redacted(self):
        token = "sk_live_" + "a" * 24
        out = redact(f"see {token} please")
        assert token not in out
        assert "[REDACTED_STRIPE_KEY]" in out

    def test_stripe_test_publishable_redacted(self):
        token = "pk_test_" + "Z" * 24
        out = redact(token)
        assert token not in out
        assert "[REDACTED_STRIPE_KEY]" in out

    def test_github_pat_redacted(self):
        token = "ghp_" + "A" * 36
        out = redact(token)
        assert token not in out
        assert "[REDACTED_GH_TOKEN]" in out

    def test_openai_key_redacted(self):
        token = "sk-" + "A" * 40
        out = redact(token)
        assert token not in out
        assert "[REDACTED_OPENAI_KEY]" in out

    def test_google_api_key_redacted(self):
        token = "AIza" + "B" * 35
        out = redact(token)
        assert token not in out
        assert "[REDACTED_GOOG_KEY]" in out


@pytest.mark.unit
class TestAuthorizationHeaders:
    def test_bearer_header_redacted(self):
        out = scrub_secrets("Authorization: Bearer abc.def.ghi")
        assert "abc.def.ghi" not in out
        assert "Authorization: Bearer [REDACTED]" in out

    def test_basic_header_redacted(self):
        out = scrub_secrets("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in out
        assert "Authorization: Basic [REDACTED]" in out


@pytest.mark.unit
class TestKeywordAnchoredCredentials:
    def test_refresh_token_keyword_redacted(self):
        out = redact("refresh_token=abcdef123456")
        assert "abcdef123456" not in out
        assert "refresh_token=[REDACTED]" in out

    def test_access_token_keyword_redacted(self):
        out = redact("access_token: zzz999")
        assert "zzz999" not in out
        assert "access_token=[REDACTED]" in out

    def test_session_id_redacted(self):
        out = redact("session_id=deadbeefcafe")
        assert "deadbeefcafe" not in out
        assert "session_id=[REDACTED]" in out

    def test_oauth_token_redacted(self):
        out = redact("oauth_token=qwertyuiop")
        assert "qwertyuiop" not in out
        assert "oauth_token=[REDACTED]" in out


@pytest.mark.unit
class TestScrubbingIsLinearInTheInput:
    """A scrubber on the reply path cannot be quadratic.

    The email rule used to start a fresh greedy scan at every offset in
    a run of local-part characters and backtrack the whole way on
    failure. That is 19 seconds on 50KB of ordinary text — and 50KB is
    what `fetchWebPage` hands back, through `scrub_secrets`, on the
    reply path. It is asserted here rather than left to review because
    the shape reads harmless and the cost only shows up on real input.
    """

    def test_redacting_a_large_input_stays_fast(self):
        import time

        started = time.perf_counter()
        redact("x" * 200_000)
        took = time.perf_counter() - started

        assert took < 2.0, f"redact took {took:.1f}s on 200KB"

    def test_scrubbing_a_page_extract_stays_fast(self):
        """`scrub_secrets` has no length cap, so the rules must be cheap."""
        import time

        started = time.perf_counter()
        scrub_secrets("a.b-c+d%e" * 20_000)
        took = time.perf_counter() - started

        assert took < 2.0, f"scrub_secrets took {took:.1f}s on 180KB"

    def test_the_cost_grows_linearly_not_quadratically(self):
        """Doubling the input must not quadruple the time."""
        import time

        def cost(size: int) -> float:
            text = "x" * size
            started = time.perf_counter()
            for _ in range(3):
                redact(text)
            return time.perf_counter() - started

        small = max(cost(50_000), 1e-4)
        large = cost(200_000)

        # Linear would be 4x for 4x the input. Quadratic would be 16x.
        assert large / small < 8, f"scaled {large / small:.1f}x for 4x the input"

    def test_an_address_in_a_long_page_is_still_redacted(self):
        """Speed must not have come from matching less."""
        haystack = "x" * 50_000 + " contact alice@example.com now"

        scrubbed = redact(haystack, max_len=200_000)

        assert "alice@example.com" not in scrubbed
        assert "[REDACTED_EMAIL]" in scrubbed
