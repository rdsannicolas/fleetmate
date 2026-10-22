"""Property test for owner-scoped prior-interaction selection (Task 28.4).

Feature: vehicle-diagnostic-assistant, Property 29: Prior-interaction selection
excludes the current session and picks the latest.

This exercises the *real* selection logic in the ``FleetMateDiagnostics``
repository's ``query_most_recent_prior(customer_id, current_session_id)`` over
the in-memory ``FakeDynamoClient`` -- no ``boto3``, no AWS. Owner-attributed
records are written via ``append_record(..., customer_id=..., created_at=...,
record_id=...)`` so they carry the GSI2 keys that back the lookup.

The property: among a single owner's records, the resolved Prior Interaction is
the latest by the tuple ``(createdAt, recordId)`` (string comparison, matching
``GSI2SK = "{createdAt}#{recordId}"`` descending) *restricted to* records whose
``sessionId`` differs from the current session; a current-session record is
never returned; and when no out-of-session record exists the result is ``None``
(Req 13.1, 13.3, 13.5).

Generators deliberately emit duplicate ``createdAt`` values so the ``recordId``
tie-break is exercised, and keep ``recordId`` unique within each generated set
so no item overwrites another (the store is keyed by PK+SK, i.e. effectively
``(createdAt, recordId)`` within a session). The query is called with a ``limit``
large enough to cover the whole generated set so the property holds for
arbitrary inputs rather than being coupled to the default-2 skip heuristic.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from diagnostics.models import DiagnosticResult
from diagnostics.repositories import FleetMateDiagnostics

from .fake_dynamodb import FakeDynamoClient

TABLE = "FleetMateDiagnostics-test"
CUSTOMER_ID = "cust-1"

# A small fixed pool of session ids so records collide into/out of the chosen
# current session, and a small pool of timestamps so duplicate ``createdAt``
# values are reached (exercising the ``recordId`` tie-break).
SESSION_IDS = ["s0", "s1", "s2", "s3"]
TIMESTAMPS = [
    "2024-01-01T00:00:00.000Z",
    "2024-01-01T00:00:00.001Z",
    "2024-01-02T12:30:00.500Z",
    "2024-03-15T08:00:00.000Z",
]


def _success_result() -> DiagnosticResult:
    return DiagnosticResult(
        probableIssue="Weak Battery",
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


# A record draw: (sessionId, createdAt). The recordId is assigned by the test
# from the draw's index so it is unique within the set but still participates in
# the (createdAt, recordId) tie-break via ordering comparison.
_record = st.tuples(st.sampled_from(SESSION_IDS), st.sampled_from(TIMESTAMPS))


@settings(max_examples=200)
@given(
    records=st.lists(_record, min_size=0, max_size=12),
    current_session_id=st.sampled_from(SESSION_IDS),
)
def test_prior_interaction_selection_excludes_current_and_picks_latest(
    records: list[tuple[str, str]],
    current_session_id: str,
) -> None:
    # Feature: vehicle-diagnostic-assistant, Property 29: Prior-interaction
    # selection excludes the current session and picks the latest.
    client = FakeDynamoClient()
    repo = FleetMateDiagnostics(client, TABLE)

    # Assign unique recordIds. Zero-padding keeps them lexically sortable so the
    # independently-computed expected winner's tie-break matches GSI2SK order.
    owned: list[tuple[str, str, str]] = []  # (sessionId, createdAt, recordId)
    for index, (session_id, created_at) in enumerate(records):
        record_id = f"r{index:04d}"
        out = repo.append_record(
            session_id,
            "symptom",
            _success_result(),
            customer_id=CUSTOMER_ID,
            created_at=created_at,
            record_id=record_id,
        )
        # append_record returns Ok; unwrap defensively.
        owned.append((session_id, created_at, out.value.recordId))

    # Independently compute the expected winner: the record with the greatest
    # (createdAt, recordId) among those whose session differs from the current
    # one. ``None`` when no such record exists.
    prior = [
        (created_at, record_id)
        for (session_id, created_at, record_id) in owned
        if session_id != current_session_id
    ]
    expected = max(prior, default=None)

    # Use a limit large enough to cover the whole generated set so selection is
    # not coupled to the default-2 skip heuristic.
    got = repo.query_most_recent_prior(
        CUSTOMER_ID, current_session_id, limit=len(owned) + 1
    )

    if expected is None:
        assert got is None
        return

    expected_created_at, expected_record_id = expected
    assert got is not None
    # The resolved record is the latest out-of-session record by (createdAt, recordId).
    assert got.recordId == expected_record_id
    assert got.createdAt == expected_created_at
    # A current-session record is never returned.
    assert got.sessionId != current_session_id
    assert got.customerId == CUSTOMER_ID
