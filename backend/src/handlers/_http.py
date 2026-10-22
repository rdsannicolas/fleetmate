"""Shared HTTP helpers for the thin Lambda handlers (Task 13.1).

The handlers are deliberately thin (design "Thin Lambda handlers"): they parse
the API Gateway proxy event, resolve the Cognito principal, delegate to the
diagnostics service layer, and shape the result into an HTTP response. All of the
response-shaping, claims-extraction, and error-code -> status mapping that the
three handlers share lives here so each handler module stays small and the
mapping is defined in exactly one place.

Key guarantees provided here:

* **Every response body carries a ``timestamp``** -- an ISO 8601 UTC string with
  millisecond precision (design response contracts; FleetMate API conventions).
* **Error bodies are always ``{ code, suggestion, timestamp }``** and never leak
  internal/provider details or stack traces (Req 5.4; tech.md anti-patterns). The
  ``suggestion`` text comes only from the typed :class:`~shared.outcomes.Err`
  messages, which are curated, customer-safe strings.
* **A single typed-error -> HTTP-status map** (:data:`ERROR_STATUS`) is the one
  authority for the 422/503/400/403/404 mapping, so every handler maps the same
  :data:`~shared.outcomes.ErrorCode` to the same status.
* **A guard decorator** (:func:`safe_handler`) wraps every entry point so an
  unexpected exception becomes a generic ``503`` with a safe suggestion instead
  of a stack trace crossing the boundary.

None of this module imports Boto3; it operates purely on dicts and the typed
outcome/error vocabulary.
"""

from __future__ import annotations

import functools
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from shared.auth import Principal, parse_principal
from shared.outcomes import ErrorCode, Err, Ok, Outcome

__all__ = [
    "ERROR_STATUS",
    "now_iso_ms",
    "success",
    "error",
    "error_from",
    "cors_headers",
    "preflight_response",
    "extract_claims",
    "resolve_principal",
    "correlation_id_from",
    "parse_json_body",
    "query_params",
    "safe_handler",
    "BadRequest",
]


# ---------------------------------------------------------------------------
# CORS (Task 24.3)
# ---------------------------------------------------------------------------
#
# The browser SPA is served from the CloudFront distribution (an HTTPS
# ``*.cloudfront.net`` origin) and calls this API cross-origin. Because requests
# carry an ``Authorization`` header, the allowed origin must be the specific
# CloudFront origin -- never a bare ``*`` wildcard. The concrete origin is injected
# into the API Lambdas as the ``WEB_APP_ORIGIN`` environment variable
# (``!Sub "https://${WebAppDistribution.DomainName}"`` in the SAM template), so
# every response reflects exactly that origin.
#
# The unauthenticated ``OPTIONS`` preflight is handled by an API Gateway MOCK
# integration in the OpenAPI body (no Cognito authorizer), so these helpers only
# need to stamp the matching headers on the actual Lambda responses.

#: Environment variable holding the allowed CloudFront origin for CORS.
WEB_APP_ORIGIN_ENV = "WEB_APP_ORIGIN"

#: Methods the browser SPA uses across the API (plus the OPTIONS preflight).
_CORS_ALLOW_METHODS = "GET,POST,OPTIONS"

#: Request headers the SPA sends (the bearer token and the JSON content type).
_CORS_ALLOW_HEADERS = "Authorization,Content-Type"


def _allowed_origin() -> Optional[str]:
    """Return the configured CloudFront origin for CORS, or ``None`` if unset.

    Reads ``WEB_APP_ORIGIN`` at call time (not import time) so tests and the
    runtime both see the current value. An unset/blank value yields ``None`` and
    no CORS header is emitted -- the API still works for non-browser callers.
    """

    origin = os.environ.get(WEB_APP_ORIGIN_ENV)
    if origin is None:
        return None
    origin = origin.strip()
    return origin or None


def cors_headers() -> dict[str, str]:
    """Build the ``Access-Control-*`` headers reflecting the CloudFront origin.

    Returns an empty dict when no origin is configured. The allowed origin is the
    specific CloudFront HTTPS origin (never ``*``) because requests are
    credentialed via the ``Authorization`` header.
    """

    origin = _allowed_origin()
    if origin is None:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": _CORS_ALLOW_METHODS,
        "Access-Control-Allow-Headers": _CORS_ALLOW_HEADERS,
        "Vary": "Origin",
    }


