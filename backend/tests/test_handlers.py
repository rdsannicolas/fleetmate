"""Unit tests for the thin Lambda handlers' outcome -> HTTP mapping (Task 13.1).

These tests exercise the three handlers (``analyze``, ``history``,
``repair_estimates``) with their dependencies injected as fakes -- a fake
diagnostics service returning each :class:`SubmissionResult` shape, and a real
:class:`InteractionStore` over the in-memory ``FakeDynamoClient``. No AWS access
is involved, so the mapping of typed outcomes to HTTP status codes and the
``{ code, suggestion, timestamp }`` error-body contract are verified directly.

_Requirements: 1.1, 1.5, 2.3, 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5_
"""

from __future__ import annotations

import json
import re

import pytest

from diagnostics.models import DiagnosticResult, Session, SessionStatus
from diagnostics.repositories import HISTORY_PAGE_SIZE, FleetMateDiagnostics
from diagnostics.services import (
    DiagnosticResponse,
    DiagnosticsService,
    Failure,
    InteractionStore,
    Rejection,
)
from handlers import analyze as analyze_handler
from handlers import history as history_handler
from handlers import previous as previous_handler
from handlers import repair_estimates as repair_estimates_handler
from handlers import _dependencies

from .fake_dynamodb import FakeDynamoClient

TABLE = "FleetMateDiagnostics-test"
_ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------
# Fakes & helpers
# ---------------------------------------------------------------------------


class FakeDiagnosticsService:
    """A stand-in for :class:`DiagnosticsService` that returns a canned result.

    Records the arguments ``analyze`` was called with so tests can assert the
    handler passed the Customer sub as ``customer_id`` and forwarded the
    correlation id.
    """

    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[dict] = []

    def analyze(self, customer_id, symptom_text, *, session_id=None, vehicle=None, correlation_id=None):
        self.calls.append(
            {
                "customer_id": customer_id,
                "symptom_text": symptom_text,
                "session_id": session_id,
                "vehicle": vehicle,
                "correlation_id": correlation_id,
            }
        )
        return self._result


@pytest.fixture(autouse=True)
def _reset_dependencies():
    """Ensure each test starts and ends with a clean dependency seam."""

    _dependencies.reset_dependencies()
    yield
    _dependencies.reset_dependencies()


def _customer_event(body: dict | None = None, user_id: str = "cust-1") -> dict:
    return {
        "requestContext": {"authorizer": {"claims": {"sub": user_id}}},
        "headers": {"X-Correlation-Id": "corr-123"},
        "body": json.dumps(body) if body is not None else None,
    }


def _analyst_claims(user_id: str = "analyst-1") -> dict:
    return {"sub": user_id, "cognito:groups": ["analysts"]}


def _body(response: dict) -> dict:
    return json.loads(response["body"])


