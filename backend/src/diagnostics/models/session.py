"""The Session conversational context model."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import SessionStatus

__all__ = ["Session"]


@dataclass(frozen=True, slots=True)
class Session:
    """A single conversational exchange context identified by ``sessionId``.

    Timestamps are ISO 8601 UTC strings. ``interactionCount`` is capped at 50
    (Req 3.4); ``lastSubmissionAt`` is the authoritative value for the 30-minute
    idle-close evaluation (Req 3.5); ``ttl`` is an epoch-seconds value used by
    DynamoDB TTL for eventual cleanup of idle sessions.
    """

    sessionId: str
    customerId: str
    status: SessionStatus
    createdAt: str
    lastSubmissionAt: str
    interactionCount: int = 0
    ttl: int | None = None
