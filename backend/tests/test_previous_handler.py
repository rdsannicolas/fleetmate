"""Unit tests for the ``GET /diagnostics/previous`` handler shape & owner
resolution (Task 30.3).

These tests complement the ``previous`` handler section in
``tests/test_handlers.py`` (Task 30.1). Rather than re-covering the same ground,
they pin down the parts Task 30.3 calls out explicitly:

* the exact 200 body **key set** -- ``{ record, timestamp }`` for a resolved
  record and ``{ record: null, timestamp }`` when no prior interaction exists,
  with ``record`` serialized in the same wire shape the history endpoint uses
  (``recordId, sessionId, symptomDescription, result{...}, createdAt``);
* the exact 400 body shape ``{ code: "MISSING_CURRENT_SESSION", suggestion,
  timestamp }`` for an omitted/blank ``currentSessionId``, returned *before* any
  store read (verified with a spy store that raises if read);
* owner resolution taken **only** from the token ``sub`` -- a client-supplied
  ``customerId``/``customer_id`` in the query is ignored.

The real :class:`InteractionStore` runs over the in-memory ``FakeDynamoClient``;
no AWS access and no ``boto3`` import are involved.

_Requirements: 13.2, 13.4, 13.5, 13.6_
"""

from __future__ import annotations

import json
import re

import pytest

from diagnostics.models import DiagnosticResult, Session, SessionStatus
from diagnostics.repositories import FleetMateDiagnostics
from diagnostics.services import InteractionStore
from handlers import previous as previous_handler
from handlers import _dependencies

from .fake_dynamodb import FakeDynamoClient

TABLE = "FleetMateDiagnostics-test"
_ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dependencies():
    """Each test starts and ends with a clean dependency seam."""

    _dependencies.reset_dependencies()
    yield
    _dependencies.reset_dependencies()


def _store_with_repo() -> FleetMateDiagnostics:
    client = FakeDynamoClient()
    repo = FleetMateDiagnostics(client, TABLE)
    _dependencies.set_interaction_store(InteractionStore(repo))
    return repo


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


def _append_owned(repo: FleetMateDiagnostics, session_id, customer_id, stamp, rid) -> None:
    """Append an owner-attributed record so GSI2 owner keys exist for the lookup."""

    repo.append_record(
        session_id,
        f"symptom {rid}",
        _success_result(),
        customer_id=customer_id,
        created_at=stamp,
        record_id=rid,
    )


def _previous_event(current_session_id, user_id: str = "cust-1", extra: dict | None = None) -> dict:
    """Build an API Gateway proxy event for the previous handler.

    ``extra`` merges additional query-string parameters (used to inject a spoofed
    client-supplied owner id that the handler must ignore).
    """

    params: dict = {}
    if current_session_id is not None:
        params["currentSessionId"] = current_session_id
    if extra:
        params.update(extra)
    return {
        "requestContext": {"authorizer": {"claims": {"sub": user_id}}},
        "queryStringParameters": params or None,
    }


class _SpyStore:
    """An InteractionStore stand-in that records (and can forbid) reads.

    Used to prove the 400 ``MISSING_CURRENT_SESSION`` path short-circuits before
    any store read: ``forbid=True`` turns a read into a hard failure.
    """

    def __init__(self, inner: InteractionStore, *, forbid: bool = False) -> None:
        self._inner = inner
        self._forbid = forbid
        self.reads = 0

    def get_most_recent_prior_interaction(self, customer_id, current_session_id):
        self.reads += 1
        if self._forbid:
            raise AssertionError(
                "store was read on the missing-currentSessionId path"
            )
        return self._inner.get_most_recent_prior_interaction(
            customer_id, current_session_id
        )


# ---------------------------------------------------------------------------
# 200 -- resolved record: exact { record, timestamp } in the history wire shape
# ---------------------------------------------------------------------------


def test_resolved_record_body_has_exactly_record_and_timestamp_in_history_shape():
    repo = _store_with_repo()
    _seed_session(repo, "s-prior", "cust-1")
    _seed_session(repo, "s-current", "cust-1")
    # A record in the current session (must be excluded) and an older one outside
    # it (the expected "previous" interaction).
    _append_owned(repo, "s-prior", "cust-1", "2024-01-01T00:00:00.000Z", "r-prior")
    _append_owned(repo, "s-current", "cust-1", "2024-01-02T00:00:00.000Z", "r-current")

    resp = previous_handler.handler(_previous_event("s-current"))

    assert resp["statusCode"] == 200
    body = _body(resp)
    # The 200 body is EXACTLY { record, timestamp } -- no extra keys.
    assert set(body.keys()) == {"record", "timestamp"}
    assert _ISO_MS.match(body["timestamp"])

    record = body["record"]
    # The record uses the SAME wire shape as handlers/history.py's _record_body:
    # recordId, sessionId, symptomDescription, nested result{...}, createdAt.
    assert set(record.keys()) == {
        "recordId",
        "sessionId",
        "symptomDescription",
        "result",
        "createdAt",
    }
    # The newest record OUTSIDE the current session is returned (not r-current).
    assert record["recordId"] == "r-prior"
    assert record["sessionId"] == "s-prior"
    assert record["symptomDescription"] == "symptom r-prior"
    assert record["createdAt"] == "2024-01-01T00:00:00.000Z"
    # The nested result mirrors the history serialization exactly.
    assert record["result"] == {
        "needsMoreDetail": False,
        "probableIssue": "Weak Battery",
        "repairRecommendation": "Replace battery",
        "estimatedCost": "INR 4,000 - 8,000",
        "confidenceScore": 0.89,
    }