def _success_result() -> DiagnosticResult:
    return DiagnosticResult(
        needsMoreDetail=False,
        probableIssue="Weak Battery",
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


# ---------------------------------------------------------------------------
# analyze handler
# ---------------------------------------------------------------------------


def test_analyze_success_maps_to_200_with_diagnosis_fields():
    result = DiagnosticResponse(sessionId="s1", result=_success_result(), escalated=False)
    fake = FakeDiagnosticsService(result)
    _dependencies.set_diagnostics_service(fake)

    resp = analyze_handler.handler(_customer_event({"symptomText": "car wont start"}))

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["sessionId"] == "s1"
    assert body["probableIssue"] == "Weak Battery"
    assert body["repairRecommendation"] == "Replace battery"
    assert body["estimatedCost"] == "INR 4,000 - 8,000"
    assert body["confidenceScore"] == 0.89
    assert body["escalated"] is False
    assert _ISO_MS.match(body["timestamp"])
    # The Customer sub is the customer_id and the correlation id is forwarded.
    assert fake.calls[0]["customer_id"] == "cust-1"
    assert fake.calls[0]["correlation_id"] == "corr-123"


def test_analyze_insufficient_info_maps_to_200_with_needs_more_detail():
    result = DiagnosticResponse(
        sessionId="s1",
        result=DiagnosticResult(needsMoreDetail=True, message="Tell me more."),
        escalated=True,
    )
    _dependencies.set_diagnostics_service(FakeDiagnosticsService(result))

    resp = analyze_handler.handler(_customer_event({"symptomText": "hmm"}))

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["needsMoreDetail"] is True
    assert body["message"] == "Tell me more."
    assert "probableIssue" not in body
    assert body["escalated"] is True


@pytest.mark.parametrize(
    "reason,suggestion",
    [
        ("EMPTY", "Describe the symptom."),
        ("TOO_LONG", "Symptom is too long."),
        ("SESSION_CLOSED", "Session closed."),
        ("LIMIT_REACHED", "Limit reached."),
    ],
)
def test_analyze_rejection_maps_to_422(reason, suggestion):
    _dependencies.set_diagnostics_service(
        FakeDiagnosticsService(Rejection(reason=reason, suggestion=suggestion))
    )

    resp = analyze_handler.handler(_customer_event({"symptomText": "x"}))

    assert resp["statusCode"] == 422
    body = _body(resp)
    assert body == {"code": reason, "suggestion": suggestion, "timestamp": body["timestamp"]}
    assert _ISO_MS.match(body["timestamp"])


@pytest.mark.parametrize("reason", ["AI_TIMEOUT", "AI_ERROR"])
def test_analyze_failure_maps_to_503(reason):
    _dependencies.set_diagnostics_service(
        FakeDiagnosticsService(Failure(reason=reason, suggestion="Try again."))
    )

    resp = analyze_handler.handler(_customer_event({"symptomText": "x"}))

    assert resp["statusCode"] == 503
    body = _body(resp)
    assert body["code"] == reason
    assert body["suggestion"] == "Try again."
    assert _ISO_MS.match(body["timestamp"])


def test_analyze_missing_claims_maps_to_403():
    _dependencies.set_diagnostics_service(FakeDiagnosticsService(None))
    event = {"body": json.dumps({"symptomText": "x"})}  # no authorizer claims

    resp = analyze_handler.handler(event)

    assert resp["statusCode"] == 403
    assert _body(resp)["code"] == "ACCESS_DENIED"


def test_analyze_malformed_body_maps_to_400():
    _dependencies.set_diagnostics_service(FakeDiagnosticsService(None))
    event = {
        "requestContext": {"authorizer": {"claims": {"sub": "cust-1"}}},
        "body": "{not json",
    }

    resp = analyze_handler.handler(event)

    assert resp["statusCode"] == 400
    assert _body(resp)["code"] == "BAD_REQUEST"


def test_analyze_missing_symptom_text_passes_none_to_service():
    # A missing symptomText is passed as None so the service's shared validator
    # produces EMPTY -- validation stays in one place, not duplicated in the handler.
    fake = FakeDiagnosticsService(Rejection(reason="EMPTY", suggestion="Describe the symptom."))
    _dependencies.set_diagnostics_service(fake)

    resp = analyze_handler.handler(_customer_event({}))

    assert resp["statusCode"] == 422
    assert fake.calls[0]["symptom_text"] is None


def test_analyze_non_string_symptom_text_maps_to_400():
    _dependencies.set_diagnostics_service(FakeDiagnosticsService(None))

    resp = analyze_handler.handler(_customer_event({"symptomText": 123}))

    assert resp["statusCode"] == 400
    assert _body(resp)["code"] == "BAD_REQUEST"


def test_analyze_forwards_vehicle_and_session_id():
    fake = FakeDiagnosticsService(
        DiagnosticResponse(sessionId="s9", result=_success_result(), escalated=False)
    )
    _dependencies.set_diagnostics_service(fake)

    resp = analyze_handler.handler(
        _customer_event(
            {
                "symptomText": "noise",
                "sessionId": "s9",
                "vehicle": {"make": "Tata", "model": "Nexon", "year": 2021},
            }
        )
    )

    assert resp["statusCode"] == 200
    call = fake.calls[0]
    assert call["session_id"] == "s9"
    assert call["vehicle"].make == "Tata"
    assert call["vehicle"].model == "Nexon"
    assert call["vehicle"].year == 2021


def test_analyze_non_integer_year_maps_to_400():
    _dependencies.set_diagnostics_service(FakeDiagnosticsService(None))

    resp = analyze_handler.handler(
        _customer_event({"symptomText": "x", "vehicle": {"year": "2021"}})
    )

    assert resp["statusCode"] == 400


def test_analyze_unexpected_exception_maps_to_503_without_leaking():
    class Boom:
        def analyze(self, *a, **k):
            raise RuntimeError("secret internal detail / stack trace")

    _dependencies.set_diagnostics_service(Boom())

    resp = analyze_handler.handler(_customer_event({"symptomText": "x"}))

    assert resp["statusCode"] == 503
    body = _body(resp)
    assert "secret internal detail" not in resp["body"]
    assert body["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# history handler (real InteractionStore over the fake DynamoDB)
# ---------------------------------------------------------------------------


def _store_with_repo() -> FleetMateDiagnostics:
    client = FakeDynamoClient()
    repo = FleetMateDiagnostics(client, TABLE)
    _dependencies.set_interaction_store(InteractionStore(repo))
    return repo


def _seed_session(repo: FleetMateDiagnostics, session_id: str, customer_id: str) -> None:
    repo.put_session(
        Session(
            sessionId=session_id,
            customerId=customer_id,
            status=SessionStatus.ACTIVE,
            createdAt="2024-01-01T00:00:00.000Z",
            lastSubmissionAt="2024-01-01T00:00:00.000Z",
            interactionCount=0,
        )
    )


def _history_event(session_id: str | None, user_id: str = "cust-1", page_token: str | None = None) -> dict:
    params: dict = {}
    if session_id is not None:
        params["sessionId"] = session_id
    if page_token is not None:
        params["pageToken"] = page_token
    return {
        "requestContext": {"authorizer": {"claims": {"sub": user_id}}},
        "queryStringParameters": params or None,
    }


def test_history_owner_empty_session_maps_to_200_empty_records():
    repo = _store_with_repo()
    _seed_session(repo, "s1", "cust-1")

    resp = history_handler.handler(_history_event("s1"))

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["records"] == []
    assert "nextPageToken" not in body
    assert _ISO_MS.match(body["timestamp"])


def test_history_returns_records_ascending():
    repo = _store_with_repo()
    _seed_session(repo, "s1", "cust-1")
    for i in range(3):
        repo.append_record(
            "s1",
            f"symptom {i}",
            _success_result(),
            created_at=f"2024-01-01T00:00:0{i}.000Z",
        )

    resp = history_handler.handler(_history_event("s1"))

    body = _body(resp)
    assert [r["symptomDescription"] for r in body["records"]] == [
        "symptom 0",
        "symptom 1",
        "symptom 2",
    ]
    assert body["records"][0]["result"]["probableIssue"] == "Weak Battery"


def test_history_paginates_at_50_with_next_page_token():
    repo = _store_with_repo()
    _seed_session(repo, "s1", "cust-1")
    for i in range(HISTORY_PAGE_SIZE + 5):
        repo.append_record(
            "s1",
            f"symptom {i:03d}",
            _success_result(),
            created_at=f"2024-01-01T00:{i // 60:02d}:{i % 60:02d}.000Z",
        )

    resp = history_handler.handler(_history_event("s1"))

    body = _body(resp)
    assert len(body["records"]) == HISTORY_PAGE_SIZE
    assert "nextPageToken" in body


def test_history_unknown_session_maps_to_404():
    _store_with_repo()

    resp = history_handler.handler(_history_event("does-not-exist"))

    assert resp["statusCode"] == 404
    assert _body(resp)["code"] == "NOT_FOUND"


def test_history_non_owner_maps_to_403():
    repo = _store_with_repo()
    _seed_session(repo, "s1", "owner")

    resp = history_handler.handler(_history_event("s1", user_id="intruder"))

    assert resp["statusCode"] == 403
    assert _body(resp)["code"] == "ACCESS_DENIED"


def test_history_missing_session_id_maps_to_400():
    _store_with_repo()

    resp = history_handler.handler(_history_event(None))

    assert resp["statusCode"] == 400
    assert _body(resp)["code"] == "BAD_REQUEST"


def test_history_missing_claims_maps_to_403():
    _store_with_repo()
    event = {"queryStringParameters": {"sessionId": "s1"}}

    resp = history_handler.handler(event)

    assert resp["statusCode"] == 403
    assert _body(resp)["code"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# repair-estimates handler (analyst RBAC date-range query)
# ---------------------------------------------------------------------------


def _estimates_event(start: str | None, end: str | None, claims: dict | None = None) -> dict:
    params: dict = {}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return {
        "requestContext": {"authorizer": {"claims": claims if claims is not None else _analyst_claims()}},
        "queryStringParameters": params or None,
    }


def test_repair_estimates_analyst_in_range_maps_to_200_ascending():
    repo = _store_with_repo()
    _seed_session(repo, "s1", "cust-1")
    for i in range(3):
        repo.append_record(
            "s1",
            f"symptom {i}",
            _success_result(),
            created_at=f"2024-01-0{i + 1}T00:00:00.000Z",
        )

    resp = repair_estimates_handler.handler(
        _estimates_event("2024-01-01T00:00:00.000Z", "2024-01-03T00:00:00.000Z")
    )

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert len(body["records"]) == 3
    created = [r["createdAt"] for r in body["records"]]
    assert created == sorted(created)
    assert _ISO_MS.match(body["timestamp"])


def test_repair_estimates_non_analyst_maps_to_403():
    _store_with_repo()

    resp = repair_estimates_handler.handler(
        _estimates_event(
            "2024-01-01T00:00:00.000Z",
            "2024-01-03T00:00:00.000Z",
            claims={"sub": "cust-1"},  # no analyst group
        )
    )

    assert resp["statusCode"] == 403
    assert _body(resp)["code"] == "ACCESS_DENIED"


def test_repair_estimates_inverted_range_maps_to_400():
    _store_with_repo()

    resp = repair_estimates_handler.handler(
        _estimates_event("2024-02-01T00:00:00.000Z", "2024-01-01T00:00:00.000Z")
    )

    assert resp["statusCode"] == 400
    assert _body(resp)["code"] == "INVALID_RANGE"


def test_repair_estimates_missing_params_maps_to_400():
    _store_with_repo()

    resp = repair_estimates_handler.handler(_estimates_event(None, None))

    assert resp["statusCode"] == 400
    assert _body(resp)["code"] == "BAD_REQUEST"


def test_repair_estimates_missing_claims_maps_to_403():
    _store_with_repo()
    event = {"queryStringParameters": {"start": "a", "end": "b"}}

    resp = repair_estimates_handler.handler(event)

    assert resp["statusCode"] == 403
    assert _body(resp)["code"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# HTTP API (jwt.claims) authorizer shape
# ---------------------------------------------------------------------------


def test_history_supports_http_api_jwt_claims_shape():
    repo = _store_with_repo()
    _seed_session(repo, "s1", "cust-1")

    event = {
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "cust-1"}}}},
        "queryStringParameters": {"sessionId": "s1"},
    }
    resp = history_handler.handler(event)

    assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# previous handler (real InteractionStore over the fake DynamoDB) -- Task 30.1
