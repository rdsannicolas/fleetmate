"""Structured JSON logging utilities.

Every FleetMate service emits structured JSON logs carrying a ``correlationId``,
``service``, ``level``, and ``message`` (tech.md required log format), plus any
additional context fields. Centralizing the log shape here guarantees every
Lambda handler and service layer produces machine-parseable, correlatable logs
without ``print``/``console.log`` debugging (tech.md anti-patterns).
"""

from __future__ import annotations

from .logger import LogLevel, StructuredLogger, format_log, get_logger

__all__ = [
    "LogLevel",
    "StructuredLogger",
    "format_log",
    "get_logger",
]
