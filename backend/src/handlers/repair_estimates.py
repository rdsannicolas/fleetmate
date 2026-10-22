"""``GET /repair-estimates`` thin Lambda handler (Task 13.1).

The analyst/reporting view: an analyst runs a date-range query over persisted
Interaction_Records. The handler resolves the Cognito principal, reads the
``start``/``end`` query params, and delegates to
:meth:`InteractionStore.query_by_date_range`. Analyst RBAC and range validation
live in the store (it checks the analyst role *before* any read and rejects
inverted ranges); the handler only extracts the request and maps the outcome.

Request (analyst JWT)::

    GET /repair-estimates?start=<ISO8601>&end=<ISO8601>

Responses (design response contract):

* ``200`` -- ``{ records: InteractionRecord[], timestamp }`` ascending by
  creation timestamp (Req 4.4).
* ``400`` -- missing ``start``/``end`` (malformed request) or ``INVALID_RANGE``
  when ``start`` is later than ``end`` (Req 4.6).
* ``403`` -- ``ACCESS_DENIED`` when the caller lacks the analyst role (Req 4.5),
  or when the JWT claims are missing/invalid.

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
    """Lambda entry point for ``GET /repair-estimates``."""

    # 1. Resolve the principal (fail-closed). The store enforces the analyst role.
    principal_outcome = _http.resolve_principal(event)
    if isinstance(principal_outcome, Err):
        return _http.error_from(principal_outcome)
    principal = principal_outcome.value

    # 2. Read the range params. Missing bounds are a bad request (400).
    params = _http.query_params(event)
    start = (params.get("start") or "").strip()
    end = (params.get("end") or "").strip()
    if not start or not end:
        raise _http.BadRequest("Both start and end date-range parameters are required.")

    # 3. Delegate; analyst RBAC is checked before any record is read, and an
    #    inverted range is rejected with INVALID_RANGE.
    outcome = get_interaction_store().query_by_date_range(principal, start, end)
    if isinstance(outcome, Err):
        # ACCESS_DENIED -> 403, INVALID_RANGE -> 400 (via the shared status map).
        return _http.error_from(outcome)

    # 4. Shape the ascending records into the 200 body.
    return _http.success(
        200, {"records": [_record_body(record) for record in outcome.value]}
    )


def _record_body(record: InteractionRecord) -> dict[str, Any]:
    """Serialize one :class:`InteractionRecord`, omitting absent result fields."""

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
