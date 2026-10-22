"""Verification tests for the ``FleetMateDiagnostics`` repository (Task 3.1).

These exercise the repository against the in-memory ``FakeDynamoClient`` so the
item mapping, UUID v4 ids, millisecond ISO timestamps, session CRUD, record
append, ascending/paginated history, and inclusive ascending date-range queries
are verified without importing ``boto3`` or calling AWS.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from itertools import count

import pytest

from diagnostics.models import DiagnosticResult, Session, SessionStatus
from diagnostics.repositories import (
    HISTORY_PAGE_SIZE,
    FleetMateDiagnostics,
    format_timestamp,
    new_record_id,
)
from shared.outcomes import Err, Ok

from .fake_dynamodb import FakeDynamoClient

TABLE = "FleetMateDiagnostics-test"

_ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _repo(client: FakeDynamoClient | None = None, **kwargs) -> FleetMateDiagnostics:
    return FleetMateDiagnostics(client or FakeDynamoClient(), TABLE, **kwargs)


def _success_result(issue: str = "Weak Battery") -> DiagnosticResult:
    return DiagnosticResult(
        probableIssue=issue,
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


def _active_session(session_id: str = "s1", customer_id: str = "c1") -> Session:
    stamp = "2024-01-01T00:00:00.000Z"
    return Session(
        sessionId=session_id,
        customerId=customer_id,
        status=SessionStatus.ACTIVE,
        createdAt=stamp,
        lastSubmissionAt=stamp,
        interactionCount=0,
        ttl=1_900_000_000,
    )


# ---------------------------------------------------------------------------
# Identity & time helpers
# ---------------------------------------------------------------------------


def test_new_record_id_is_uuid_v4() -> None:
    rid = new_record_id()
    parsed = uuid.UUID(rid)
    assert parsed.version == 4


def test_format_timestamp_is_utc_millisecond_iso() -> None:
    moment = datetime(2024, 3, 2, 10, 20, 30, 123456, tzinfo=timezone.utc)
    stamp = format_timestamp(moment)
    assert stamp == "2024-03-02T10:20:30.123Z"
    assert _ISO_MS.match(stamp)


def test_format_timestamp_normalizes_non_utc_offset() -> None:
    from datetime import timedelta

    tz = timezone(timedelta(hours=5, minutes=30))
    moment = datetime(2024, 3, 2, 15, 50, 30, 500000, tzinfo=tz)
    assert format_timestamp(moment) == "2024-03-02T10:20:30.500Z"


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def test_put_then_get_session_round_trips() -> None:
    repo = _repo()
    session = _active_session()
    assert isinstance(repo.put_session(session), Ok)

    got = repo.get_session("s1")
    assert isinstance(got, Ok)
    assert got.value == session


def test_get_unknown_session_returns_not_found() -> None:
    got = _repo().get_session("missing")
    assert isinstance(got, Err)
    assert got.code == "NOT_FOUND"


def test_update_session_patches_only_given_fields() -> None:
    repo = _repo()
    repo.put_session(_active_session())

    updated = repo.update_session(
        "s1",
        status=SessionStatus.CLOSED,
        last_submission_at="2024-01-01T00:30:00.000Z",
        interaction_count=3,
    )
    assert isinstance(updated, Ok)
    assert updated.value.status is SessionStatus.CLOSED
    assert updated.value.lastSubmissionAt == "2024-01-01T00:30:00.000Z"
    assert updated.value.interactionCount == 3
    # Unspecified fields are preserved.
    assert updated.value.customerId == "c1"
    assert updated.value.createdAt == "2024-01-01T00:00:00.000Z"


def test_update_unknown_session_returns_not_found() -> None:
    out = _repo().update_session("ghost", interaction_count=1)
    assert isinstance(out, Err)
    assert out.code == "NOT_FOUND"


# ---------------------------------------------------------------------------
# append_record: identity + time ownership and persistence round-trip
# ---------------------------------------------------------------------------


def test_append_record_mints_uuid_and_ms_timestamp() -> None:
    repo = _repo()
    out = repo.append_record("s1", "engine wont start", _success_result())
    assert isinstance(out, Ok)
    rec = out.value
    assert uuid.UUID(rec.recordId).version == 4
    assert _ISO_MS.match(rec.createdAt)


def test_append_record_round_trips_faithfully_with_fake_clock() -> None:
    fixed = datetime(2024, 5, 6, 7, 8, 9, 250000, tzinfo=timezone.utc)
    ids = (f"id-{n}" for n in count())
    repo = _repo(clock=lambda: fixed, id_factory=lambda: next(ids))

    result = _success_result("Alternator Fault")
    out = repo.append_record("sess-9", "lights dim at idle", result)
    assert isinstance(out, Ok)
    assert out.value.createdAt == "2024-05-06T07:08:09.250Z"

    page = repo.get_session_history("sess-9")
    assert isinstance(page, Ok)
    (read_back,) = page.value.records
    assert read_back.symptomDescription == "lights dim at idle"
    assert read_back.result == result
    assert read_back.sessionId == "sess-9"
    assert read_back.createdAt == "2024-05-06T07:08:09.250Z"


def test_append_record_round_trips_insufficient_info_result() -> None:
    repo = _repo()
    result = DiagnosticResult(needsMoreDetail=True, message="Please add detail.")
    repo.append_record("s1", "weird noise", result)

    page = repo.get_session_history("s1")
    assert isinstance(page, Ok)
    (rec,) = page.value.records
    assert rec.result == result
    assert rec.result.probableIssue is None


# ---------------------------------------------------------------------------
# Forward-only owner attribution: customerId + GSI2 key attributes (Task 28.1)
# ---------------------------------------------------------------------------


def _stored_record_item(
    client: FakeDynamoClient, session_id: str, created_at: str, record_id: str
) -> dict:
    """Read the raw stored DynamoDB item map for a persisted record."""
    key = {
        "PK": {"S": f"SESSION#{session_id}"},
        "SK": {"S": f"REC#{created_at}#{record_id}"},
    }
    resp = client.get_item(TableName=TABLE, Key=key)
    return resp["Item"]


def test_append_record_with_customer_id_writes_gsi2_keys() -> None:
    client = FakeDynamoClient()
    repo = _repo(client)
    stamp = "2024-06-01T10:00:00.000Z"

    out = repo.append_record(
        "sess-1",
        "engine stalls",
        _success_result(),
        customer_id="cust-42",
        created_at=stamp,
        record_id="rec-1",
    )
    assert isinstance(out, Ok)
    assert out.value.customerId == "cust-42"

    item = _stored_record_item(client, "sess-1", stamp, "rec-1")
    assert item["customerId"]["S"] == "cust-42"
    assert item["GSI2PK"]["S"] == "CUSTOMER#cust-42"
    assert item["GSI2SK"]["S"] == f"{stamp}#rec-1"


def test_append_record_without_customer_id_omits_customer_attributes() -> None:
    client = FakeDynamoClient()
    repo = _repo(client)
    stamp = "2024-06-01T10:00:00.000Z"

    out = repo.append_record(
        "sess-1",
        "engine stalls",
        _success_result(),
        created_at=stamp,
        record_id="rec-1",
    )
    assert isinstance(out, Ok)
    assert out.value.customerId is None

    item = _stored_record_item(client, "sess-1", stamp, "rec-1")
    # Forward-only: no customerId or GSI2 keys for owner-less records.
    assert "customerId" not in item
    assert "GSI2PK" not in item
    assert "GSI2SK" not in item


def test_append_record_customer_id_round_trips_through_history() -> None:
    repo = _repo()
    stamp = "2024-06-01T10:00:00.000Z"
    repo.append_record(
        "sess-1",
        "engine stalls",
        _success_result(),
        customer_id="cust-42",
        created_at=stamp,
        record_id="rec-1",
    )

    page = repo.get_session_history("sess-1")
    assert isinstance(page, Ok)
    (rec,) = page.value.records
    assert rec.customerId == "cust-42"


def test_history_read_tolerates_records_without_customer_id() -> None:
    """Records written before owner attribution read back with customerId=None."""
    repo = _repo()
    repo.append_record(
        "sess-1",
        "engine stalls",
        _success_result(),
        created_at="2024-06-01T10:00:00.000Z",
        record_id="rec-1",
    )

    page = repo.get_session_history("sess-1")
    assert isinstance(page, Ok)
    (rec,) = page.value.records
    assert rec.customerId is None


# ---------------------------------------------------------------------------
# Ordered, paginated session history
# ---------------------------------------------------------------------------


def _append_n(repo: FleetMateDiagnostics, session_id: str, n: int) -> list[str]:
    """Append ``n`` records with increasing timestamps; return created ids."""
    ids: list[str] = []
    for i in range(n):
        # Strictly increasing millisecond timestamps so lexical SK order
        # equals chronological order.
        stamp = f"2024-01-01T00:00:00.{i:03d}Z"
        out = repo.append_record(
            session_id, f"symptom {i}", _success_result(), created_at=stamp
        )
        assert isinstance(out, Ok)
        ids.append(out.value.recordId)
    return ids


def test_history_returns_ascending_by_created_at() -> None:
    repo = _repo()
    # Insert out of chronological order.
    for stamp in ["...3", "...1", "...2"]:
        i = {"...1": 1, "...2": 2, "...3": 3}[stamp]
        repo.append_record(
            "s1",
            f"symptom {i}",
            _success_result(),
            created_at=f"2024-01-01T00:00:00.{i:03d}Z",
            record_id=f"r{i}",
        )

    page = repo.get_session_history("s1")
    assert isinstance(page, Ok)
    stamps = [r.createdAt for r in page.value.records]
    assert stamps == sorted(stamps)
    assert [r.recordId for r in page.value.records] == ["r1", "r2", "r3"]


def test_history_empty_known_session_returns_empty_page() -> None:
    page = _repo().get_session_history("s-empty")
    assert isinstance(page, Ok)
    assert page.value.records == ()
    assert page.value.nextPageToken is None


def test_history_paginates_at_50_and_concatenates_completely() -> None:
    repo = _repo()
    total = 120
    created_ids = _append_n(repo, "s1", total)

    collected = []
    token = None
    pages = 0
    while True:
        out = repo.get_session_history("s1", token)
        assert isinstance(out, Ok)
        page = out.value
        assert len(page.records) <= HISTORY_PAGE_SIZE
        collected.extend(page.records)
        pages += 1
        token = page.nextPageToken
        if token is None:
            break

    assert pages == 3  # 50 + 50 + 20
    # Complete set exactly once, ascending, no duplicates/omissions.
    assert len(collected) == total
    assert len({r.recordId for r in collected}) == total
    stamps = [r.createdAt for r in collected]
    assert stamps == sorted(stamps)
    assert {r.recordId for r in collected} == set(created_ids)


# ---------------------------------------------------------------------------
# Date-range queries over GSI1
# ---------------------------------------------------------------------------


def _append_at(repo: FleetMateDiagnostics, session_id: str, stamp: str, rid: str) -> None:
    repo.append_record(
        session_id, "symptom", _success_result(), created_at=stamp, record_id=rid
    )


def test_date_range_returns_inclusive_in_range_ascending() -> None:
    repo = _repo()
    _append_at(repo, "s1", "2024-01-01T00:00:00.000Z", "a")  # before
    _append_at(repo, "s1", "2024-01-02T00:00:00.000Z", "b")  # == start (inclusive)
    _append_at(repo, "s2", "2024-01-03T12:00:00.000Z", "c")  # inside
    _append_at(repo, "s3", "2024-01-04T00:00:00.000Z", "d")  # == end (inclusive)
    _append_at(repo, "s3", "2024-01-05T00:00:00.000Z", "e")  # after

    out = repo.query_by_date_range(
        "2024-01-02T00:00:00.000Z", "2024-01-04T00:00:00.000Z"
    )
    assert isinstance(out, Ok)
    assert [r.recordId for r in out.value] == ["b", "c", "d"]
    stamps = [r.createdAt for r in out.value]
    assert stamps == sorted(stamps)


def test_date_range_includes_all_records_sharing_end_timestamp() -> None:
    repo = _repo()
    end = "2024-01-04T00:00:00.000Z"
    _append_at(repo, "s1", end, "z1")
    _append_at(repo, "s1", end, "z2")

    out = repo.query_by_date_range("2024-01-01T00:00:00.000Z", end)
    assert isinstance(out, Ok)
    assert {r.recordId for r in out.value} == {"z1", "z2"}


def test_inverted_date_range_is_rejected_with_no_records() -> None:
    repo = _repo()
    _append_at(repo, "s1", "2024-01-02T00:00:00.000Z", "a")

    out = repo.query_by_date_range(
        "2024-01-05T00:00:00.000Z", "2024-01-01T00:00:00.000Z"
    )
    assert isinstance(out, Err)
    assert out.code == "INVALID_RANGE"


# ---------------------------------------------------------------------------
# Owner-scoped most-recent-prior lookup over GSI2 (Task 28.3)
# ---------------------------------------------------------------------------


def _append_owned(
    repo: FleetMateDiagnostics,
    customer_id: str,
    session_id: str,
    stamp: str,
    rid: str,
) -> None:
    out = repo.append_record(
        session_id,
        "symptom",
        _success_result(),
        customer_id=customer_id,
        created_at=stamp,
        record_id=rid,
    )
    assert isinstance(out, Ok)


def test_most_recent_prior_selects_newest_outside_current_session() -> None:
    repo = _repo()
    _append_owned(repo, "cust-1", "s-old", "2024-01-01T00:00:00.000Z", "r1")
    _append_owned(repo, "cust-1", "s-old", "2024-01-02T00:00:00.000Z", "r2")
    _append_owned(repo, "cust-1", "s-mid", "2024-01-03T00:00:00.000Z", "r3")

    got = repo.query_most_recent_prior("cust-1", "s-current")
    assert got is not None
    assert got.recordId == "r3"
    assert got.sessionId == "s-mid"
    assert got.customerId == "cust-1"


def test_most_recent_prior_skips_current_session_record() -> None:
    repo = _repo()
    # Newest record belongs to the current session and must be skipped.
    _append_owned(repo, "cust-1", "s-prior", "2024-01-01T00:00:00.000Z", "r1")
    _append_owned(repo, "cust-1", "s-current", "2024-01-05T00:00:00.000Z", "r-cur")

    got = repo.query_most_recent_prior("cust-1", "s-current")
    assert got is not None
    assert got.recordId == "r1"
    assert got.sessionId == "s-prior"


def test_most_recent_prior_returns_none_when_only_current_session_records() -> None:
    repo = _repo()
    _append_owned(repo, "cust-1", "s-current", "2024-01-01T00:00:00.000Z", "r1")
    _append_owned(repo, "cust-1", "s-current", "2024-01-02T00:00:00.000Z", "r2")

    assert repo.query_most_recent_prior("cust-1", "s-current") is None


def test_most_recent_prior_returns_none_when_customer_owns_no_records() -> None:
    repo = _repo()
    # A different customer's records must never satisfy the lookup.
    _append_owned(repo, "cust-other", "s-x", "2024-01-01T00:00:00.000Z", "r1")

    assert repo.query_most_recent_prior("cust-1", "s-current") is None


def test_most_recent_prior_breaks_timestamp_tie_on_greater_record_id() -> None:
    repo = _repo()
    stamp = "2024-01-03T00:00:00.000Z"
    _append_owned(repo, "cust-1", "s-a", stamp, "r-aaa")
    _append_owned(repo, "cust-1", "s-b", stamp, "r-zzz")

    got = repo.query_most_recent_prior("cust-1", "s-current")
    assert got is not None
    # GSI2SK = "{createdAt}#{recordId}" descending -> greater recordId wins ties.
    assert got.recordId == "r-zzz"


# ---------------------------------------------------------------------------
# Boto3 isolation: the repository module never imports the SDK
# ---------------------------------------------------------------------------


def test_repository_does_not_import_boto3() -> None:
    """The repository is the Boto3 seam but must not statically import the SDK.

    Isolation here means production injects a real ``boto3.client`` from the
    composition root; the module itself never imports ``boto3``/``botocore`` so
    unit tests run with a fake and callers never pull in the SDK transitively.
    We assert on the parsed import graph, not on prose, so docstrings mentioning
    the SDK by name do not trigger a false failure.
    """
    import ast
    from pathlib import Path

    import diagnostics.repositories.fleetmate_diagnostics as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "boto3" not in imported
    assert "botocore" not in imported


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