def preflight_response() -> dict[str, Any]:
    """Build a ``204`` CORS preflight response (used if a Lambda handles OPTIONS).

    The OPTIONS preflight is normally answered by an unauthenticated API Gateway
    MOCK integration, but this helper lets a handler respond to a direct OPTIONS
    invocation with the same CORS headers and no body.
    """

    return {
        "statusCode": 204,
        "headers": {**cors_headers()},
        "body": "",
    }


# ---------------------------------------------------------------------------
# Error-code -> HTTP status (the single mapping authority)
# ---------------------------------------------------------------------------

# Maps every typed ErrorCode that can surface at a handler to its HTTP status.
# Validation/limit/session problems are 422 (unprocessable), AI failures are 503
# (temporarily unavailable), authorization denials are 403, an unknown session is
# 404, and a bad/inverted range or a malformed request is 400.
ERROR_STATUS: dict[ErrorCode, int] = {
    "EMPTY": 422,
    "TOO_LONG": 422,
    "SESSION_CLOSED": 422,
    "LIMIT_REACHED": 422,
    "AI_TIMEOUT": 503,
    "AI_ERROR": 503,
    "PERSIST_FAILED": 503,
    "ACCESS_DENIED": 403,
    "INVALID_RANGE": 400,
    "NOT_FOUND": 404,
}

# A customer-safe fallback suggestion for an unexpected/unmapped failure. It never
# reveals internal detail (tech.md: never leak provider errors or stack traces).
_GENERIC_SUGGESTION = "Something went wrong. Please try again shortly."


class BadRequest(Exception):
    """Raised inside a handler for a malformed request -> HTTP 400.

    Carries a customer-safe ``suggestion``; the message is curated by the handler
    (e.g. "sessionId is required") and never contains internal detail. The
    :func:`safe_handler` guard turns it into a ``400`` body.
    """

    def __init__(self, suggestion: str) -> None:
        super().__init__(suggestion)
        self.suggestion = suggestion


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def now_iso_ms(clock: Callable[[], datetime] | None = None) -> str:
    """Return the current time as ISO 8601 UTC with millisecond precision.

    The format matches the store's :func:`format_timestamp` (``...Z`` suffix,
    exactly three fractional digits) so timestamps are uniform across the API.
    ``clock`` is injectable for deterministic tests.
    """

    moment = (clock or _utcnow)().astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response with a JSON string body.

    Both success and error responses include the ``Access-Control-Allow-Origin``
    (and related) CORS headers reflecting the configured CloudFront origin, so the
    browser can read the response to every cross-origin call (Task 24.3). When no
    origin is configured the CORS headers are simply omitted.
    """

    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **cors_headers()},
        "body": json.dumps(body, default=str, separators=(",", ":")),
    }


def success(
    status: int,
    payload: Mapping[str, Any],
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build a success response, injecting a ``timestamp`` into the body.

    A caller-supplied ``timestamp`` is preserved; otherwise one is stamped now.
    """

    body = dict(payload)
    body.setdefault("timestamp", now_iso_ms(clock))
    return _response(status, body)