# ---------------------------------------------------------------------------


class _CountingStore:
    """Wraps an InteractionStore to count prior-interaction reads.

    Lets a test assert the handler performs NO store read when currentSessionId
    is missing/blank (the 400 short-circuits before delegation).
    """

    def __init__(self, inner: InteractionStore) -> None:
        self._inner = inner
        self.reads = 0

    def get_most_recent_prior_interaction(self, customer_id, current_session_id):
        self.reads += 1
        return self._inner.get_most_recent_prior_interaction(
            customer_id, current_session_id
        )


def _append_owned(repo: FleetMateDiagnostics, session_id, customer_id, stamp, rid):
    """Append an owner-attributed record so GSI2 keys exist for the lookup."""
    repo.append_record(
        session_id,
        f"symptom {rid}",
        _success_result(),
        customer_id=customer_id,
        created_at=stamp,
        record_id=rid,
    )


def _previous_event(current_session_id, user_id: str = "cust-1", extra: dict | None = None) -> dict:
    params: dict = {}
    if current_session_id is not None:
        params["currentSessionId"] = current_session_id
    if extra:
        params.update(extra)
    return {
        "requestContext": {"authorizer": {"claims": {"sub": user_id}}},
        "queryStringParameters": params or None,
    }


def test_previous_resolved_record_maps_to_200_in_history_shape():
    repo = _store_with_repo()
    _seed_session(repo, "s-prior", "cust-1")
    _seed_session(repo, "s-current", "cust-1")
    _append_owned(repo, "s-prior", "cust-1", "2024-01-01T00:00:00.000Z", "r-prior")
    _append_owned(repo, "s-current", "cust-1", "2024-01-02T00:00:00.000Z", "r-current")

    resp = previous_handler.handler(_previous_event("s-current"))

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert _ISO_MS.match(body["timestamp"])
    record = body["record"]
    # Newest record OUTSIDE the current session, in the history serialization shape.
    assert record["recordId"] == "r-prior"
    assert record["sessionId"] == "s-prior"
    assert record["symptomDescription"] == "symptom r-prior"
    assert record["createdAt"] == "2024-01-01T00:00:00.000Z"
    assert record["result"]["probableIssue"] == "Weak Battery"
    assert record["result"]["repairRecommendation"] == "Replace battery"
    assert record["result"]["estimatedCost"] == "INR 4,000 - 8,000"
    assert record["result"]["confidenceScore"] == 0.89
    assert record["result"]["needsMoreDetail"] is False


