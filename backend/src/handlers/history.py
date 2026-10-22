"""``GET /diagnostics/history`` thin Lambda handler (Task 13.1).

The handler resolves the Cognito principal, reads the query-string parameters,
and delegates to :meth:`InteractionStore.get_session_history`. Authorization
(owner-only) and all retrieval logic live in the store; the handler only extracts
the request and maps the typed outcome to HTTP.

Request (Customer JWT, owner only)::

    GET /diagnostics/history?sessionId=...&pageToken=...

Responses (design response contract):

* ``200`` -- ``{ records: InteractionRecord[<=50], nextPageToken?, timestamp }``
  in ascending creation order (Req 5.1, 5.5); a known-but-empty Session yields an
  empty ``records`` list (Req 5.2).
* ``400`` -- missing/blank ``sessionId`` (a malformed request).
* ``404`` -- ``NOT_FOUND`` for an unknown Session (Req 5.3).
* ``403`` -- ``ACCESS_DENIED`` when the caller does not own the Session (Req 5.4),
  or when the JWT claims are missing/invalid.

Every body carries a ``timestamp``; error bodies are ``{ code, suggestion,
timestamp }`` and never leak internal detail. :func:`safe_handler` guards the
entry point so unexpected exceptions become a generic ``503``.
"""

from __future__ import annotations

from typing import Any, Mapping

from diagnostics.models import InteractionRecord, Page
from shared.outcomes import Err

from . import _http
from ._dependencies import get_interaction_store

__all__ = ["handler"]


@_http.safe_handler
def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for ``GET /diagnostics/history``."""

    # 1. Resolve the Customer principal (fail-closed on missing claims).
    principal_outcome = _http.resolve_principal(event)
    if isinstance(principal_outcome, Err):
        return _http.error_from(principal_outcome)
    principal = principal_outcome.value

    # 2. Read the query params. A missing/blank sessionId is a bad request (400).
    params = _http.query_params(event)
    session_id = (params.get("sessionId") or "").strip()
    if not session_id:
        raise _http.BadRequest("sessionId is required.")
    page_token = (params.get("pageToken") or "").strip() or None

    # 3. Delegate; the store enforces owner-only authorization before any read.
    outcome = get_interaction_store().get_session_history(
        principal, session_id, page_token
    )
    if isinstance(outcome, Err):
        # NOT_FOUND -> 404, ACCESS_DENIED -> 403 (via the shared status map).
        return _http.error_from(outcome)

    # 4. Shape the ascending, <=50-record page into the 200 body.
    return _http.success(200, _page_body(outcome.value))


def _page_body(page: Page) -> dict[str, Any]:
    """Serialize a :class:`Page` into the history response body.

    ``records`` is emitted in order; ``nextPageToken`` is included only when more
    records remain (Req 5.5).
    """

    body: dict[str, Any] = {
        "records": [_record_body(record) for record in page.records],
    }
    if page.nextPageToken is not None:
        body["nextPageToken"] = page.nextPageToken
    return body


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
