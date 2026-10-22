"""Cognito JWT parsing and RBAC helpers.

The Cognito JWT authorizer on API Gateway validates the token signature and
expiry before a Lambda runs, so the claims reaching these helpers are already
authenticated. These helpers translate those claims into a typed
:class:`Principal` and apply role-based access control (RBAC):

* a **Customer** is identified by the Cognito ``sub`` claim and may act only on
  resources they own (Req 5.4);
* an **analyst** must carry the analyst group/claim to run date-range queries
  (Req 4.5).

Authorization decisions fail closed and are returned as typed outcomes
(``Ok``/``Err(ACCESS_DENIED)``) so callers check them before reading any records
and return zero records on denial.
"""

from __future__ import annotations

from .principal import (
    ANALYST_GROUP,
    Principal,
    Role,
    authorize_analyst,
    authorize_session_owner,
    parse_principal,
)

__all__ = [
    "ANALYST_GROUP",
    "Principal",
    "Role",
    "authorize_analyst",
    "authorize_session_owner",
    "parse_principal",
]