def test_previous_no_prior_interaction_maps_to_200_with_null_record():
    repo = _store_with_repo()
    _seed_session(repo, "s-current", "cust-1")
    # Only a current-session record exists, so there is no prior interaction.
    _append_owned(repo, "s-current", "cust-1", "2024-01-02T00:00:00.000Z", "r-current")

    resp = previous_handler.handler(_previous_event("s-current"))

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["record"] is None
    assert _ISO_MS.match(body["timestamp"])


def test_previous_no_records_at_all_maps_to_200_with_null_record():
    _store_with_repo()

    resp = previous_handler.handler(_previous_event("s-current"))

    assert resp["statusCode"] == 200
    assert _body(resp)["record"] is None


@pytest.mark.parametrize("current_session_id", [None, "", "   ", "\t\n"])
def test_previous_missing_or_blank_session_maps_to_400_without_read(current_session_id):
    repo = _store_with_repo()
    # Wrap the injected store so we can assert no read happens on the 400 path.
    counting = _CountingStore(_dependencies.get_interaction_store())
    _dependencies.set_interaction_store(counting)
    _seed_session(repo, "s-prior", "cust-1")
    _append_owned(repo, "s-prior", "cust-1", "2024-01-01T00:00:00.000Z", "r-prior")

    resp = previous_handler.handler(_previous_event(current_session_id))

    assert resp["statusCode"] == 400
    body = _body(resp)
    assert body == {
        "code": "MISSING_CURRENT_SESSION",
        "suggestion": body["suggestion"],
        "timestamp": body["timestamp"],
    }
    assert _ISO_MS.match(body["timestamp"])
    # The 400 short-circuits BEFORE any store read.
    assert counting.reads == 0