def error(
    status: int,
    code: str,
    suggestion: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build an error response body of exactly ``{ code, suggestion, timestamp }``.

    ``suggestion`` must be a customer-safe string; this function never embeds
    internal/provider detail.
    """

    return _response(
        status,
        {"code": code, "suggestion": suggestion, "timestamp": now_iso_ms(clock)},
    )


def error_from(
    err: Err,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Map a typed :class:`Err` to its HTTP error response via :data:`ERROR_STATUS`.

    The status is looked up from the one mapping authority; an unmapped code
    defaults to ``503``. The body's ``suggestion`` is the Err's curated message,
    falling back to the generic safe suggestion when the message is blank.
    """

    status = ERROR_STATUS.get(err.code, 503)
    suggestion = err.message or _GENERIC_SUGGESTION
    return error(status, err.code, suggestion, clock=clock)


# ---------------------------------------------------------------------------
# Request parsing / claims extraction
# ---------------------------------------------------------------------------


def extract_claims(event: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Pull the Cognito JWT claims out of an API Gateway proxy event.

    Supports both authorizer shapes:

    * REST API / Cognito authorizer: ``requestContext.authorizer.claims``
    * HTTP API (JWT authorizer): ``requestContext.authorizer.jwt.claims``

    Returns ``None`` when no claims are present (an unauthenticated context),
    which callers turn into ``ACCESS_DENIED`` via :func:`resolve_principal`.
    """

    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    claims = authorizer.get("claims")
    if claims is None:
        jwt = authorizer.get("jwt") or {}
        claims = jwt.get("claims")
    return claims if isinstance(claims, Mapping) else None


def resolve_principal(event: Mapping[str, Any]) -> Outcome[Principal]:
    """Resolve a :class:`Principal` from the event's Cognito claims (fail-closed).

    Missing claims or a missing ``sub`` yield ``Err(ACCESS_DENIED)`` so handlers
    reject before any service/storage call.
    """

    return parse_principal(extract_claims(event))


def correlation_id_from(event: Mapping[str, Any]) -> str:
    """Read a correlation id from request headers, or mint a fresh UUID v4.

    Honours an inbound ``X-Correlation-Id`` (case-insensitive) so a client- or
    gateway-provided id flows through the structured logs; otherwise a new id is
    generated. The id is passed to the service layer and never trusted for
    authorization.
    """

    headers = event.get("headers") or {}
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == "x-correlation-id":
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return str(uuid.uuid4())


def parse_json_body(event: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the proxy event's JSON body into a dict.

    An absent body yields ``{}``. A present-but-malformed body (invalid JSON or a
    non-object JSON value) raises :class:`BadRequest` -> ``400``, since the
    request cannot be processed.
    """

    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        parsed: Any = raw
    else:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            raise BadRequest("The request body must be valid JSON.")
    if not isinstance(parsed, dict):
        raise BadRequest("The request body must be a JSON object.")
    return parsed


def _http_method_of(event: Mapping[str, Any]) -> Optional[str]:
    """Return the request's HTTP method (uppercased), across proxy event shapes.

    Supports the REST API proxy shape (``httpMethod``) and the HTTP API v2 shape
    (``requestContext.http.method``). Returns ``None`` when no method is present.
    """

    method = event.get("httpMethod")
    if not isinstance(method, str):
        request_context = event.get("requestContext") or {}
        if isinstance(request_context, Mapping):
            http = request_context.get("http") or {}
            if isinstance(http, Mapping):
                method = http.get("method")
    return method.upper() if isinstance(method, str) else None


def query_params(event: Mapping[str, Any]) -> dict[str, str]:
    """Return the event's query-string parameters as a plain dict (never ``None``)."""

    params = event.get("queryStringParameters")
    if not isinstance(params, Mapping):
        return {}
    return {str(k): v for k, v in params.items() if v is not None}


# ---------------------------------------------------------------------------
# Guard decorator
# ---------------------------------------------------------------------------


def safe_handler(
    func: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """Wrap a Lambda entry point so no exception/stack trace ever leaks out.

    * A :class:`BadRequest` becomes a ``400`` ``{ code: "BAD_REQUEST", suggestion }``.
    * Any other unexpected exception becomes a generic ``503`` with a safe
      suggestion -- the exception is intentionally swallowed here (callers already
      log through the structured logger) so internal/provider detail and stack
      traces never cross the API boundary (tech.md anti-patterns; Req 5.4).
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        event = args[0] if args else kwargs.get("event")
        # Answer a CORS preflight directly, without authentication or delegation,
        # should an OPTIONS request ever reach the Lambda (the API Gateway MOCK
        # integration normally handles preflight). This keeps OPTIONS unauthenticated.
        if isinstance(event, Mapping) and _http_method_of(event) == "OPTIONS":
            return preflight_response()
        try:
            return func(*args, **kwargs)
        except BadRequest as bad:
            return error(400, "BAD_REQUEST", bad.suggestion)
        except Exception:  # noqa: BLE001 -- never leak internals past the boundary.
            return error(503, "INTERNAL_ERROR", _GENERIC_SUGGESTION)

    return wrapper
