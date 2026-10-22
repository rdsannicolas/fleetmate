"""``POST /diagnostics/analyze`` thin Lambda handler (Task 13.1).

The handler is intentionally thin (design "Thin Lambda handlers"): it resolves
the Cognito principal, parses the request body, attaches a ``correlationId``, and
delegates to :meth:`DiagnosticsService.analyze`. **No orchestration happens
here** -- validation, session resolution, AI generation, escalation, and
persistence all live in the service layer; the handler only maps the returned
:class:`~diagnostics.services.SubmissionResult` to an HTTP response.

Request (Customer JWT)::

    POST /diagnostics/analyze
    { "symptomText": "...", "sessionId"?: "...",
      "vehicle"?: { "make"?, "model"?, "year"? } }

Responses (design response contract):

* ``200`` -- :class:`~diagnostics.services.DiagnosticResponse`: the structured
  diagnosis fields (``probableIssue``/``repairRecommendation``/``estimatedCost``/
  ``confidenceScore`` or the insufficient-info ``needsMoreDetail``/``message``),
  plus ``escalated``, ``sessionId``, and ``timestamp`` (Req 1.1, 2.3, 2.5).
* ``422`` -- ``EMPTY``/``TOO_LONG`` (invalid symptom, Req 1.2/1.3) or
  ``SESSION_CLOSED``/``LIMIT_REACHED`` (unusable session, Req 3.4/3.5).
* ``503`` -- ``AI_TIMEOUT``/``AI_ERROR`` (Req 2.6).
* ``403`` -- missing/invalid JWT claims (``ACCESS_DENIED``).

Every body carries a ``timestamp``; error bodies are ``{ code, suggestion,
timestamp }`` and never leak internal/provider detail (Req 5.4). The whole entry
point is wrapped by :func:`safe_handler` so an unexpected exception becomes a
generic ``503`` rather than a stack trace crossing the API boundary.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from shared.outcomes import Err

from diagnostics.services import (
    DiagnosticResponse,
    Failure,
    Rejection,
    Vehicle,
)

from . import _http
from ._dependencies import get_diagnostics_service

__all__ = ["handler"]


@_http.safe_handler
def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point for ``POST /diagnostics/analyze``."""

    # 1. Resolve the Customer principal from the Cognito claims (fail-closed).
    principal_outcome = _http.resolve_principal(event)
    if isinstance(principal_outcome, Err):
        return _http.error_from(principal_outcome)
    principal = principal_outcome.value

    # 2. Parse the request body (malformed JSON -> 400 via safe_handler).
    body = _http.parse_json_body(event)
    symptom_text = _symptom_text(body)
    session_id = _optional_str(body.get("sessionId"))
    vehicle = _parse_vehicle(body.get("vehicle"))
    correlation_id = _http.correlation_id_from(event)

    # 3. Delegate to the service layer. The Customer's sub is the customer_id;
    #    no orchestration lives here.
    result = get_diagnostics_service().analyze(
        principal.user_id,
        symptom_text,
        session_id=session_id,
        vehicle=vehicle,
        correlation_id=correlation_id,
    )

    # 4. Map the typed SubmissionResult to an HTTP response.
    return _to_response(result)


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _symptom_text(body: Mapping[str, Any]) -> Optional[str]:
    """Read ``symptomText`` from the body, or raise ``400`` if it is non-string.

    A missing value is passed through as ``None`` so the service's shared
    validator rejects it with ``EMPTY`` (422) -- keeping validation semantics in
    one place. A present-but-non-string value is a malformed request (400).
    """

    value = body.get("symptomText")
    if value is None:
        return None
    if not isinstance(value, str):
        raise _http.BadRequest("symptomText must be a string.")
    return value


def _optional_str(value: Any) -> Optional[str]:
    """Coerce an optional string field; a non-string present value is a 400."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise _http.BadRequest("sessionId must be a string.")
    stripped = value.strip()
    return stripped or None


def _parse_vehicle(value: Any) -> Optional[Vehicle]:
    """Parse the optional ``vehicle`` object into a :class:`Vehicle`.

    All fields are optional (partial details accepted). A present ``vehicle`` that
    is not an object, or a ``year`` that is not an integer, is a malformed request
    (400). Field-level business validation of the values themselves belongs to the
    service layer, not the handler.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _http.BadRequest("vehicle must be an object.")

    make = _optional_str_field(value.get("make"), "vehicle.make")
    model = _optional_str_field(value.get("model"), "vehicle.model")
    year = value.get("year")
    if year is not None and not isinstance(year, int):
        raise _http.BadRequest("vehicle.year must be an integer.")

    if make is None and model is None and year is None:
        return None
    return Vehicle(make=make, model=model, year=year)


def _optional_str_field(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _http.BadRequest(f"{name} must be a string.")
    return value


# ---------------------------------------------------------------------------
# Result -> HTTP mapping
# ---------------------------------------------------------------------------


def _to_response(result: Any) -> dict[str, Any]:
    """Map a :class:`SubmissionResult` to its HTTP response."""

    if isinstance(result, DiagnosticResponse):
        return _http.success(200, _diagnosis_body(result))
    if isinstance(result, (Rejection, Failure)):
        # Rejection -> 422 (EMPTY/TOO_LONG/SESSION_CLOSED/LIMIT_REACHED);
        # Failure -> 503 (AI_TIMEOUT/AI_ERROR). Both carry a safe suggestion.
        return _http.error_from(Err(result.reason, result.suggestion))
    # Defensive: an unrecognised result shape never leaks internals.
    return _http.error(503, "INTERNAL_ERROR", "Something went wrong. Please try again shortly.")


def _diagnosis_body(response: DiagnosticResponse) -> dict[str, Any]:
    """Shape a successful :class:`DiagnosticResponse` into the 200 body.

    The success shape surfaces the four structured diagnosis fields plus
    ``escalated``; the insufficient-info shape surfaces ``needsMoreDetail`` and
    the customer-observable ``message`` instead (Req 2.5). ``None`` fields are
    omitted so the body carries only meaningful keys.
    """

    result = response.result
    body: dict[str, Any] = {
        "sessionId": response.sessionId,
        "escalated": response.escalated,
        "needsMoreDetail": result.needsMoreDetail,
    }
    if result.probableIssue is not None:
        body["probableIssue"] = result.probableIssue
    if result.repairRecommendation is not None:
        body["repairRecommendation"] = result.repairRecommendation
    if result.estimatedCost is not None:
        body["estimatedCost"] = result.estimatedCost
    if result.confidenceScore is not None:
        body["confidenceScore"] = result.confidenceScore
    if result.message is not None:
        body["message"] = result.message
    return body
