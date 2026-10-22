"""Shared, fail-fast Symptom_Description validator.

A single pure function used by both the diagnostics service and the
Conversation_Manager so validation semantics are identical everywhere and run
*before* any AI or storage call (fail-fast). Centralizing this logic guarantees
that Req 1.2/1.3, Req 2.4, and Req 3.2 reject identically.

Contract (see design.md):

    validate_symptom(text) -> Valid | Invalid(EMPTY) | Invalid(TOO_LONG)
      EMPTY     : text is null, "", or contains only whitespace
      TOO_LONG  : length exceeds 2000 characters
      Valid     : 1..2000 characters with at least one non-whitespace char

Outcomes are the typed values from :mod:`shared.outcomes`:
``Ok[str]`` carrying the original (unmodified) text on success, or ``Err`` with
the ``EMPTY``/``TOO_LONG`` :data:`~shared.outcomes.ErrorCode` on rejection. The
function is pure: it performs no I/O, mutates nothing, and never raises for
invalid input.
"""

from __future__ import annotations

from typing import Optional

from shared.outcomes import Err, Ok, Outcome

__all__ = ["MAX_SYMPTOM_LENGTH", "validate_symptom"]

# The maximum accepted Symptom_Description length (Req 1.3, 2.4, 3.2).
MAX_SYMPTOM_LENGTH = 2000


def validate_symptom(text: Optional[str]) -> Outcome[str]:
    """Validate a Symptom_Description, returning a typed outcome.

    Emptiness is checked before length so that a null, empty, or whitespace-only
    input is always ``EMPTY`` -- even when it is also longer than the limit --
    matching the design's ordering ("text is null, '', or contains only
    whitespace" listed first).

    Args:
        text: The raw Symptom_Description, or ``None``.

    Returns:
        ``Ok(text)`` carrying the original text when it has 1..2000 characters
        with at least one non-whitespace character; ``Err("EMPTY")`` when the
        text is ``None``, empty, or whitespace-only; ``Err("TOO_LONG")`` when a
        non-blank text exceeds 2000 characters.
    """

    # EMPTY: null, "", or whitespace-only (Req 1.2, 2.4, 3.2).
    if text is None or text.strip() == "":
        return Err("EMPTY", "Please provide a non-empty symptom description.")

    # TOO_LONG: non-blank text whose length exceeds the 2000-char maximum
    # (Req 1.3, 2.4, 3.2).
    if len(text) > MAX_SYMPTOM_LENGTH:
        return Err(
            "TOO_LONG",
            f"The symptom description must be at most {MAX_SYMPTOM_LENGTH} characters.",
        )

    # Valid: 1..2000 chars with at least one non-whitespace char.
    return Ok(text)
