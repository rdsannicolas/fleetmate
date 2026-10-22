"""Typed outcomes for the FleetMate backend.

Errors are modeled as explicit, typed values that cross component boundaries as
data -- never as exceptions. Every component returns either a success value or an
``Err`` carrying one of the well-known :data:`ErrorCode` literals, so callers can
exhaustively handle each outcome and the Diagnostic_Assistant can map outcomes to
HTTP responses without catching exceptions.

The error-code set is a closed string-literal union (Python ``Literal``) so the
set of possible errors is fixed and type-checkable, mirroring the design's
string-literal error codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar, Union

__all__ = [
    "ErrorCode",
    "ERROR_CODES",
    "Ok",
    "Err",
    "Outcome",
    "is_ok",
    "is_err",
]

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

# The closed set of error codes that may cross a component boundary. Keeping this
# a ``Literal`` union makes it a compile-time-checkable, closed set.
ErrorCode = Literal[
    "EMPTY",  # symptom is null, empty, or whitespace-only (Req 1.2, 2.4, 3.2)
    "TOO_LONG",  # symptom exceeds 2000 characters (Req 1.3, 2.4, 3.2)
    "SESSION_CLOSED",  # follow-up submitted to an idle-closed session (Req 3.5)
    "LIMIT_REACHED",  # session already holds 50 interactions (Req 3.4)
    "AI_TIMEOUT",  # AI generation exceeded the 30s budget (Req 2.6)
    "AI_ERROR",  # Guardrail block or model/invocation failure (Req 2.6)
    "PERSIST_FAILED",  # interaction record could not be persisted (Req 4.3)
    "ACCESS_DENIED",  # principal not authorized for the request (Req 4.5, 5.4)
    "INVALID_RANGE",  # date-range start is later than end (Req 4.6)
    "NOT_FOUND",  # requested session does not exist (Req 5.3)
]

# A runtime-inspectable tuple of the same codes (handy for validation and tests).
ERROR_CODES: tuple[ErrorCode, ...] = (
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
)


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    """A successful outcome carrying a value."""

    value: T


@dataclass(frozen=True, slots=True)
class Err:
    """A failed outcome carrying a typed :data:`ErrorCode` and optional message.

    ``message`` is a human-friendly suggestion safe to surface to the caller; it
    never contains internal/provider details or stack traces.
    """

    code: ErrorCode
    message: str = ""


# An ``Outcome`` is either success (``Ok[T]``) or a typed failure (``Err``).
Outcome = Union[Ok[T], Err]


def is_ok(outcome: Outcome[T]) -> bool:
    """Return ``True`` when ``outcome`` is a successful :class:`Ok`."""

    return isinstance(outcome, Ok)


def is_err(outcome: Outcome[T]) -> bool:
    """Return ``True`` when ``outcome`` is a typed :class:`Err`."""

    return isinstance(outcome, Err)
