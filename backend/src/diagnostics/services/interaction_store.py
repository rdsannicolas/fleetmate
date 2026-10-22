"""The Interaction_Store read side: authorized retrieval of Interaction_Records.

This service is the synchronous *read* half of the Interaction_Store (design
"Interaction_Store"): it serves analyst date-range queries (Req 4.4-4.6) and
Customer paginated Session history (Req 5.1-5.5). The asynchronous *write* half
(event emission + persistence Lambda) lives in ``diagnostics/events/``.

The repository (:class:`~diagnostics.repositories.FleetMateDiagnostics`) is the
authless data seam -- it reads whatever it is asked for. Authorization is this
service's responsibility and is enforced **before any Interaction_Record is
read**, so the store *fails closed*: on an authorization denial it returns
``Err(ACCESS_DENIED)`` carrying zero records, regardless of the underlying data
(design "Authorization Model (Cognito + RBAC)"; Req 4.5, 5.4).

Two retrieval paths:

* :meth:`InteractionStore.query_by_date_range` -- analyst-only. The analyst RBAC
  check runs first; an inverted range is then rejected with ``INVALID_RANGE``;
  otherwise the inclusive in-range records are returned ascending by creation
  timestamp (Req 4.4, 4.5, 4.6).
* :meth:`InteractionStore.get_session_history` -- Customer owner-only. The owning
  Session is resolved first so an unknown Session maps to ``NOT_FOUND`` (Req 5.3)
  and a non-owner to ``ACCESS_DENIED`` (Req 5.4) *before* any history is read; a
  known-but-empty Session yields an empty page (Req 5.2); results are ascending
  and paginated at 50 per page (Req 5.1, 5.5).
* :meth:`InteractionStore.get_most_recent_prior_interaction` -- Customer
  owner-only. Owner-scoping is *structural*: the GSI2 lookup is partitioned by
  the owning Customer (``GSI2PK = CUSTOMER#{customerId}``), so the authenticated
  ``customerId`` is the partition and another Customer's records can never be
  read (Req 13.4). It fails closed on a blank ``customerId`` or
  ``currentSessionId`` (no read) and returns the single newest record outside
  the Current_Session, or ``None`` when the Customer owns no such record
  (Req 13.1, 13.3, 13.5, 13.6). Unlike the other two paths it returns
  ``InteractionRecord | None`` directly rather than an ``Outcome``.

Design constraints honoured here:

* **Repository-only dependency.** All data access flows through the repository;
  this module never imports Boto3 (design "Layering and Dependency Direction").
* **Typed outcomes.** Boundary failures are returned as
  :class:`~shared.outcomes.Err` values (``ACCESS_DENIED``, ``INVALID_RANGE``,
  ``NOT_FOUND``), never raised across the boundary.
"""

from __future__ import annotations

from typing import Optional

from diagnostics.models import InteractionRecord, Page
from diagnostics.repositories import FleetMateDiagnostics
from shared.auth import Principal, authorize_analyst, authorize_session_owner
from shared.outcomes import Err, Ok, Outcome

__all__ = ["InteractionStore"]


