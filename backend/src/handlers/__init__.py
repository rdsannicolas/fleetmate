"""Thin AWS Lambda entry points. Handlers parse/authorize/validate and delegate
to the diagnostics service layer; no orchestration logic lives here.

Three handlers back the REST API (design "Diagnostic_Assistant"):

* :func:`handlers.analyze.handler` -- ``POST /diagnostics/analyze`` (Customer JWT)
* :func:`handlers.history.handler` -- ``GET /diagnostics/history`` (owner-only)
* :func:`handlers.repair_estimates.handler` -- ``GET /repair-estimates`` (analyst)

Each exposes a Lambda ``handler(event, context)`` entry point. Dependency wiring
lives behind the injectable factory seam in :mod:`handlers._dependencies` so the
handlers are unit-testable without AWS; shared response/claims helpers live in
:mod:`handlers._http`.
"""

from __future__ import annotations

from .analyze import handler as analyze_handler
from .history import handler as history_handler
from .repair_estimates import handler as repair_estimates_handler

__all__ = [
    "analyze_handler",
    "history_handler",
    "repair_estimates_handler",
]
