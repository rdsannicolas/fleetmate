"""Property test for the Most_Recent_Prior_Interaction empty-result case (Task 29.3).

Feature: vehicle-diagnostic-assistant, Property 31: An empty result is returned
when no record exists outside the current session.

Exercise :meth:`InteractionStore.get_most_recent_prior_interaction` over the
real :class:`FleetMateDiagnostics` repository backed by the in-memory
``FakeDynamoClient`` (no boto3/AWS). When every owned record belongs to the
Current_Session -- including the zero-record case -- the authenticated Customer
has no prior interaction, so the service returns an empty result containing no
Interaction_Record (``None``).

A complementary variant additionally seeds records for *other* Customers in
*other* sessions and asserts the authenticated Customer still gets ``None``:
none of the owner's records lie outside the Current_Session, so cross-owner data
cannot manufacture a prior interaction.

_Requirements: 13.5_
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from diagnostics.models import DiagnosticResult
from diagnostics.repositories import FleetMateDiagnostics
from diagnostics.services import InteractionStore

from .fake_dynamodb import FakeDynamoClient

TABLE = "FleetMateDiagnostics-test"

CUSTOMER_ID = "cust-1"
CURRENT_SESSION_ID = "s-current"

# A small pool of UTC-millisecond ISO 8601 timestamps. Duplicates are allowed so
# ties on createdAt are exercised; the repository breaks ties on recordId.
_TIMESTAMP_POOL = [
    "2024-01-01T00:00:00.000Z",
    "2024-01-01T00:00:00.001Z",
    "2024-01-02T12:30:45.123Z",
    "2024-03-15T08:00:00.500Z",
    "2024-06-30T23:59:59.999Z",
    "2024-12-31T00:00:00.000Z",
]

# Timestamps/sessions used only for the cross-owner noise in the second variant.
_OTHER_CUSTOMERS = ["cust-2", "cust-3", "cust-4"]
_OTHER_SESSIONS = ["s-alpha", "s-beta", "s-gamma"]


def _result() -> DiagnosticResult:
    return DiagnosticResult(
        probableIssue="Weak Battery",
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


def _store() -> tuple[InteractionStore, FleetMateDiagnostics]:
    repo = FleetMateDiagnostics(FakeDynamoClient(), TABLE)
    return InteractionStore(repo), repo


# Each element is the createdAt stamp for one owned record; the list length
# (0..12, including the empty case) is the record-set size.
_owner_stamps = st.lists(st.sampled_from(_TIMESTAMP_POOL), min_size=0, max_size=12)


@settings(max_examples=200)
@given(stamps=_owner_stamps)
def test_empty_result_when_all_owned_records_in_current_session(
    stamps: list[str],
) -> None:
    """All owned records sit in the Current_Session => empty result (``None``).

    Feature: vehicle-diagnostic-assistant, Property 31: An empty result is
    returned when no record exists outside the current session.

    _Validates: Requirements 13.5_
    """
    store, repo = _store()

    # Every generated record belongs to CUSTOMER_ID AND to CURRENT_SESSION_ID.
    # Unique, zero-padded recordIds keep ordering deterministic across ties.
    for i, stamp in enumerate(stamps):
        repo.append_record(
            CURRENT_SESSION_ID,
            "symptom",
            _result(),
            customer_id=CUSTOMER_ID,
            created_at=stamp,
            record_id=f"r{i:03d}",
        )

    # No record exists outside the current session -> empty result.
    assert (
        store.get_most_recent_prior_interaction(CUSTOMER_ID, CURRENT_SESSION_ID)
        is None
    )


@settings(max_examples=200)
@given(
    stamps=_owner_stamps,
    noise=st.lists(
        st.tuples(
            st.sampled_from(_OTHER_CUSTOMERS),
            st.sampled_from(_OTHER_SESSIONS),
            st.sampled_from(_TIMESTAMP_POOL),
        ),
        min_size=0,
        max_size=12,
    ),
)
def test_empty_result_despite_other_owners_outside_the_current_session(
    stamps: list[str], noise: list[tuple[str, str, str]]
) -> None:
    """Other Customers' records in other sessions cannot create a prior interaction.

    The authenticated owner's records all live in the Current_Session, so the
    owner has nothing outside it; records owned by *other* Customers in *other*
    sessions populate different GSI2 partitions and must never surface as the
    authenticated owner's prior interaction. The result stays empty (``None``).

    Feature: vehicle-diagnostic-assistant, Property 31: An empty result is
    returned when no record exists outside the current session.

    _Validates: Requirements 13.5_
    """
    store, repo = _store()

    for i, stamp in enumerate(stamps):
        repo.append_record(
            CURRENT_SESSION_ID,
            "symptom",
            _result(),
            customer_id=CUSTOMER_ID,
            created_at=stamp,
            record_id=f"r{i:03d}",
        )

    # Cross-owner noise: records for OTHER customers in OTHER sessions. These
    # land in different GSI2 partitions (CUSTOMER#cust-2, ...), never the
    # authenticated owner's partition.
    for j, (other_customer, other_session, stamp) in enumerate(noise):
        repo.append_record(
            other_session,
            "symptom",
            _result(),
            customer_id=other_customer,
            created_at=stamp,
            record_id=f"n{j:03d}",
        )

    assert (
        store.get_most_recent_prior_interaction(CUSTOMER_ID, CURRENT_SESSION_ID)
        is None
    )
