"""Structured JSON logger for FleetMate services.

The logger always emits a single-line JSON object whose first four fields are the
tech.md-required keys -- ``correlationId``, ``service``, ``level``, ``message`` --
followed by any extra context passed at call time. Example (per tech.md):

    {"correlationId": "uuid", "service": "diagnostic-service",
     "level": "INFO", "message": "Vehicle diagnosis completed"}

The logger is intentionally free of request/response state: a ``StructuredLogger``
is bound to a ``service`` and ``correlationId`` and writes each record to a sink
(``sys.stdout`` by default, so CloudWatch captures it). Keeping the formatting in a
pure :func:`format_log` function makes the log shape unit-testable without
capturing stdout.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, Literal

__all__ = ["LogLevel", "StructuredLogger", "format_log", "get_logger"]

# The structured log severity levels used across the backend.
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# The four fields every record must carry, in the order tech.md specifies.
_REQUIRED_FIELDS = ("correlationId", "service", "level", "message")


def format_log(
    correlation_id: str,
    service: str,
    level: LogLevel,
    message: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Render a single structured log record as a compact JSON string.

    The required fields (``correlationId``, ``service``, ``level``, ``message``)
    are emitted first and in order; ``context`` supplies any additional fields
    (for example ``sessionId`` or ``recordId``). Context keys that collide with a
    required field are ignored so the required shape can never be overwritten.

    The output is deterministic and single-line so it parses cleanly in
    CloudWatch Logs Insights.
    """

    record: dict[str, Any] = {
        "correlationId": correlation_id,
        "service": service,
        "level": level,
        "message": message,
    }
    if context:
        for key, value in context.items():
            if key in _REQUIRED_FIELDS:
                continue
            record[key] = value

    # ``default=str`` keeps the logger from ever raising on a non-serializable
    # context value -- logging must never break the request path.
    return json.dumps(record, default=str, separators=(",", ":"))


class StructuredLogger:
    """A logger bound to one ``service`` and one ``correlationId``.

    Each log call writes a single JSON line (see :func:`format_log`) to the
    configured stream. One logger instance is created per request so every record
    for that request shares the same correlation id, making the whole flow
    traceable in CloudWatch.
    """

    __slots__ = ("_service", "_correlation_id", "_stream")

    def __init__(
        self,
        service: str,
        correlation_id: str,
        stream: IO[str] | None = None,
    ) -> None:
        self._service = service
        self._correlation_id = correlation_id
        self._stream = stream if stream is not None else sys.stdout

    @property
    def correlation_id(self) -> str:
        """The correlation id stamped on every record from this logger."""

        return self._correlation_id

    @property
    def service(self) -> str:
        """The service name stamped on every record from this logger."""

        return self._service

    def log(self, level: LogLevel, message: str, **context: Any) -> str:
        """Emit a record at ``level`` and return the JSON line that was written."""

        line = format_log(
            self._correlation_id,
            self._service,
            level,
            message,
            context or None,
        )
        self._stream.write(line + "\n")
        return line

    def debug(self, message: str, **context: Any) -> str:
        """Emit a ``DEBUG`` record."""

        return self.log("DEBUG", message, **context)

    def info(self, message: str, **context: Any) -> str:
        """Emit an ``INFO`` record."""

        return self.log("INFO", message, **context)

    def warning(self, message: str, **context: Any) -> str:
        """Emit a ``WARNING`` record."""

        return self.log("WARNING", message, **context)

    def error(self, message: str, **context: Any) -> str:
        """Emit an ``ERROR`` record."""

        return self.log("ERROR", message, **context)

    def critical(self, message: str, **context: Any) -> str:
        """Emit a ``CRITICAL`` record."""

        return self.log("CRITICAL", message, **context)


def get_logger(
    service: str,
    correlation_id: str,
    stream: IO[str] | None = None,
) -> StructuredLogger:
    """Create a :class:`StructuredLogger` bound to ``service``/``correlation_id``."""

    return StructuredLogger(service, correlation_id, stream)
