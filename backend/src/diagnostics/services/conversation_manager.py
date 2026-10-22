"""The Conversation_Manager: Session lifecycle and context assembly.

The Conversation_Manager owns the conversational :class:`Session` lifecycle
(design "Conversation_Manager"):

* Create a new Session with a unique UUID v4 identifier when none is active, so
  the Session exists *before* a submission is processed (Req 1.4).
* Supply all of the active Session's Interaction_Records as ascending-ordered
  context to the AI_Engine (Req 3.1) and maintain that ascending order as new
  records are appended (Req 3.3).
* Enforce the 50-interaction limit per Session (Req 3.4).
* Enforce the 30-minute idle timeout, treating ``lastSubmissionAt`` as the
  authoritative gate while DynamoDB TTL handles eventual cleanup, and reject
  submissions to closed Sessions (Req 3.5).
* Delegate follow-up Symptom_Description validation to the shared validator and
  preserve the Session and its context unchanged on rejection (Req 3.2).

Design constraints honoured here:

* **Repository-only dependency.** All state lives in DynamoDB behind the
  :class:`~diagnostics.repositories.FleetMateDiagnostics` repository; this module
  never imports Boto3 (design "Layering and Dependency Direction").
* **Injectable clock.** The 30-minute idle check and the ``createdAt`` /
  ``lastSubmissionAt`` / ``ttl`` stamps all read "now" from an injected clock so
  tests can drive a fake clock across the 30-minute boundary (Property 8).
* **Typed outcomes.** Boundary failures are returned as
  :class:`~shared.outcomes.Err` values (``SESSION_CLOSED``, ``LIMIT_REACHED``,
  ``EMPTY``, ``TOO_LONG``, ``NOT_FOUND``), never raised across the boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from diagnostics.models import (
    DiagnosticResult,
    InteractionRecord,
    Session,
    SessionStatus,
)
from diagnostics.repositories import FleetMateDiagnostics, format_timestamp
from shared.outcomes import Err, Ok, Outcome
from shared.validation import validate_symptom

__all__ = [
    "ConversationManager",
    "MAX_INTERACTIONS",
    "IDLE_TIMEOUT",
    "new_session_id",
]

# Req 3.4: a Session never holds more than 50 Interaction_Records.
MAX_INTERACTIONS = 50

# Req 3.5: a Session is closed once idle for 30 continuous minutes. The boundary
# is inclusive -- idle time of *exactly* 30 minutes closes the Session
# (Property 8: "closed if and only if elapsed time is at least 30 minutes").
IDLE_TIMEOUT = timedelta(minutes=30)

# Grace period added on top of the idle window for the DynamoDB TTL attribute.
# TTL is eventual-cleanup only; the authoritative gate is the ``lastSubmissionAt``
# comparison in :meth:`ConversationManager.is_active`, so a little slack here is
# harmless and avoids deleting a session the moment it idles out.
_TTL_GRACE = timedelta(minutes=5)


def new_session_id() -> str:
    """Return a fresh globally unique UUID v4 Session identifier (Req 1.4)."""

    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(stamp: str) -> datetime:
    """Parse an ISO 8601 UTC timestamp (``...Z`` or offset) to an aware datetime.

    The repository writes timestamps ending in ``Z``; Python's
    :meth:`datetime.fromisoformat` accepts ``+00:00`` but historically not a bare
    ``Z``, so normalise before parsing. The result is always timezone-aware UTC.
    """

    normalised = stamp.replace("Z", "+00:00") if stamp.endswith("Z") else stamp
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ttl_epoch(moment: datetime) -> int:
    """Epoch-seconds TTL for a session last active at ``moment`` (idle + grace)."""

    expiry = moment + IDLE_TIMEOUT + _TTL_GRACE
    return int(expiry.timestamp())


class ConversationManager:
    """Owns Session lifecycle and ascending context assembly over the repository.

    Depends solely on :class:`FleetMateDiagnostics` (never Boto3). "Now" is read
    from an injected clock so the 30-minute idle check and timestamp stamping are
    deterministic under test; session ids come from an injectable factory for the
    same reason.
    """

    def __init__(
        self,
        repository: FleetMateDiagnostics,
        *,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], str] = new_session_id,
    ) -> None:
        self._repo = repository
        self._clock = clock
        self._new_session_id = id_factory

    # -- Session lifecycle -------------------------------------------------

    def ensure_session(
        self, customer_id: str, session_id: Optional[str] = None
    ) -> Outcome[Session]:
        """Return a usable active Session, creating one on demand if needed.

        * When ``session_id`` is ``None`` a brand-new Session is created with a
          unique UUID v4 id and persisted *before* it is returned, so it exists
          before the submission is processed (Req 1.4, Property 2).
        * When ``session_id`` names an existing Session it is reused only if it
          is still usable: a Session idle for >= 30 minutes is closed and
          rejected with ``SESSION_CLOSED`` (Req 3.5), and a Session already
          holding 50 interactions is rejected with ``LIMIT_REACHED`` (Req 3.4).
        * An unknown ``session_id`` yields ``NOT_FOUND``.

        The idle-close check runs first and, when it fires, the Session's stored
        ``status`` is flipped to ``CLOSED`` so the closure is durable.
        """

        if session_id is None:
            return self._create_session(customer_id)

        found = self._repo.get_session(session_id)
        if isinstance(found, Err):
            # Unknown session id -- surface NOT_FOUND unchanged.
            return found

        session = found.value
        now = self._clock()

        # Idle-close gate first (Req 3.5): an already-CLOSED session, or one that
        # has now idled past the window, is not usable.
        if session.status is SessionStatus.CLOSED or self._is_idle(session, now):
            if session.status is not SessionStatus.CLOSED:
                # Persist the closure so the state is durable; ignore a NOT_FOUND
                # race (the session is being treated as closed regardless).
                self._repo.update_session(
                    session.sessionId, status=SessionStatus.CLOSED
                )
            return Err("SESSION_CLOSED", "This session has closed after 30 minutes idle.")

        # Interaction-limit gate (Req 3.4): a full session accepts no more.
        if session.interactionCount >= MAX_INTERACTIONS:
            return Err(
                "LIMIT_REACHED",
                f"This session has reached its limit of {MAX_INTERACTIONS} interactions.",
            )

        return Ok(session)

    def _create_session(self, customer_id: str) -> Outcome[Session]:
        now = self._clock()
        stamp = format_timestamp(now)
        session = Session(
            sessionId=self._new_session_id(),
            customerId=customer_id,
            status=SessionStatus.ACTIVE,
            createdAt=stamp,
            lastSubmissionAt=stamp,
            interactionCount=0,
            ttl=_ttl_epoch(now),
        )
        stored = self._repo.put_session(session)
        if isinstance(stored, Err):
            return stored
        return Ok(stored.value)

    def is_active(self, session_id: str, now: Optional[datetime] = None) -> bool:
        """Return ``True`` iff the Session exists, is ``ACTIVE``, and is not idle.

        A Session is *not* active when it is unknown, already ``CLOSED``, or has
        been idle (no submission) for 30 continuous minutes or more measured
        against ``lastSubmissionAt`` -- the authoritative idle gate (Req 3.5).
        ``now`` defaults to the injected clock but may be supplied explicitly so
        callers (and tests) can evaluate activeness at a chosen instant.
        """

        found = self._repo.get_session(session_id)
        if isinstance(found, Err):
            return False
        session = found.value
        if session.status is SessionStatus.CLOSED:
            return False
        moment = now if now is not None else self._clock()
        return not self._is_idle(session, moment)

    @staticmethod
    def _is_idle(session: Session, now: datetime) -> bool:
        """Idle iff ``now - lastSubmissionAt >= 30 minutes`` (inclusive boundary)."""

        elapsed = now - _parse_iso(session.lastSubmissionAt)
        return elapsed >= IDLE_TIMEOUT

    # -- Context assembly --------------------------------------------------

    def get_context(self, session_id: str) -> list[InteractionRecord]:
        """Return all of a Session's Interaction_Records ascending by ``createdAt``.

        This is the ordered context handed to the AI_Engine (Req 3.1). The
        repository already returns records ascending and paginated at 50; since a
        Session never exceeds 50 interactions (Req 3.4) a single page covers it,
        but every page is drained so the invariant holds even if the cap ever
        changes. A known-but-empty (or unknown) Session yields an empty list.
        """

        records: list[InteractionRecord] = []
        page_token: Optional[str] = None
        while True:
            outcome = self._repo.get_session_history(session_id, page_token)
            if isinstance(outcome, Err):
                # The repository does not distinguish unknown from empty here;
                # either way there is no context to supply.
                break
            page = outcome.value
            records.extend(page.records)
            page_token = page.nextPageToken
            if not page_token:
                break
        return records

    # -- Interaction append ------------------------------------------------

    def append_interaction(
        self,
        session_id: str,
        symptom_description: str,
        result: DiagnosticResult,
        *,
        created_at: Optional[str] = None,
    ) -> Outcome[InteractionRecord]:
        """Persist a new Interaction_Record for the Session, maintaining order.

        The record is appended through the repository, whose sort-key design
        keeps records ordered ascending by ``createdAt`` (Req 3.3). The Session's
        ``lastSubmissionAt`` is advanced to the record's timestamp (resetting the
        idle clock), ``interactionCount`` is incremented, and the DynamoDB TTL is
        refreshed.

        The 50-interaction limit is enforced defensively (Req 3.4): if the
        Session is already full the append is rejected with ``LIMIT_REACHED`` and
        nothing is written, so the count can never exceed 50. An unknown Session
        yields ``NOT_FOUND``.
        """

        found = self._repo.get_session(session_id)
        if isinstance(found, Err):
            return found
        session = found.value

        if session.interactionCount >= MAX_INTERACTIONS:
            return Err(
                "LIMIT_REACHED",
                f"This session has reached its limit of {MAX_INTERACTIONS} interactions.",
            )

        stamp = created_at if created_at is not None else format_timestamp(self._clock())
        appended = self._repo.append_record(
            session_id,
            symptom_description,
            result,
            created_at=stamp,
        )
        if isinstance(appended, Err):
            return appended

        new_count = session.interactionCount + 1
        last_at = _parse_iso(stamp)
        self._repo.update_session(
            session_id,
            last_submission_at=stamp,
            interaction_count=new_count,
            ttl=_ttl_epoch(last_at),
        )
        return appended

    # -- Follow-up validation ---------------------------------------------

    def validate_follow_up(self, text: Optional[str]) -> Outcome[str]:
        """Validate a follow-up Symptom_Description via the shared validator.

        Delegates to :func:`shared.validation.validate_symptom` so follow-up
        submissions reject identically to first submissions (Req 3.2). This is a
        pure check: on rejection the Session and its existing context are left
        untouched because nothing is written here -- callers must gate the append
        on this outcome.
        """

        return validate_symptom(text)
