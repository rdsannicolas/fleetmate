"""The structured output produced by the AI_Engine."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DiagnosticResult"]


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """Structured diagnosis parsed from the AI model -- never raw model text.

    The model encodes two mutually exclusive shapes (design "Field Notes"):

    * Success shape -- ``probableIssue``, ``repairRecommendation``,
      ``estimatedCost``, and ``confidenceScore`` are present and
      ``needsMoreDetail`` is ``False`` (Req 2.2).
    * Insufficient-info shape -- ``probableIssue`` is absent,
      ``needsMoreDetail`` is ``True``, and ``message`` carries a
      Customer-observable prompt for more detail (Req 2.5).

    Optional fields default to ``None`` so partial/insufficient results are
    representable without sentinel strings.
    """

    needsMoreDetail: bool = False
    probableIssue: str | None = None
    repairRecommendation: str | None = None
    estimatedCost: str | None = None
    confidenceScore: float | None = None
    message: str | None = None
