"""Principal resolution and RBAC checks from Cognito JWT claims.

Cognito places the authenticated user id in the ``sub`` claim and group
memberships in ``cognito:groups`` (a list, or occasionally a comma/space
separated string in some token configurations). :func:`parse_principal` turns a
claims mapping into a typed :class:`Principal`; the ``authorize_*`` helpers apply
RBAC and return typed outcomes so callers never leak records on denial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from shared.outcomes import Err, Ok, Outcome

__all__ = [
    "ANALYST_GROUP",
    "Role",
    "Principal",
    "parse_principal",
    "authorize_analyst",
    "authorize_session_owner",
]

# The Cognito group that grants the analyst role (date-range query access).
ANALYST_GROUP = "analysts"

# Claim keys Cognito uses for the subject and group membership.
_SUB_CLAIM = "sub"
_GROUPS_CLAIM = "cognito:groups"


class Role(str, Enum):
    """The RBAC roles FleetMate distinguishes.

    ``CUSTOMER`` is the default role for any authenticated subject; ``ANALYST`` is
    granted only when the analyst group is present in the token. Inheriting from
    ``str`` keeps the value JSON-serializable.
    """

    CUSTOMER = "CUSTOMER"
    ANALYST = "ANALYST"


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller resolved from validated Cognito JWT claims.

    ``user_id`` is the Cognito ``sub`` claim and is the ownership key for Customer
    resources (Req 5.4). ``groups`` carries the normalized Cognito group set;
    ``role`` is :data:`Role.ANALYST` when the analyst group is present, otherwise
    :data:`Role.CUSTOMER`.
    """

    user_id: str
    role: Role
    groups: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_analyst(self) -> bool:
        """``True`` when this principal holds the analyst role."""

        return self.role is Role.ANALYST


def _normalize_groups(raw: Any) -> frozenset[str]:
    """Coerce the ``cognito:groups`` claim into a set of group names.

    Cognito usually supplies a list, but some authorizer/token configurations
    flatten it to a comma- or space-separated string. Both shapes are handled;
    anything else yields an empty set.
    """

    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(str(g).strip() for g in raw if str(g).strip())
    if isinstance(raw, str):
        # Split on commas and whitespace to cover both encodings.
        tokens = raw.replace(",", " ").split()
        return frozenset(tokens)
    return frozenset()


def parse_principal(claims: Mapping[str, Any] | None) -> Outcome[Principal]:
    """Resolve a :class:`Principal` from a Cognito JWT claims mapping.

    Returns ``Err(ACCESS_DENIED)`` when the claims are missing or carry no
    ``sub`` (an unauthenticated or malformed context), so callers fail closed.
    Otherwise returns ``Ok(Principal)`` with the role derived from group
    membership.
    """

    if not claims:
        return Err("ACCESS_DENIED", "Authentication claims are missing.")

    sub = claims.get(_SUB_CLAIM)
    if not isinstance(sub, str) or not sub.strip():
        return Err("ACCESS_DENIED", "Authentication subject is missing.")

    groups = _normalize_groups(claims.get(_GROUPS_CLAIM))
    role = Role.ANALYST if ANALYST_GROUP in groups else Role.CUSTOMER
    return Ok(Principal(user_id=sub, role=role, groups=groups))


def authorize_analyst(principal: Principal) -> Outcome[Principal]:
    """Permit analyst-only actions (date-range queries) (Req 4.5).

    Returns ``Ok(principal)`` when the principal holds the analyst role, else
    ``Err(ACCESS_DENIED)`` with no records implied by the caller.
    """

    if principal.is_analyst:
        return Ok(principal)
    return Err("ACCESS_DENIED", "Analyst role is required for this request.")


def authorize_session_owner(
    principal: Principal, owner_customer_id: str
) -> Outcome[Principal]:
    """Permit a Customer to act only on a Session they own (Req 5.4).

    Ownership holds when ``principal.user_id`` matches the session's
    ``customerId``. Returns ``Ok(principal)`` on a match, else
    ``Err(ACCESS_DENIED)`` so the caller returns zero records.
    """

    if principal.user_id == owner_customer_id:
        return Ok(principal)
    return Err("ACCESS_DENIED", "You are not authorized to access this session.")