class InteractionStore:
    """Authorized, read-only access to persisted Interaction_Records.

    Depends solely on :class:`FleetMateDiagnostics`; authorization is applied in
    this layer so the repository stays authless and callers can never bypass the
    RBAC/ownership gate.
    """

    def __init__(self, repository: FleetMateDiagnostics) -> None:
        self._repo = repository

    # -- Analyst date-range query -----------------------------------------

    def query_by_date_range(
        self, principal: Principal, start: str, end: str
    ) -> Outcome[list[InteractionRecord]]:
        """Return inclusive in-range Interaction_Records ascending, analyst-only.

        Order of checks is deliberate and fail-closed:

        1. **Analyst RBAC** (Req 4.5) -- a principal lacking the analyst role is
           rejected with ``ACCESS_DENIED`` before any record is read, so denial
           returns zero records regardless of the stored data.
        2. **Range validity** (Req 4.6) -- a start later than the end is rejected
           with ``INVALID_RANGE`` and no records.
        3. **Read** (Req 4.4) -- otherwise every record whose ``createdAt`` is
           ``>= start`` and ``<= end`` is returned ascending by creation
           timestamp.
        """

        authorized = authorize_analyst(principal)
        if isinstance(authorized, Err):
            # Fail closed: no repository read happens on denial (Req 4.5).
            return authorized

        # The repository also rejects inverted ranges, but we never reach the
        # read for an unauthorized caller because the RBAC gate ran first.
        return self._repo.query_by_date_range(start, end)

    # -- Customer session history -----------------------------------------

    def get_session_history(
        self,
        principal: Principal,
        session_id: str,
        page_token: Optional[str] = None,
    ) -> Outcome[Page]:
        """Return one ascending page (<=50) of a Session's history, owner-only.

        Order of checks is deliberate and fail-closed:

        1. **Resolve the owning Session** via :meth:`FleetMateDiagnostics.get_session`.
           An unknown Session maps to ``NOT_FOUND`` (Req 5.3) and no history is
           read.
        2. **Owner check** (Req 5.4) -- the Session's ``customerId`` must match
           the principal's ``user_id``; a non-owner is rejected with
           ``ACCESS_DENIED`` *before* any Interaction_Record is read, so denial
           returns zero records.
        3. **Read** (Req 5.1, 5.2, 5.5) -- otherwise the records are returned
           ascending by creation timestamp, paginated at 50 per page; a
           known-but-empty Session yields an empty page (not an error).

        Resolving the Session reads only its metadata item, never any
        Interaction_Record, so the "no records before authorization" guarantee
        holds.
        """

        # Resolve ownership first. get_session reads only the SESSION#..#META
        # item, never interaction records, so this precedes any history read.
        found = self._repo.get_session(session_id)
        if isinstance(found, Err):
            # Unknown session -> NOT_FOUND (Req 5.3), no records.
            return found

        owner_check = authorize_session_owner(principal, found.value.customerId)
        if isinstance(owner_check, Err):
            # Fail closed: non-owner gets ACCESS_DENIED with zero records (Req 5.4).
            return owner_check

        # Authorized: read one ascending page, capped at 50 (Req 5.1, 5.2, 5.5).
        return self._repo.get_session_history(session_id, page_token)

    # -- Customer most-recent prior interaction ---------------------------

    def get_most_recent_prior_interaction(
        self, customerId: str, currentSessionId: str
    ) -> InteractionRecord | None:
        """Return the Customer's most-recent InteractionRecord outside the session.

        Resolves the Most_Recent_Prior_Interaction (Req 13.1, 13.3): the single
        record owned by ``customerId`` whose ``sessionId`` differs from
        ``currentSessionId`` with the latest ``createdAt``, breaking ties on the
        greater ``recordId``. Returns ``None`` when the Customer owns no such
        record (Req 13.5).

        Unlike the other read methods this returns ``InteractionRecord | None``
        directly (per the task's stated signature and the handler's
        ``{ record | null }`` response), rather than an :class:`Outcome`.

        Owner-scoping is **structural and fails closed** (Req 13.4): the GSI2
        lookup is partitioned by the owning Customer
        (``GSI2PK = CUSTOMER#{customerId}``), so passing the authenticated
        ``customerId`` as the partition is itself the authorization -- the method
        can never read another Customer's partition. A blank/empty/whitespace-only
        ``customerId`` is therefore treated as a closed failure: no read happens
        and ``None`` is returned.

        A non-blank ``currentSessionId`` is required (Req 13.6). When it is
        blank/empty/whitespace-only this method performs no read and returns
        ``None``; surfacing the user-facing 400 is the handler's responsibility,
        but the service stays safe and never reads on a blank session id.
        """

        # Fail closed before any read: a blank owner id must never fall through
        # to a repository query (which would otherwise read an empty/unintended
        # partition) (Req 13.4).
        if not customerId or not customerId.strip():
            return None

        # Require a non-blank current session id; do not read on a blank id
        # (Req 13.6). The handler layer maps this to a user-facing 400.
        if not currentSessionId or not currentSessionId.strip():
            return None

        # Owner-scoped by construction via the GSI2 partition; delegate and
        # return the repository's direct InteractionRecord | None result
        # (Req 13.1, 13.3, 13.5).
        return self._repo.query_most_recent_prior(customerId, currentSessionId)
