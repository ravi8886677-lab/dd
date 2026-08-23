from __future__ import annotations
import re

# Deterministic structural scrub patterns. Order matters: specific
# vendor-shaped tokens are matched before generic catches so the more
# informative label wins (e.g. "[REDACTED_AWS_KEY]" beats "[REDACTED_HEX]").
#: How much beyond ``max_len`` the rules still see, so a credential
#: sitting across the cut is matched whole rather than left a fragment.
_SCRUB_MARGIN = 1024

_REDACTION_RULES: list[tuple[re.Pattern[str], str]] = [
    # The lookbehind is load-bearing, not decoration. Without it the
    # leading `+` starts a fresh greedy scan at every offset in a run of
    # local-part characters and backtracks the whole way on failure, so
    # the rule costs O(n^2): 19 seconds on 50KB of ordinary text, which
    # is a page extract. Refusing to start mid-run makes it linear and
    # matches exactly the same addresses — the engine was only ever
    # re-finding the same match from a later start.
    (re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.IGNORECASE), "[REDACTED_EMAIL]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED_CARD]"),
    # Vendor-specific access keys (bare, no surrounding keyword required).
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[REDACTED_STRIPE_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED_GOOG_KEY]"),
    # Authorisation headers — Bearer/Basic carry credentials in line.
    (re.compile(r"Authorization:\s*Bearer\s+\S+", re.IGNORECASE), "Authorization: Bearer [REDACTED]"),
    (re.compile(r"Authorization:\s*Basic\s+[A-Za-z0-9+/=]+", re.IGNORECASE), "Authorization: Basic [REDACTED]"),
    # Generic prefix catch — left after the vendor-specific rules so
    # newer formats like gh[pousr]_ get a precise label first.
    (re.compile(r"\b(AWS|GH|GCP|AZURE|xox[abpcr]-)[A-Za-z0-9_\-]{10,}\b", re.IGNORECASE), "[REDACTED_TOKEN]"),
    (re.compile(r"\b(?:eyJ[0-9A-Za-z._\-]+)\b"), "[REDACTED_JWT]"),
    # Keyword-anchored credentials. Covers refresh/access/oauth/session
    # variants in addition to the original pass/secret/token/apikey set.
    (re.compile(
        r"\b(pass(?:word)?|secret|token|apikey|api_key|"
        r"(?:refresh|access|id|oauth)_?token|session(?:_?id)?|sid)"
        r"\s*[:=]\s*\S+\b",
        re.IGNORECASE,
    ), r"\1=[REDACTED]"),
    (re.compile(r"\b[0-9A-Fa-f]{32,}\b"), "[REDACTED_HEX]"),
]



# One-time codes are asked as a whole-text question, not a per-match one.
#
# As a lookahead this only looked *forward*, so it fired for "482913 is
# your code" and not for "your code is 482913" — the way people actually
# write it. Nothing else here catches six digits: hex needs 32, card
# needs 13. It also rescanned to the end of the string at every match,
# which is quadratic, and worst on the text least likely to contain a
# code at all: a page of figures with no keyword in it.
#
# Asking once, up front, is both correct and cheap. It redacts strictly
# more than the lookahead did, never less, which is the safe direction
# for a scrubber.
_OTP_KEYWORD = re.compile(r"otp|2fa|code", re.IGNORECASE)
_SIX_DIGITS = re.compile(r"\b\d{6}\b")


def _apply_rules(text: str) -> str:
    """Run every structural rule over ``text``.

    The pair list runs first so a more informative label wins: a card
    number or a hex secret is already replaced by the time six bare
    digits are looked for, and cannot be mistaken for a code.
    """
    scrubbed = text
    for pattern, repl in _REDACTION_RULES:
        scrubbed = pattern.sub(repl, scrubbed)
    if _OTP_KEYWORD.search(scrubbed):
        scrubbed = _SIX_DIGITS.sub("[REDACTED_OTP]", scrubbed)
    return scrubbed


def redact(text: str, max_len: int = 8000) -> str:
    """Scrub structurally, collapse whitespace, and cap the result.

    The cap bounds the work as well as the output. Scrubbing the whole
    string and truncating afterwards made ``max_len`` read like
    protection while providing none: every caller passing a long string
    paid for rules over text that was about to be thrown away. The
    margin keeps a credential that straddles the cut whole when the
    rules see it.
    """
    scrubbed = _apply_rules(text[:max_len + _SCRUB_MARGIN])
    scrubbed = " ".join(scrubbed.split())
    if len(scrubbed) > max_len:
        scrubbed = scrubbed[:max_len]
    return scrubbed


def scrub_secrets(text: str) -> str:
    """Apply the structural scrub rules without whitespace collapse or length cap.

    Use for structured content (tool output, multi-line payloads) where
    preserving newlines matters but tokens/emails/etc. must still be masked.
    """
    return _apply_rules(text)
