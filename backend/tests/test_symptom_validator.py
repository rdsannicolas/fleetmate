"""Example-based unit tests for the shared Symptom_Description validator (Task 2.1).

Covers the design contract and boundary examples (exactly 1 char, exactly 2000
chars, exactly 2001 chars) plus emptiness/whitespace/None handling. Asserts the
function returns typed ``Ok``/``Err`` outcomes and is pure (no mutation of the
input).

_Requirements: 1.2, 1.3, 2.4, 3.2_
"""

from __future__ import annotations

import pytest

from shared.outcomes import Err, Ok, is_err, is_ok
from shared.validation import MAX_SYMPTOM_LENGTH, validate_symptom


def test_single_character_is_valid() -> None:
    """Exactly 1 non-whitespace char is accepted and returned unchanged."""
    outcome = validate_symptom("x")
    assert is_ok(outcome)
    assert isinstance(outcome, Ok)
    assert outcome.value == "x"


def test_exactly_2000_characters_is_valid() -> None:
    """Exactly 2000 characters is the inclusive upper bound (accepted)."""
    text = "a" * MAX_SYMPTOM_LENGTH
    outcome = validate_symptom(text)
    assert is_ok(outcome)
    assert isinstance(outcome, Ok)
    assert outcome.value == text


def test_exactly_2001_characters_is_too_long() -> None:
    """2001 characters exceeds the maximum and is rejected with TOO_LONG."""
    outcome = validate_symptom("a" * (MAX_SYMPTOM_LENGTH + 1))
    assert is_err(outcome)
    assert isinstance(outcome, Err)
    assert outcome.code == "TOO_LONG"
    assert "2000" in outcome.message


def test_none_is_empty() -> None:
    """A null symptom is rejected with EMPTY."""
    outcome = validate_symptom(None)
    assert isinstance(outcome, Err)
    assert outcome.code == "EMPTY"


def test_empty_string_is_empty() -> None:
    """An empty string is rejected with EMPTY."""
    outcome = validate_symptom("")
    assert isinstance(outcome, Err)
    assert outcome.code == "EMPTY"


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n", " \t\n\r "])
def test_whitespace_only_is_empty(blank: str) -> None:
    """Whitespace-only text (various kinds) is rejected with EMPTY."""
    outcome = validate_symptom(blank)
    assert isinstance(outcome, Err)
    assert outcome.code == "EMPTY"


def test_whitespace_only_over_limit_is_empty_not_too_long() -> None:
    """Emptiness is checked before length: blank >2000 chars is EMPTY."""
    outcome = validate_symptom(" " * (MAX_SYMPTOM_LENGTH + 50))
    assert isinstance(outcome, Err)
    assert outcome.code == "EMPTY"


def test_valid_text_with_surrounding_whitespace_is_returned_unchanged() -> None:
    """Original text is preserved verbatim; the validator does not trim."""
    text = "  engine wont start  "
    outcome = validate_symptom(text)
    assert isinstance(outcome, Ok)
    assert outcome.value == text


def test_validator_does_not_mutate_or_raise_on_invalid_input() -> None:
    """The function is pure: invalid input yields an Err, never an exception."""
    # None, empty, whitespace, and over-length all return without raising.
    assert isinstance(validate_symptom(None), Err)
    assert isinstance(validate_symptom(""), Err)
    assert isinstance(validate_symptom("   "), Err)
    assert isinstance(validate_symptom("a" * 2001), Err)
