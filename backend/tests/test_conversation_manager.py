"""Verification tests for the Conversation_Manager (Task 5.1).

These exercise session lifecycle, ascending context assembly, the
50-interaction limit, and the 30-minute idle close against a *real*
``FleetMateDiagnostics`` repository backed by the in-memory ``FakeDynamoClient``
-- so the manager is verified end-to-end over its only dependency (the
repository) without importing ``boto3`` or touching AWS.

A deterministic fake clock drives the 30-minute idle check (design: injectable
clock for Property 8) so the boundary behaviour is exact and reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from diagnostics.models import DiagnosticResult, SessionStatus
from diagnostics.repositories import FleetMateDiagnostics
from diagnostics.services import MAX_INTERACTIONS, ConversationManager
from shared.outcomes import Err, Ok

from .fake_dynamodb import FakeDynamoClient

TABLE = "FleetMateDiagnostics-test"
BASE = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """A mutable clock returning a chosen instant; advance it to drive timeouts."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def _success_result(issue: str = "Weak Battery") -> DiagnosticResult:
    return DiagnosticResult(
        probableIssue=issue,
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


def _manager(clock: FakeClock, ids: list[str] | None = None):
    repo = FleetMateDiagnostics(FakeDynamoClient(), TABLE)
    if ids is not None:
        seq = iter(ids)
        id_factory = lambda: next(seq)  # noqa: E731
    else:
        id_factory = None
    kwargs = {"clock": clock}
    if id_factory is not None:
        kwargs["id_factory"] = id_factory
    return ConversationManager(repo, **kwargs), repo


# ---------------------------------------------------------------------------
# ensure_session: on-demand creation and uniqueness (Req 1.4)
# ---------------------------------------------------------------------------


def test_ensure_session_creates_active_session_on_demand():
    clock = FakeClock(BASE)
    manager, repo = _manager(clock, ids=["sess-1"])

    outcome = manager.ensure_session("cust-1")

    assert isinstance(outcome, Ok)
    session = outcome.value
    assert session.sessionId == "sess-1"
    assert session.customerId == "cust-1"
    assert session.status is SessionStatus.ACTIVE
    assert session.interactionCount == 0
    # The session is persisted (exists) before it is returned (Req 1.4).
    stored = repo.get_session("sess-1")
    assert isinstance(stored, Ok)


def test_ensure_session_creates_unique_ids_across_sessions():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock, ids=["a", "b", "c"])

    ids = {manager.ensure_session("cust").value.sessionId for _ in range(3)}

    assert ids == {"a", "b", "c"}  # three distinct identifiers


def test_ensure_session_unknown_id_returns_not_found():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock)

    outcome = manager.ensure_session("cust", session_id="missing")

    assert isinstance(outcome, Err)
    assert outcome.code == "NOT_FOUND"


def test_ensure_session_reuses_existing_active_session():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock, ids=["sess-1"])
    created = manager.ensure_session("cust-1").value

    reused = manager.ensure_session("cust-1", session_id=created.sessionId)

    assert isinstance(reused, Ok)
    assert reused.value.sessionId == created.sessionId


# ---------------------------------------------------------------------------
# 30-minute idle close against lastSubmissionAt (Req 3.5)
# ---------------------------------------------------------------------------


def test_is_active_true_just_before_30_minute_boundary():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value

    clock.advance(timedelta(minutes=30) - timedelta(seconds=1))

    assert manager.is_active(session.sessionId) is True


def test_is_active_false_at_exactly_30_minutes():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value

    clock.advance(timedelta(minutes=30))

    assert manager.is_active(session.sessionId) is False


def test_ensure_session_rejects_idle_closed_session():
    clock = FakeClock(BASE)
    manager, repo = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value

    clock.advance(timedelta(minutes=30))
    outcome = manager.ensure_session("cust-1", session_id=session.sessionId)

    assert isinstance(outcome, Err)
    assert outcome.code == "SESSION_CLOSED"
    # The closure is persisted durably.
    assert repo.get_session(session.sessionId).value.status is SessionStatus.CLOSED


