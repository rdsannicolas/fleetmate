"""Verification tests for the asynchronous Interaction_Store write path (Task 9.1).

These exercise event emission (``EventBridgePublisher`` + the ``DiagnosisPublisher``
seam) and the ``PersistenceLambda`` SQS consumer against the in-memory
``FakeDynamoClient`` and a fake EventBridge client, with an injected sleeper/clock
so retry timing is tested without actually waiting. They cover:

* a ``DiagnosisCompleted`` draft round-tripping through its detail dict,
* successful emission building the EventBridge envelope and reporting failures,
* a happy-path SQS write producing a persisted, globally-unique record,
* retry-once-within-5s on a transient failure then success,
* DLQ routing (batch-item-failure) on repeated failure with no partial record
  and a log carrying sessionId + timestamp (Req 4.3),
* an unparseable message routed to the DLQ without writing anything.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from diagnostics.events import (
    RETRY_DELAY_SECONDS,
    DiagnosisCompleted,
    EventBridgePublisher,
    InMemoryPublisher,
    PersistenceLambda,
    persist_draft,
)
from diagnostics.events.diagnosis_completed import EVENT_DETAIL_TYPE, EVENT_SOURCE
from diagnostics.models import DiagnosticResult
from diagnostics.repositories import FleetMateDiagnostics
from shared.logging import get_logger
from shared.outcomes import Err, Ok, is_err, is_ok

from .fake_dynamodb import FakeDynamoClient, FakeDynamoError

TABLE = "FleetMateDiagnostics-test"
STAMP = "2024-01-01T00:00:00.123Z"


def _result(issue: str = "Weak Battery") -> DiagnosticResult:
    return DiagnosticResult(
        probableIssue=issue,
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


def _draft(session_id: str = "s1", issue: str = "Weak Battery") -> DiagnosisCompleted:
    return DiagnosisCompleted(
        sessionId=session_id,
        symptomDescription="Car won't start in the morning.",
        result=_result(issue),
        createdAt=STAMP,
    )


def _sqs_event(*drafts: DiagnosisCompleted, envelope: bool = True) -> dict:
    """Build an SQS batch event whose message bodies carry the drafts."""

    records = []
    for i, draft in enumerate(drafts):
        if envelope:
            body = json.dumps(
                {
                    "source": EVENT_SOURCE,
                    "detail-type": EVENT_DETAIL_TYPE,
                    "detail": draft.to_detail(),
                }
            )
        else:
            body = json.dumps(draft.to_detail())
        records.append({"messageId": f"m{i}", "body": body})
    return {"Records": records}


# ---------------------------------------------------------------------------
# DiagnosisCompleted event model
# ---------------------------------------------------------------------------


def test_draft_round_trips_through_detail_dict():
    draft = _draft()
    restored = DiagnosisCompleted.from_detail(draft.to_detail())
    assert restored == draft


def test_detail_omits_absent_optional_result_fields():
    draft = DiagnosisCompleted(
        sessionId="s1",
        symptomDescription="noise",
        result=DiagnosticResult(needsMoreDetail=True, message="Tell me more."),
        createdAt=STAMP,
    )
    detail = draft.to_detail()
    assert "probableIssue" not in detail["result"]
    assert detail["result"]["needsMoreDetail"] is True
    # And it still round-trips.
    assert DiagnosisCompleted.from_detail(detail) == draft


def test_from_detail_rejects_missing_fields():
    with pytest.raises(KeyError):
        DiagnosisCompleted.from_detail({"sessionId": "s1"})


def test_draft_round_trips_customer_id_through_detail_dict():
    # customerId (owning Customer's Cognito sub) survives the detail round-trip
    # so the persistence Lambda can owner-attribute the record (Req 13.1, 13.4).
    draft = _draft()
    draft = DiagnosisCompleted(
        sessionId=draft.sessionId,
        symptomDescription=draft.symptomDescription,
        result=draft.result,
        createdAt=draft.createdAt,
        customerId="cust-42",
    )
    detail = draft.to_detail()
    assert detail["customerId"] == "cust-42"
    assert DiagnosisCompleted.from_detail(detail) == draft


def test_detail_omits_absent_customer_id_and_parses_as_none():
    # Forward-only: a draft without customerId omits the key, and a message
    # lacking it still parses with customerId=None (backward-compatible).
    draft = _draft()
    assert draft.customerId is None
    detail = draft.to_detail()
    assert "customerId" not in detail
    restored = DiagnosisCompleted.from_detail(detail)
    assert restored.customerId is None
    assert restored == draft


# ---------------------------------------------------------------------------
# Emission side -- EventBridgePublisher + the publisher seam
# ---------------------------------------------------------------------------


class _FakeEventBridge:
    def __init__(self, *, fail: bool = False, failed_entry_count: int = 0) -> None:
        self.calls: list[dict] = []
        self._fail = fail
        self._failed_entry_count = failed_entry_count

    def put_events(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("transport error")
        return {
            "FailedEntryCount": self._failed_entry_count,
            "Entries": [{"EventId": "evt-123"}],
        }


def test_publisher_builds_envelope_and_returns_event_id():
    client = _FakeEventBridge()
    publisher = EventBridgePublisher(client, bus_name="fleetmate-bus")

    outcome = publisher.publish(_draft())

    assert is_ok(outcome)
    assert outcome.value == "evt-123"
    (entry,) = client.calls[0]["Entries"]
    assert entry["Source"] == EVENT_SOURCE
    assert entry["DetailType"] == EVENT_DETAIL_TYPE
    assert entry["EventBusName"] == "fleetmate-bus"
    # Detail is a JSON string carrying the draft.
    assert json.loads(entry["Detail"])["sessionId"] == "s1"


def test_publisher_transport_error_becomes_typed_failure():
    publisher = EventBridgePublisher(_FakeEventBridge(fail=True), bus_name="bus")
    outcome = publisher.publish(_draft())
    assert is_err(outcome) and outcome.code == "PERSIST_FAILED"


def test_publisher_partial_failure_becomes_typed_failure():
    client = _FakeEventBridge(failed_entry_count=1)
    outcome = EventBridgePublisher(client, bus_name="bus").publish(_draft())
    assert is_err(outcome) and outcome.code == "PERSIST_FAILED"


def test_in_memory_publisher_records_and_can_simulate_failure():
    ok_pub = InMemoryPublisher()
    assert is_ok(ok_pub.publish(_draft()))
    assert len(ok_pub.published) == 1

    fail_pub = InMemoryPublisher(fail=True)
    assert is_err(fail_pub.publish(_draft()))
    assert fail_pub.published == []


# ---------------------------------------------------------------------------
# Persistence Lambda -- happy path
# ---------------------------------------------------------------------------


def _repo(client: FakeDynamoClient) -> FleetMateDiagnostics:
    return FleetMateDiagnostics(client, TABLE)


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


def test_persist_draft_writes_record_with_unique_id_and_draft_timestamp():
    client = FakeDynamoClient()
    repo = _repo(client)

    out1 = persist_draft(repo, _draft())
    out2 = persist_draft(repo, _draft())

    assert is_ok(out1) and is_ok(out2)
    # UUID v4 ids are distinct (Req 4.2).
    assert out1.value != out2.value
    # Timestamp from the draft is preserved on the stored record (Req 4.1).
    page = repo.get_session_history("s1")
    assert is_ok(page)
    assert all(r.createdAt == STAMP for r in page.value.records)


def _owned_draft(session_id: str = "s1", customer_id: str = "cust-42") -> DiagnosisCompleted:
    base = _draft(session_id)
    return DiagnosisCompleted(
        sessionId=base.sessionId,
        symptomDescription=base.symptomDescription,
        result=base.result,
        createdAt=base.createdAt,
        customerId=customer_id,
    )


def test_persist_draft_forwards_customer_id_so_record_is_owner_attributed():
    # The draft's customerId must flow through append_record so the persisted
    # record is owner-attributed with its GSI2 keys (Req 13.1, 13.4).
    client = FakeDynamoClient()
    repo = _repo(client)

    out = persist_draft(repo, _owned_draft("s1", "cust-42"))
    assert is_ok(out)

    page = repo.get_session_history("s1")
    assert is_ok(page)
    (record,) = page.value.records
    assert record.customerId == "cust-42"

    # The GSI2 key attributes backing the owner-scoped lookup are present.
    stored = _stored_record_item(client, "s1", STAMP, record.recordId)
    assert stored["customerId"]["S"] == "cust-42"
    assert stored["GSI2PK"]["S"] == "CUSTOMER#cust-42"
    assert stored["GSI2SK"]["S"] == f"{STAMP}#{record.recordId}"


def test_persist_draft_without_customer_id_writes_no_gsi2_keys():
    # Forward-only: a draft lacking customerId persists exactly as before -- no
    # customerId attribute and no GSI2 keys on the stored item.
    client = FakeDynamoClient()
    repo = _repo(client)

    out = persist_draft(repo, _draft("s1"))
    assert is_ok(out)

    page = repo.get_session_history("s1")
    assert is_ok(page)
    (record,) = page.value.records
    assert record.customerId is None

    stored = _stored_record_item(client, "s1", STAMP, record.recordId)
    assert "customerId" not in stored
    assert "GSI2PK" not in stored
    assert "GSI2SK" not in stored


def test_handler_persists_batch_and_reports_no_failures():
    client = FakeDynamoClient()
    sleeps: list[float] = []
    handler = PersistenceLambda(_repo(client), sleeper=sleeps.append)

    resp = handler.handle(_sqs_event(_draft("s1"), _draft("s2")))

    assert resp == {"batchItemFailures": []}
    assert sleeps == []  # no retries on the happy path
    assert is_ok(_repo(client).get_session_history("s1"))


def test_handler_accepts_bare_detail_body_without_envelope():
    client = FakeDynamoClient()
    handler = PersistenceLambda(_repo(client), sleeper=lambda _s: None)
    resp = handler.handle(_sqs_event(_draft("s1"), envelope=False))
    assert resp == {"batchItemFailures": []}


# ---------------------------------------------------------------------------
# Retry-once-within-5s then DLQ routing (Req 4.3)
# ---------------------------------------------------------------------------


class _FlakyDynamo(FakeDynamoClient):
    """Fails ``put_item`` for the first ``fail_times`` record writes, then works."""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self._remaining = fail_times
        self.record_put_attempts = 0

    def put_item(self, *, TableName, Item, **kwargs):
        if Item["SK"]["S"].startswith("REC#"):
            self.record_put_attempts += 1
            if self._remaining > 0:
                self._remaining -= 1
                raise FakeDynamoError("transient write failure")
        return super().put_item(TableName=TableName, Item=Item, **kwargs)


def test_handler_retries_once_within_5s_then_succeeds():
    client = _FlakyDynamo(fail_times=1)
    sleeps: list[float] = []
    handler = PersistenceLambda(_repo(client), sleeper=sleeps.append)

    resp = handler.handle(_sqs_event(_draft("s1")))

    assert resp == {"batchItemFailures": []}  # eventually succeeded
    assert client.record_put_attempts == 2  # one failure + one retry
    assert sleeps == [RETRY_DELAY_SECONDS]  # exactly one retry wait
    assert RETRY_DELAY_SECONDS <= 5.0  # retry happens within the 5s window
    # Record was persisted.
    page = _repo(client).get_session_history("s1")
    assert is_ok(page) and len(page.value.records) == 1


def test_handler_routes_to_dlq_on_repeated_failure_without_partial_record():
    client = _FlakyDynamo(fail_times=99)  # always fails
    sleeps: list[float] = []
    log_stream = io.StringIO()

    def logger_factory(service, correlation_id):
        return get_logger(service, correlation_id, stream=log_stream)

    handler = PersistenceLambda(
        _repo(client), sleeper=sleeps.append, logger_factory=logger_factory
    )

    resp = handler.handle(_sqs_event(_draft("s1")))

    # The message is reported as a batch item failure -> SQS redrive -> DLQ.
    assert resp == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    # Exactly one retry (two attempts total) within the 5s budget.
    assert client.record_put_attempts == 2
    assert sleeps == [RETRY_DELAY_SECONDS]
    # No partial record remains (Req 4.3): history is empty.
    page = _repo(client).get_session_history("s1")
    assert is_ok(page) and page.value.records == ()
    # Failure logged with sessionId and timestamp (Req 4.3).
    logged = [json.loads(line) for line in log_stream.getvalue().splitlines() if line]
    failure_logs = [r for r in logged if r["level"] == "ERROR"]
    assert failure_logs, "expected a failure log line"
    assert failure_logs[-1]["sessionId"] == "s1"
    assert failure_logs[-1]["timestamp"] == STAMP


def test_handler_routes_unparseable_message_to_dlq():
    client = FakeDynamoClient()
    handler = PersistenceLambda(_repo(client), sleeper=lambda _s: None)

    event = {"Records": [{"messageId": "bad", "body": "not-json"}]}
    resp = handler.handle(event)

    assert resp == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_handler_isolates_failures_per_message_in_a_batch():
    # First message always fails to write; second succeeds.
    class _SelectiveDynamo(FakeDynamoClient):
        def put_item(self, *, TableName, Item, **kwargs):
            if Item["SK"]["S"].startswith("REC#") and Item["sessionId"]["S"] == "bad":
                raise FakeDynamoError("write failure")
            return super().put_item(TableName=TableName, Item=Item, **kwargs)

    client = _SelectiveDynamo()
    handler = PersistenceLambda(_repo(client), sleeper=lambda _s: None)

    resp = handler.handle(_sqs_event(_draft("bad"), _draft("good")))

    assert resp == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
    # The healthy message was still persisted.
    page = _repo(client).get_session_history("good")
    assert is_ok(page) and len(page.value.records) == 1