def test_previous_missing_claims_maps_to_403():
    _store_with_repo()
    event = {"queryStringParameters": {"currentSessionId": "s-current"}}

    resp = previous_handler.handler(event)

    assert resp["statusCode"] == 403
    assert _body(resp)["code"] == "ACCESS_DENIED"


def test_previous_customer_id_comes_only_from_token_sub():
    repo = _store_with_repo()
    _seed_session(repo, "s-prior", "owner")
    _seed_session(repo, "s-current", "owner")
    _append_owned(repo, "s-prior", "owner", "2024-01-01T00:00:00.000Z", "r-owner")

    # A client supplies a customerId in the query that would, if trusted, point at
    # the owner's data -- but the authenticated sub is "intruder", who owns nothing.
    resp = previous_handler.handler(
        _previous_event(
            "s-current",
            user_id="intruder",
            extra={"customerId": "owner", "customer_id": "owner"},
        )
    )

    assert resp["statusCode"] == 200
    # The spoofed customerId is ignored; the intruder (token sub) owns no record.
    assert _body(resp)["record"] is None

    # And the real owner, resolved only from their own sub, does see the record.
    owner_resp = previous_handler.handler(
        _previous_event("s-current", user_id="owner")
    )
    assert owner_resp["statusCode"] == 200
    assert _body(owner_resp)["record"]["recordId"] == "r-owner"