# ---------------------------------------------------------------------------
# 200 -- no prior interaction: exact { record: null, timestamp }
# ---------------------------------------------------------------------------


def test_no_prior_interaction_returns_exactly_null_record_and_timestamp():
    repo = _store_with_repo()
    _seed_session(repo, "s-current", "cust-1")
    # The owner's only record lives in the current session, so nothing is prior.
    _append_owned(repo, "s-current", "cust-1", "2024-01-02T00:00:00.000Z", "r-current")

    resp = previous_handler.handler(_previous_event("s-current"))

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert set(body.keys()) == {"record", "timestamp"}
    assert body["record"] is None
    assert _ISO_MS.match(body["timestamp"])


def test_owner_with_no_records_returns_null_record():
    _store_with_repo()

    resp = previous_handler.handler(_previous_event("s-current"))

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["record"] is None
    assert _ISO_MS.match(body["timestamp"])


# ---------------------------------------------------------------------------
# 400 -- missing/blank currentSessionId: exact body, no store read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current_session_id", [None, "", "   ", "\t\n"])
def test_missing_or_blank_current_session_returns_400_body_without_reading_store(
    current_session_id,
):
    repo = _store_with_repo()
    # Replace the injected store with a spy that FAILS if read -- proving the 400
    # short-circuits before any Interaction_Store access (Req 13.6).
    spy = _SpyStore(_dependencies.get_interaction_store(), forbid=True)
    _dependencies.set_interaction_store(spy)
    # Seed data that WOULD resolve if a read occurred, so the assertion is real.
    _seed_session(repo, "s-prior", "cust-1")
    _append_owned(repo, "s-prior", "cust-1", "2024-01-01T00:00:00.000Z", "r-prior")

    resp = previous_handler.handler(_previous_event(current_session_id))

    assert resp["statusCode"] == 400
    body = _body(resp)
    # The body is EXACTLY { code, suggestion, timestamp }.
    assert set(body.keys()) == {"code", "suggestion", "timestamp"}
    assert body["code"] == "MISSING_CURRENT_SESSION"
    assert isinstance(body["suggestion"], str) and body["suggestion"].strip()
    assert _ISO_MS.match(body["timestamp"])
    # No read happened (the spy would have raised; reads stays at zero).
    assert spy.reads == 0


# ---------------------------------------------------------------------------
# Owner resolved ONLY from the token sub (Req 13.2, 13.4)
# ---------------------------------------------------------------------------


def test_customer_id_resolved_only_from_token_sub_spoofed_query_id_ignored():
    repo = _store_with_repo()
    _seed_session(repo, "s-prior", "owner")
    _seed_session(repo, "s-current", "owner")
    _append_owned(repo, "s-prior", "owner", "2024-01-01T00:00:00.000Z", "r-owner")

    # An intruder authenticates (token sub = "intruder") but supplies the owner's
    # id in the query. A handler that trusted the query would leak the owner's
    # record; the handler must resolve the owner only from claims.sub.
    intruder_resp = previous_handler.handler(
        _previous_event(
            "s-current",
            user_id="intruder",
            extra={"customerId": "owner", "customer_id": "owner"},
        )
    )

    assert intruder_resp["statusCode"] == 200
    # The spoofed id is ignored; the intruder owns nothing.
    assert _body(intruder_resp)["record"] is None

    # The real owner, resolved from their OWN token sub, sees their record -- even
    # with a spoofed id pointing at a different (nonexistent) customer.
    owner_resp = previous_handler.handler(
        _previous_event(
            "s-current",
            user_id="owner",
            extra={"customerId": "someone-else", "customer_id": "someone-else"},
        )
    )

    assert owner_resp["statusCode"] == 200
    assert _body(owner_resp)["record"]["recordId"] == "r-owner"


def test_different_token_sub_owning_nothing_returns_null_record():
    repo = _store_with_repo()
    _seed_session(repo, "s-prior", "owner")
    _append_owned(repo, "s-prior", "owner", "2024-01-01T00:00:00.000Z", "r-owner")

    # A different authenticated customer who owns no record gets a null record.
    resp = previous_handler.handler(_previous_event("s-current", user_id="other"))

    assert resp["statusCode"] == 200
    assert _body(resp)["record"] is None


# ---------------------------------------------------------------------------
# Missing claims -> 403 (fail-closed, reaffirms owner resolution is mandatory)
# ---------------------------------------------------------------------------


def test_missing_claims_returns_403_access_denied():
    _store_with_repo()
    event = {"queryStringParameters": {"currentSessionId": "s-current"}}

    resp = previous_handler.handler(event)

    assert resp["statusCode"] == 403
    assert _body(resp)["code"] == "ACCESS_DENIED"