def test_append_resets_idle_clock():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value

    # 20 minutes pass, then a submission advances lastSubmissionAt.
    clock.advance(timedelta(minutes=20))
    manager.append_interaction(session.sessionId, "clicking noise", _success_result())

    # Another 20 minutes: total 40 from creation, but only 20 since last submit.
    clock.advance(timedelta(minutes=20))
    assert manager.is_active(session.sessionId) is True


# ---------------------------------------------------------------------------
# Ascending context ordering (Req 3.1, 3.3)
# ---------------------------------------------------------------------------


def test_get_context_returns_records_ascending_by_created_at():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value

    # Append three records at increasing (fake-clock) timestamps.
    for i in range(3):
        clock.advance(timedelta(minutes=1))
        manager.append_interaction(session.sessionId, f"symptom {i}", _success_result())

    context = manager.get_context(session.sessionId)

    assert [r.symptomDescription for r in context] == ["symptom 0", "symptom 1", "symptom 2"]
    stamps = [r.createdAt for r in context]
    assert stamps == sorted(stamps)


def test_get_context_empty_session_is_empty_list():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value

    assert manager.get_context(session.sessionId) == []


# ---------------------------------------------------------------------------
# 50-interaction limit (Req 3.4)
# ---------------------------------------------------------------------------


def test_fiftieth_interaction_accepted_fifty_first_rejected():
    clock = FakeClock(BASE)
    manager, repo = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value

    for i in range(MAX_INTERACTIONS):
        clock.advance(timedelta(seconds=1))
        outcome = manager.append_interaction(
            session.sessionId, f"symptom {i}", _success_result()
        )
        assert isinstance(outcome, Ok), f"append {i} should be accepted"

    # The 51st append is rejected and nothing is written.
    clock.advance(timedelta(seconds=1))
    rejected = manager.append_interaction(session.sessionId, "one too many", _success_result())
    assert isinstance(rejected, Err)
    assert rejected.code == "LIMIT_REACHED"

    stored = repo.get_session(session.sessionId).value
    assert stored.interactionCount == MAX_INTERACTIONS  # never exceeds 50


def test_ensure_session_rejects_full_session_with_limit_reached():
    clock = FakeClock(BASE)
    manager, repo = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value
    # Drive the stored count to the cap directly through the repository.
    repo.update_session(session.sessionId, interaction_count=MAX_INTERACTIONS)

    outcome = manager.ensure_session("cust-1", session_id=session.sessionId)

    assert isinstance(outcome, Err)
    assert outcome.code == "LIMIT_REACHED"


# ---------------------------------------------------------------------------
# Follow-up validation delegates and preserves context (Req 3.2)
# ---------------------------------------------------------------------------


def test_validate_follow_up_rejects_empty_and_too_long():
    clock = FakeClock(BASE)
    manager, _ = _manager(clock)

    empty = manager.validate_follow_up("   ")
    too_long = manager.validate_follow_up("x" * 2001)
    valid = manager.validate_follow_up("brakes squeal when stopping")

    assert isinstance(empty, Err) and empty.code == "EMPTY"
    assert isinstance(too_long, Err) and too_long.code == "TOO_LONG"
    assert isinstance(valid, Ok)


def test_rejected_follow_up_leaves_session_and_context_unchanged():
    clock = FakeClock(BASE)
    manager, repo = _manager(clock, ids=["sess-1"])
    session = manager.ensure_session("cust-1").value
    manager.append_interaction(session.sessionId, "initial symptom", _success_result())
    before_context = manager.get_context(session.sessionId)
    before_session = repo.get_session(session.sessionId).value

    # A rejected validation performs no writes; the caller gates the append on it.
    rejected = manager.validate_follow_up("")
    assert isinstance(rejected, Err)

    after_context = manager.get_context(session.sessionId)
    after_session = repo.get_session(session.sessionId).value
    assert [r.recordId for r in after_context] == [r.recordId for r in before_context]
    assert after_session == before_session
