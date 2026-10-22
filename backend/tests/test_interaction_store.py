"""Verification tests for the Interaction_Store read side (Task 10.1).

Exercise :class:`InteractionStore` over the real repository backed by the
in-memory ``FakeDynamoClient``, asserting the fail-closed authorization order,
inverted-range rejection, ascending in-range results, owner-only history,
NOT_FOUND for unknown sessions, empty pages for known-but-empty sessions, and
50-record pagination.

_Requirements: 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5_
"""

from __future__ import annotations

import pytest

from diagnostics.models import DiagnosticResult, Session, SessionStatus
from diagnostics.repositories import HISTORY_PAGE_SIZE, FleetMateDiagnostics
from diagnostics.services import InteractionStore
from shared.auth import ANALYST_GROUP, Principal, Role
from shared.outcomes import Err, Ok

from .fake_dynamodb import FakeDynamoClient

TABLE = "FleetMateDiagnostics-test"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _store(client: FakeDynamoClient | None = None) -> tuple[InteractionStore, FleetMateDiagnostics]:
    repo = FleetMateDiagnostics(client or FakeDynamoClient(), TABLE)
    return InteractionStore(repo), repo


def _analyst(user_id: str = "a1") -> Principal:
    return Principal(user_id, Role.ANALYST, frozenset({ANALYST_GROUP}))


def _customer(user_id: str = "c1") -> Principal:
    return Principal(user_id, Role.CUSTOMER)


