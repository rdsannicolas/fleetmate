"""Unit tests for the shared HTTP helper's CORS behavior (Task 24.3).

The browser SPA is served from the CloudFront distribution and calls the API
cross-origin, so every Lambda response must echo ``Access-Control-Allow-Origin``
set to the specific CloudFront origin (never ``*``, since requests carry an
``Authorization`` header). The allowed origin is read from the ``WEB_APP_ORIGIN``
environment variable, which the SAM template sets to
``https://${WebAppDistribution.DomainName}`` on the API Lambdas.

The unauthenticated ``OPTIONS`` preflight is normally answered by an API Gateway
MOCK integration, but ``safe_handler`` also short-circuits a direct OPTIONS
invocation so OPTIONS never requires authentication at the Lambda either.

_Requirements: 8.2, 9.1_
"""

from __future__ import annotations

import json

import pytest

from handlers import _http

WEB_ORIGIN = "https://d123abc.cloudfront.net"


@pytest.fixture
def _with_origin(monkeypatch):
    """Set the configured CloudFront origin for the duration of a test."""

    monkeypatch.setenv(_http.WEB_APP_ORIGIN_ENV, WEB_ORIGIN)
    yield WEB_ORIGIN


@pytest.fixture
def _without_origin(monkeypatch):
    """Ensure no origin is configured."""

    monkeypatch.delenv(_http.WEB_APP_ORIGIN_ENV, raising=False)
    yield


# ---------------------------------------------------------------------------
# cors_headers
# ---------------------------------------------------------------------------


def test_cors_headers_reflect_specific_origin(_with_origin):
    headers = _http.cors_headers()

    # The specific CloudFront origin is reflected -- never a bare wildcard.
    assert headers["Access-Control-Allow-Origin"] == WEB_ORIGIN
    assert headers["Access-Control-Allow-Origin"] != "*"
    # Methods and headers cover what the browser SPA sends.
    assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
    assert "GET" in headers["Access-Control-Allow-Methods"]
    assert "POST" in headers["Access-Control-Allow-Methods"]
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    assert "Content-Type" in headers["Access-Control-Allow-Headers"]
    assert headers["Vary"] == "Origin"


def test_cors_headers_empty_when_origin_unset(_without_origin):
    assert _http.cors_headers() == {}


def test_cors_headers_empty_when_origin_blank(monkeypatch):
    monkeypatch.setenv(_http.WEB_APP_ORIGIN_ENV, "   ")
    assert _http.cors_headers() == {}


# ---------------------------------------------------------------------------
# success / error responses carry the CORS origin
# ---------------------------------------------------------------------------


def test_success_response_includes_cors_origin(_with_origin):
    resp = _http.success(200, {"ok": True})
    assert resp["headers"]["Access-Control-Allow-Origin"] == WEB_ORIGIN
    assert resp["headers"]["Content-Type"] == "application/json"


def test_error_response_includes_cors_origin(_with_origin):
    resp = _http.error(503, "AI_ERROR", "Try again shortly.")
    assert resp["headers"]["Access-Control-Allow-Origin"] == WEB_ORIGIN
    body = json.loads(resp["body"])
    # The no-leak error-body contract is unchanged.
    assert set(body) == {"code", "suggestion", "timestamp"}


def test_response_omits_cors_when_origin_unset(_without_origin):
    resp = _http.success(200, {"ok": True})
    assert "Access-Control-Allow-Origin" not in resp["headers"]


# ---------------------------------------------------------------------------
# OPTIONS preflight handling in safe_handler (unauthenticated short-circuit)
# ---------------------------------------------------------------------------


def test_safe_handler_answers_options_preflight_without_delegating(_with_origin):
    called = {"n": 0}

    @_http.safe_handler
    def handler(event, context=None):
        called["n"] += 1
        return _http.success(200, {"ok": True})

    # REST API proxy shape carries the method under httpMethod.
    resp = handler({"httpMethod": "OPTIONS"})

    assert resp["statusCode"] == 204
    assert resp["body"] == ""
    assert resp["headers"]["Access-Control-Allow-Origin"] == WEB_ORIGIN
    # The wrapped handler (which would need auth) is never invoked for OPTIONS.
    assert called["n"] == 0


def test_safe_handler_options_http_api_v2_shape(_with_origin):
    @_http.safe_handler
    def handler(event, context=None):
        raise AssertionError("should not be called for OPTIONS")

    resp = handler({"requestContext": {"http": {"method": "OPTIONS"}}})

    assert resp["statusCode"] == 204
    assert resp["headers"]["Access-Control-Allow-Origin"] == WEB_ORIGIN


def test_safe_handler_passes_through_non_options(_with_origin):
    @_http.safe_handler
    def handler(event, context=None):
        return _http.success(200, {"ok": True})

    resp = handler({"httpMethod": "POST"})

    assert resp["statusCode"] == 200
    assert resp["headers"]["Access-Control-Allow-Origin"] == WEB_ORIGIN
