"""Unit tests for the shared structured JSON logger (Task 2.2).

Assert the structured JSON log shape required by tech.md: the first four fields
are ``correlationId``, ``service``, ``level``, ``message`` (in order), extra
context is appended, and the required fields can never be overwritten.

_Requirements: 4.5_
"""

from __future__ import annotations

import io
import json

from shared.logging import StructuredLogger, format_log, get_logger


def test_format_log_has_required_fields_in_order() -> None:
    """The record carries the four required keys first, in tech.md order."""
    line = format_log("corr-1", "diagnostic-service", "INFO", "diagnosis done")
    record = json.loads(line)

    assert list(record.keys())[:4] == [
        "correlationId",
        "service",
        "level",
        "message",
    ]
    assert record == {
        "correlationId": "corr-1",
        "service": "diagnostic-service",
        "level": "INFO",
        "message": "diagnosis done",
    }


def test_format_log_appends_context_fields() -> None:
    """Extra context (e.g. sessionId) is included alongside required fields."""
    line = format_log(
        "corr-2",
        "diagnostic-service",
        "ERROR",
        "persist failed",
        {"sessionId": "s1", "createdAt": "2024-01-01T00:00:00.000Z"},
    )
    record = json.loads(line)

    assert record["sessionId"] == "s1"
    assert record["createdAt"] == "2024-01-01T00:00:00.000Z"
    assert record["level"] == "ERROR"


def test_context_cannot_overwrite_required_fields() -> None:
    """Context keys colliding with required fields are ignored."""
    line = format_log(
        "corr-3",
        "diagnostic-service",
        "INFO",
        "real message",
        {"message": "spoofed", "level": "DEBUG", "service": "other"},
    )
    record = json.loads(line)

    assert record["message"] == "real message"
    assert record["level"] == "INFO"
    assert record["service"] == "diagnostic-service"


def test_format_log_is_single_line() -> None:
    """Records are single-line so CloudWatch parses one JSON object per line."""
    line = format_log("corr-4", "svc", "WARNING", "line one\nline two")
    assert "\n" not in line


def test_format_log_tolerates_non_serializable_context() -> None:
    """A non-serializable context value never raises -- logging must not break."""

    class Weird:
        def __str__(self) -> str:
            return "weird-value"

    line = format_log("corr-5", "svc", "INFO", "msg", {"obj": Weird()})
    record = json.loads(line)
    assert record["obj"] == "weird-value"


def test_logger_writes_json_line_to_stream() -> None:
    """StructuredLogger writes one JSON line per call and returns it."""
    stream = io.StringIO()
    logger = StructuredLogger("diagnostic-service", "corr-6", stream=stream)

    returned = logger.info("Vehicle diagnosis completed", sessionId="s9")

    written = stream.getvalue()
    assert written.endswith("\n")
    assert written.strip() == returned
    record = json.loads(returned)
    assert record["correlationId"] == "corr-6"
    assert record["service"] == "diagnostic-service"
    assert record["level"] == "INFO"
    assert record["message"] == "Vehicle diagnosis completed"
    assert record["sessionId"] == "s9"


def test_logger_level_helpers_set_level() -> None:
    """Each severity helper stamps the matching level."""
    stream = io.StringIO()
    logger = get_logger("svc", "corr-7", stream=stream)

    for method, level in [
        (logger.debug, "DEBUG"),
        (logger.info, "INFO"),
        (logger.warning, "WARNING"),
        (logger.error, "ERROR"),
        (logger.critical, "CRITICAL"),
    ]:
        record = json.loads(method("m"))
        assert record["level"] == level


def test_logger_shares_correlation_id_across_calls() -> None:
    """Every record from one logger carries the same correlation id."""
    stream = io.StringIO()
    logger = StructuredLogger("svc", "corr-8", stream=stream)
    logger.info("first")
    logger.warning("second")

    lines = [json.loads(ln) for ln in stream.getvalue().splitlines()]
    assert {ln["correlationId"] for ln in lines} == {"corr-8"}