def _result(issue: str = "Weak Battery") -> DiagnosticResult:
    return DiagnosticResult(
        probableIssue=issue,
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


def _put_session(repo: FleetMateDiagnostics, session_id: str, customer_id: str) -> None:
    stamp = "2024-01-01T00:00:00.000Z"
    repo.put_session(
        Session(
            sessionId=session_id,
            customerId=customer_id,
            status=SessionStatus.ACTIVE,
            createdAt=stamp,
            lastSubmissionAt=stamp,
            interactionCount=0,
            ttl=1_900_000_000,
        )
    )


def _append_at(
    repo: FleetMateDiagnostics, session_id: str, stamp: str, rid: str
) -> None:
    repo.append_record(
        session_id, "symptom", _result(), created_at=stamp, record_id=rid
    )


def _append_owned_at(
    repo: FleetMateDiagnostics,
    session_id: str,
    customer_id: str,
    stamp: str,
    rid: str,
) -> None:
    """Append an owner-attributed record so it participates in the GSI2 lookup."""
    repo.append_record(
        session_id,
        "symptom",
        _result(),
        customer_id=customer_id,
        created_at=stamp,
        record_id=rid,
    )


# ---------------------------------------------------------------------------
# query_by_date_range: analyst RBAC first, then range, then ascending read
# ---------------------------------------------------------------------------


def test_date_range_analyst_gets_inclusive_in_range_ascending() -> None:
    store, repo = _store()
    _append_at(repo, "s1", "2024-01-01T00:00:00.000Z", "a")  # before
    _append_at(repo, "s1", "2024-01-02T00:00:00.000Z", "b")  # == start
    _append_at(repo, "s2", "2024-01-03T12:00:00.000Z", "c")  # inside
    _append_at(repo, "s3", "2024-01-04T00:00:00.000Z", "d")  # == end
    _append_at(repo, "s3", "2024-01-05T00:00:00.000Z", "e")  # after

    out = store.query_by_date_range(
        _analyst(), "2024-01-02T00:00:00.000Z", "2024-01-04T00:00:00.000Z"
    )
    assert isinstance(out, Ok)
    assert [r.recordId for r in out.value] == ["b", "c", "d"]
    stamps = [r.createdAt for r in out.value]
    assert stamps == sorted(stamps)


def test_date_range_denies_non_analyst_with_no_records() -> None:
    """A Customer (no analyst role) is denied before any record is read (Req 4.5)."""
    store, repo = _store()
    _append_at(repo, "s1", "2024-01-02T00:00:00.000Z", "a")

    out = store.query_by_date_range(
        _customer(), "2024-01-01T00:00:00.000Z", "2024-01-31T00:00:00.000Z"
    )
    assert isinstance(out, Err)
    assert out.code == "ACCESS_DENIED"


def test_date_range_rejects_inverted_range_for_analyst() -> None:
    store, repo = _store()
    _append_at(repo, "s1", "2024-01-02T00:00:00.000Z", "a")

    out = store.query_by_date_range(
        _analyst(), "2024-01-05T00:00:00.000Z", "2024-01-01T00:00:00.000Z"
    )
    assert isinstance(out, Err)
    assert out.code == "INVALID_RANGE"


def test_date_range_authorization_precedes_range_validation() -> None:
    """An unauthorized caller is denied even when the range is also invalid.

    Fail-closed ordering: the analyst RBAC gate runs first, so a non-analyst
    gets ``ACCESS_DENIED`` rather than ``INVALID_RANGE`` (and no records).
    """
    store, _ = _store()
    out = store.query_by_date_range(
        _customer(), "2024-01-05T00:00:00.000Z", "2024-01-01T00:00:00.000Z"
    )
    assert isinstance(out, Err)
    assert out.code == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# get_session_history: owner-only, NOT_FOUND, empty, pagination
# ---------------------------------------------------------------------------


def test_history_owner_gets_ascending_records() -> None:
    store, repo = _store()
    _put_session(repo, "s1", "c1")
    _append_at(repo, "s1", "2024-01-01T00:00:00.002Z", "r2")
    _append_at(repo, "s1", "2024-01-01T00:00:00.001Z", "r1")

    out = store.get_session_history(_customer("c1"), "s1")
    assert isinstance(out, Ok)
    assert [r.recordId for r in out.value.records] == ["r1", "r2"]
    assert out.value.nextPageToken is None


def test_history_denies_non_owner_with_no_records() -> None:
    """A Customer requesting a session they do not own is denied (Req 5.4)."""
    store, repo = _store()
    _put_session(repo, "s1", "owner-1")
    _append_at(repo, "s1", "2024-01-01T00:00:00.001Z", "r1")

    out = store.get_session_history(_customer("intruder"), "s1")
    assert isinstance(out, Err)
    assert out.code == "ACCESS_DENIED"


def test_history_unknown_session_returns_not_found() -> None:
    """An unknown session id maps to NOT_FOUND with no records (Req 5.3)."""
    store, _ = _store()
    out = store.get_session_history(_customer("c1"), "ghost")
    assert isinstance(out, Err)
    assert out.code == "NOT_FOUND"


def test_history_known_but_empty_session_returns_empty_page() -> None:
    """A known session with no records yields an empty page, not an error (Req 5.2)."""
    store, repo = _store()
    _put_session(repo, "s-empty", "c1")

    out = store.get_session_history(_customer("c1"), "s-empty")
    assert isinstance(out, Ok)
    assert out.value.records == ()
    assert out.value.nextPageToken is None


def test_history_paginates_at_50() -> None:
    """Owner history is capped at 50 records per page and fully traversable (Req 5.5)."""
    store, repo = _store()
    _put_session(repo, "s1", "c1")
    total = 120
    for i in range(total):
        _append_at(repo, "s1", f"2024-01-01T00:00:00.{i:03d}Z", f"r{i:03d}")

    collected = []
    token = None
    pages = 0
    while True:
        out = store.get_session_history(_customer("c1"), "s1", token)
        assert isinstance(out, Ok)
        page = out.value
        assert len(page.records) <= HISTORY_PAGE_SIZE
        collected.extend(page.records)
        pages += 1
        token = page.nextPageToken
        if token is None:
            break

    assert pages == 3  # 50 + 50 + 20
    assert len(collected) == total
    stamps = [r.createdAt for r in collected]
    assert stamps == sorted(stamps)


def test_history_denial_does_not_depend_on_underlying_data() -> None:
    """Owner check happens before reading records: denial returns zero records.

    Even though the non-owned session has records, the non-owner never sees
    them -- the ACCESS_DENIED outcome carries no Page.
    """
    store, repo = _store()
    _put_session(repo, "s1", "owner-1")
    for i in range(5):
        _append_at(repo, "s1", f"2024-01-01T00:00:00.{i:03d}Z", f"r{i}")

    out = store.get_session_history(_customer("not-owner"), "s1")
    assert isinstance(out, Err)
    assert out.code == "ACCESS_DENIED"
    assert not hasattr(out, "records")


# ---------------------------------------------------------------------------
# get_most_recent_prior_interaction: owner-scoped, newest-outside-session, None
# _Requirements: 13.1, 13.3, 13.4, 13.5, 13.6_
# ---------------------------------------------------------------------------


def test_prior_returns_newest_record_outside_current_session_for_owner() -> None:
    """Owner's newest record whose session differs from current is returned (Req 13.1, 13.3)."""
    store, repo = _store()
    # Three records owned by c1 across two prior sessions and the current one.
    _append_owned_at(repo, "s-old", "c1", "2024-01-01T00:00:00.000Z", "r-old")
    _append_owned_at(repo, "s-prior", "c1", "2024-01-02T00:00:00.000Z", "r-prior")
    _append_owned_at(repo, "s-current", "c1", "2024-01-03T00:00:00.000Z", "r-current")

    record = store.get_most_recent_prior_interaction("c1", "s-current")
    assert record is not None
    # Newest record outside s-current is r-prior, not the current-session r-current.
    assert record.recordId == "r-prior"
    assert record.sessionId == "s-prior"


def test_prior_returns_none_when_owner_has_only_current_session_records() -> None:
    """When every owned record is in the current session, there is no prior (Req 13.5)."""
    store, repo = _store()
    _append_owned_at(repo, "s-current", "c1", "2024-01-01T00:00:00.000Z", "r1")
    _append_owned_at(repo, "s-current", "c1", "2024-01-02T00:00:00.000Z", "r2")

    assert store.get_most_recent_prior_interaction("c1", "s-current") is None


def test_prior_returns_none_when_owner_has_no_records() -> None:
    """A Customer owning no records has no prior interaction (Req 13.5)."""
    store, _ = _store()
    assert store.get_most_recent_prior_interaction("c1", "s-current") is None


def test_prior_never_returns_another_customers_records() -> None:
    """Owner-scoping is structural: a Customer owning nothing sees no other owner's records (Req 13.4)."""
    store, repo = _store()
    # Records attributed to owner c1 populate c1's GSI2 partition only.
    _append_owned_at(repo, "s-prior", "c1", "2024-01-01T00:00:00.000Z", "r-prior")

    # A different customer owns nothing and must never see c1's records: the
    # GSI2 partition CUSTOMER#other is empty by construction.
    assert store.get_most_recent_prior_interaction("other", "s-current") is None
    # Sanity: c1 does see its own prior interaction.
    owned = store.get_most_recent_prior_interaction("c1", "s-current")
    assert owned is not None and owned.recordId == "r-prior"


def test_prior_blank_customer_id_causes_no_read_and_returns_none() -> None:
    """Blank/whitespace customerId fails closed: no read, returns None (Req 13.4)."""
    store, repo = _store()
    _append_owned_at(repo, "s-prior", "c1", "2024-01-01T00:00:00.000Z", "r-prior")

    for blank in ("", "   ", "\t\n"):
        assert store.get_most_recent_prior_interaction(blank, "s-current") is None


def test_prior_blank_current_session_id_causes_no_read_and_returns_none() -> None:
    """Blank/whitespace currentSessionId performs no read and returns None (Req 13.6)."""
    store, repo = _store()
    _append_owned_at(repo, "s-prior", "c1", "2024-01-01T00:00:00.000Z", "r-prior")

    for blank in ("", "   ", "\t\n"):
        assert store.get_most_recent_prior_interaction("c1", blank) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
