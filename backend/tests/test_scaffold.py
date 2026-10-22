"""Scaffold smoke tests for Task 1.

Verifies the package layout imports cleanly, the core data models and typed
outcomes are usable, and the pytest + Hypothesis framework is wired up.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from hypothesis import given
from hypothesis import strategies as st

from diagnostics.models import (
    DiagnosticResult,
    InteractionRecord,
    Page,
    Session,
    SessionStatus,
)
from shared.outcomes import ERROR_CODES, Err, Ok, is_err, is_ok


def test_packages_import_cleanly() -> None:
    """All scaffolded packages import without error."""
    import diagnostics  # noqa: F401
    import diagnostics.events  # noqa: F401
    import diagnostics.prompts  # noqa: F401
    import diagnostics.repositories  # noqa: F401
    import diagnostics.services  # noqa: F401
    import handlers  # noqa: F401
    import shared.auth  # noqa: F401
    import shared.logging  # noqa: F401
    import shared.utils  # noqa: F401
    import shared.validation  # noqa: F401


def test_session_status_enum_values() -> None:
    """SessionStatus exposes exactly ACTIVE and CLOSED as string values."""
    assert {s.value for s in SessionStatus} == {"ACTIVE", "CLOSED"}
    assert SessionStatus.ACTIVE == "ACTIVE"


def test_error_code_set_is_complete() -> None:
    """The typed error-code set matches the design's string-literal union."""
    assert set(ERROR_CODES) == {
        "EMPTY",
        "TOO_LONG",
        "SESSION_CLOSED",
        "LIMIT_REACHED",
        "AI_TIMEOUT",
        "AI_ERROR",
        "PERSIST_FAILED",
        "ACCESS_DENIED",
        "INVALID_RANGE",
        "NOT_FOUND",
    }


def test_ok_and_err_are_distinguishable() -> None:
    """Outcomes are typed values, not exceptions, and are distinguishable."""
    ok: Ok[int] = Ok(42)
    err = Err("NOT_FOUND", "session not found")

    assert is_ok(ok) and not is_err(ok)
    assert is_err(err) and not is_ok(err)
    assert ok.value == 42
    assert err.code == "NOT_FOUND"
    assert err.message == "session not found"


def test_diagnostic_result_success_shape() -> None:
    """The success shape carries an issue and recommendation, no more-detail flag."""
    result = DiagnosticResult(
        probableIssue="Weak Battery",
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )
    assert result.needsMoreDetail is False
    assert result.probableIssue == "Weak Battery"


def test_diagnostic_result_insufficient_info_shape() -> None:
    """The insufficient-info shape omits the issue and signals more detail."""
    result = DiagnosticResult(needsMoreDetail=True, message="Please add detail.")
    assert result.probableIssue is None
    assert result.needsMoreDetail is True


def test_models_are_frozen() -> None:
    """Models are immutable value objects."""
    session = Session(
        sessionId="s1",
        customerId="c1",
        status=SessionStatus.ACTIVE,
        createdAt="2024-01-01T00:00:00.000Z",
        lastSubmissionAt="2024-01-01T00:00:00.000Z",
    )
    with pytest.raises(FrozenInstanceError):
        session.interactionCount = 5  # type: ignore[misc]


def test_page_defaults_to_empty() -> None:
    """A Page defaults to an empty record set with no continuation token."""
    page = Page()
    assert page.records == ()
    assert page.nextPageToken is None


def test_interaction_record_holds_result() -> None:
    """An InteractionRecord composes a DiagnosticResult and ordering timestamp."""
    record = InteractionRecord(
        recordId="r1",
        sessionId="s1",
        symptomDescription="engine wont start",
        result=DiagnosticResult(needsMoreDetail=True, message="more detail"),
        createdAt="2024-01-01T00:00:00.123Z",
    )
    assert record.result.needsMoreDetail is True
    assert record.createdAt.endswith("Z")


@given(st.sampled_from(ERROR_CODES), st.text(max_size=50))
def test_err_round_trips_any_code_and_message(code: str, message: str) -> None:
    """Hypothesis framework is wired: Err preserves any code/message pair."""
    err = Err(code, message)  # type: ignore[arg-type]
    assert err.code == code
    assert err.message == message
    assert is_err(err)
