"""``GET /diagnostics/previous`` thin Lambda handler (Task 30.1).

The handler resolves the Cognito principal, reads the ``currentSessionId`` query
parameter, and delegates to
:meth:`InteractionStore.get_most_recent_prior_interaction`. The owning Customer
is taken **exclusively** from the validated token ``sub`` claim (the principal's
``user_id``) -- never from a query/header/body value -- so a client cannot read
another Customer's prior interaction. Owner-scoping and the newest-outside-session
selection live in the store/repository; the handler only extracts the request and
shapes the result into HTTP.

Request (Customer JWT, owner only)::

    GET /diagnostics/previous?currentSessionId=...

Responses (design response contract):

* ``200`` -- ``{ record: InteractionRecord | null, timestamp }``. The record is
  the Customer's most-recent InteractionRecord outside ``currentSessionId`` in the
  same serialization shape as the history endpoint (Req 13.1, 13.2); when the
  Customer owns no prior interaction the body is ``{ record: null, timestamp }``
  (Req 13.5).
* ``400`` -- ``MISSING_CURRENT_SESSION`` when ``currentSessionId`` is
  omitted/empty/whitespace-only, returned before any read (Req 13.6).
* ``403`` -- ``ACCESS_DENIED`` when the JWT claims are missing/invalid.

Every body carries a ``timestamp``; error bodies are ``{ code, suggestion,
timestamp }`` and never leak internal detail. :func:`safe_handler` guards the
entry point so unexpected exceptions become a generic ``503``.
"""

from __future__ import annotations

from typing import Any, Mapping

from diagnostics.models import InteractionRecord
from shared.outcomes import Err

from . import _http
from ._dependencies import get_interaction_store

__all__ = ["handler"]


@_http.safe_handler
def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for ``GET /diagnostics/previous``."""

    # 1. Resolve the Customer principal (fail-closed on missing claims). The
    #    customerId is the token sub and is never read from the request.
    principal_outcome = _http.resolve_principal(event)
    if isinstance(principal_outcome, Err):
        return _http.error_from(principal_outcome)
    customer_id = principal_outcome.value.user_id

    # 2. Require a non-blank currentSessionId before any read (Req 13.6). The task
    #    specifies the MISSING_CURRENT_SESSION code, so emit it directly rather
    #    than taking the generic BAD_REQUEST path.
    params = _http.query_params(event)
    current_session_id = (params.get("currentSessionId") or "").strip()
    if not current_session_id:
        return _http.error(
            400,
            "MISSING_CURRENT_SESSION",
            "currentSessionId is required.",
        )

    # 3. Delegate; owner-scoping is structural (the GSI2 partition is the token
    #    sub) and returns InteractionRecord | None directly (Req 13.1, 13.3).
    record = get_interaction_store().get_most_recent_prior_interaction(
        customer_id, current_session_id
    )

    # 4. Shape the single record (or null) into the 200 body, using the same
    #    serialization as history so the panel and history render identically.
    return _http.success(
        200, {"record": _record_body(record) if record is not None else None}
    )


def _record_body(record: InteractionRecord) -> dict[str, Any]:
    """Serialize one :class:`InteractionRecord`, omitting absent result fields.

    Mirrors the history handler's record serialization so a prior interaction
    rendered in the panel has the identical shape to the same record in history.
    """

    result = record.result
    result_body: dict[str, Any] = {"needsMoreDetail": result.needsMoreDetail}
    if result.probableIssue is not None:
        result_body["probableIssue"] = result.probableIssue
    if result.repairRecommendation is not None:
        result_body["repairRecommendation"] = result.repairRecommendation
    if result.estimatedCost is not None:
        result_body["estimatedCost"] = result.estimatedCost
    if result.confidenceScore is not None:
        result_body["confidenceScore"] = result.confidenceScore
    if result.message is not None:
        result_body["message"] = result.message

    return {
        "recordId": record.recordId,
        "sessionId": record.sessionId,
        "symptomDescription": record.symptomDescription,
        "result": result_body,
        "createdAt": record.createdAt,
    }
