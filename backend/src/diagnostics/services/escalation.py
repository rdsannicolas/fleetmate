"""Escalation decision for a generated diagnosis (service layer).

A diagnosis produced by the :class:`~diagnostics.services.AiEngine` is returned
to the Customer as *authoritative* only when the system is confident it is both
determinate and safe. Otherwise the service escalates it -- surfacing an
``escalated: true`` flag so the Customer response signals that the diagnosis
needs human review rather than being acted on directly.

This module is a **pure decision**: it takes an already-produced
:class:`~diagnostics.models.DiagnosticResult` (plus an out-of-band
Guardrail-safety signal) and returns an :class:`EscalationDecision`. It performs
no I/O, holds no state, and never calls Bedrock, DynamoDB, or the clock, so the
three escalation triggers are trivially unit-testable and the service layer can
compute ``escalated`` deterministically.

Escalation triggers (design "Confidence-driven escalation"; tech.md "Confidence
Threshold"; Req 2.3). A diagnosis is escalated when **any** of the following
hold:

* **Low confidence** -- ``confidenceScore < 0.70``. A missing confidence score
  (``None``) is treated as low confidence, since the system cannot assert the
  diagnosis meets the threshold.
* **Unsafe recommendation** -- Bedrock Guardrails flagged the recommendation as
  unsafe. Guardrails run inside the AI_Engine adapter; the service passes the
  resulting safety signal in via ``guardrail_flagged`` because an escalation is a
  pure function of its inputs and must not reach back into the provider.
* **Ambiguous diagnosis** -- the result is not a single determinate diagnosis:
  the insufficient-information shape (``needsMoreDetail`` is ``True``), or a
  success shape that is missing a Probable_Issue or a Repair_Recommendation.

The threshold is defined once here (:data:`CONFIDENCE_THRESHOLD`) so the service
layer and its tests share a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from diagnostics.models import DiagnosticResult

__all__ = [
    "CONFIDENCE_THRESHOLD",
    "EscalationDecision",
    "evaluate_escalation",
    "should_escalate",
]

# tech.md "Confidence Threshold": escalate when confidenceScore < 0.70. The
# boundary is exclusive -- a score of exactly 0.70 meets the threshold and does
# not escalate on confidence alone.
CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """The outcome of evaluating a diagnosis against the escalation triggers.

    ``escalated`` is the single boolean the service layer places on the Customer
    response. The three boolean reasons are retained for observability/audit
    logging and testing so a decision can explain *why* it escalated without
    re-deriving it. ``escalated`` is ``True`` exactly when at least one reason is
    ``True``.
    """

    escalated: bool
    low_confidence: bool
    unsafe_recommendation: bool
    ambiguous: bool

    @property
    def reasons(self) -> tuple[str, ...]:
        """Return the active escalation reason labels (for audit logging)."""

        labels: list[str] = []
        if self.low_confidence:
            labels.append("LOW_CONFIDENCE")
        if self.unsafe_recommendation:
            labels.append("UNSAFE_RECOMMENDATION")
        if self.ambiguous:
            labels.append("AMBIGUOUS")
        return tuple(labels)


def _is_ambiguous(result: DiagnosticResult) -> bool:
    """Return ``True`` when the diagnosis is not a single determinate result.

    The insufficient-information shape (``needsMoreDetail``) is ambiguous by
    definition (Req 2.5). A nominal success shape is also ambiguous if it is
    missing either half of the required pair -- a Probable_Issue or a
    Repair_Recommendation (Req 2.2) -- since there is then no single determinate
    diagnosis to act on. Blank/whitespace-only values are treated as missing.
    """

    if result.needsMoreDetail:
        return True
    has_issue = bool(result.probableIssue and result.probableIssue.strip())
    has_recommendation = bool(
        result.repairRecommendation and result.repairRecommendation.strip()
    )
    return not (has_issue and has_recommendation)


def _is_low_confidence(result: DiagnosticResult) -> bool:
    """Return ``True`` when confidence is below the threshold or unknown.

    A ``None`` score means the system cannot assert the diagnosis meets the
    0.70 threshold, so it is treated as low confidence and escalated.
    """

    if result.confidenceScore is None:
        return True
    return result.confidenceScore < CONFIDENCE_THRESHOLD


def evaluate_escalation(
    result: DiagnosticResult,
    *,
    guardrail_flagged: bool = False,
) -> EscalationDecision:
    """Evaluate a diagnosis against the three escalation triggers (Req 2.3).

    Pure function. ``result`` is the diagnosis produced by the AI_Engine;
    ``guardrail_flagged`` is the Bedrock Guardrails safety signal supplied by the
    service layer (``True`` when Guardrails flagged the recommendation as
    unsafe). Returns an :class:`EscalationDecision` whose ``escalated`` field is
    ``True`` when confidence is below 0.70, the recommendation was flagged
    unsafe, or the diagnosis is ambiguous.
    """

    low_confidence = _is_low_confidence(result)
    unsafe_recommendation = bool(guardrail_flagged)
    ambiguous = _is_ambiguous(result)
    escalated = low_confidence or unsafe_recommendation or ambiguous
    return EscalationDecision(
        escalated=escalated,
        low_confidence=low_confidence,
        unsafe_recommendation=unsafe_recommendation,
        ambiguous=ambiguous,
    )


def should_escalate(
    result: DiagnosticResult,
    *,
    guardrail_flagged: bool = False,
) -> bool:
    """Return ``True`` when ``result`` should be escalated (see Req 2.3).

    Thin boolean convenience over :func:`evaluate_escalation` for callers that
    only need the ``escalated`` flag and not the per-reason breakdown.
    """

    return evaluate_escalation(result, guardrail_flagged=guardrail_flagged).escalated
