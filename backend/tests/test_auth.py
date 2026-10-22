"""Unit tests for Cognito JWT parsing and RBAC helpers (Task 2.2).

Assert RBAC resolves the Customer-owner vs analyst distinction correctly from
Cognito claims, and that authorization fails closed with ``ACCESS_DENIED`` and no
implied records on denial.

_Requirements: 4.5, 5.4_
"""

from __future__ import annotations

from shared.auth import (
    ANALYST_GROUP,
    Principal,
    Role,
    authorize_analyst,
    authorize_session_owner,
    parse_principal,
)
from shared.outcomes import Err, Ok, is_err, is_ok


def test_parse_principal_resolves_customer_by_default() -> None:
    """A subject with no analyst group resolves to the Customer role."""
    outcome = parse_principal({"sub": "user-123"})

    assert is_ok(outcome)
    assert isinstance(outcome, Ok)
    principal = outcome.value
    assert principal.user_id == "user-123"
    assert principal.role is Role.CUSTOMER
    assert principal.is_analyst is False


def test_parse_principal_resolves_analyst_from_group_list() -> None:
    """The analyst group in cognito:groups grants the analyst role."""
    outcome = parse_principal(
        {"sub": "analyst-1", "cognito:groups": ["other", ANALYST_GROUP]}
    )

    assert isinstance(outcome, Ok)
    assert outcome.value.role is Role.ANALYST
    assert outcome.value.is_analyst is True


def test_parse_principal_handles_string_group_claim() -> None:
    """A comma/space separated groups claim is normalized correctly."""
    outcome = parse_principal(
        {"sub": "analyst-2", "cognito:groups": f"other, {ANALYST_GROUP}"}
    )

    assert isinstance(outcome, Ok)
    assert outcome.value.is_analyst is True
    assert ANALYST_GROUP in outcome.value.groups


def test_parse_principal_denies_missing_claims() -> None:
    """Missing claims fail closed with ACCESS_DENIED."""
    outcome = parse_principal(None)
    assert is_err(outcome)
    assert isinstance(outcome, Err)
    assert outcome.code == "ACCESS_DENIED"


def test_parse_principal_denies_missing_sub() -> None:
    """A context without a usable subject is denied."""
    assert isinstance(parse_principal({}), Err)
    assert isinstance(parse_principal({"sub": "   "}), Err)
    assert isinstance(parse_principal({"sub": 123}), Err)


def test_authorize_analyst_allows_analyst_denies_customer() -> None:
    """Analyst role passes; Customer role is denied for analyst actions (Req 4.5)."""
    analyst = Principal("a1", Role.ANALYST, frozenset({ANALYST_GROUP}))
    customer = Principal("c1", Role.CUSTOMER)

    assert isinstance(authorize_analyst(analyst), Ok)

    denied = authorize_analyst(customer)
    assert isinstance(denied, Err)
    assert denied.code == "ACCESS_DENIED"


def test_authorize_session_owner_matches_sub() -> None:
    """A Customer may act only on a session whose customerId is their sub (Req 5.4)."""
    customer = Principal("owner-1", Role.CUSTOMER)

    assert isinstance(authorize_session_owner(customer, "owner-1"), Ok)

    denied = authorize_session_owner(customer, "someone-else")
    assert isinstance(denied, Err)
    assert denied.code == "ACCESS_DENIED"


def test_analyst_is_not_implicitly_a_session_owner() -> None:
    """Owner check is identity-based, independent of role (fails closed)."""
    analyst = Principal("a1", Role.ANALYST, frozenset({ANALYST_GROUP}))
    denied = authorize_session_owner(analyst, "customer-9")
    assert isinstance(denied, Err)
    assert denied.code == "ACCESS_DENIED"
