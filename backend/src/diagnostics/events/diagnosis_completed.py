"""The ``DiagnosisCompleted`` event -- the Interaction_Record draft carried
asynchronously from the diagnostics service to the persistence Lambda.

The diagnostics service emits one ``DiagnosisCompleted`` event per returned
diagnosis (design "Non-blocking, event-driven persistence"). The event is a
*draft* Interaction_Record: it carries the ``sessionId``, the
``symptomDescription``, and the structured :class:`DiagnosticResult`, but it does
*not* carry a ``recordId`` -- the store owns identity (UUID v4) and the
authoritative ``createdAt`` timestamp (design "Store owns identity, time").

A ``createdAt`` field is carried on the event as well, stamped when the diagnosis
completed, so a persistence failure can be logged with the Session id and
timestamp (Req 4.3) even before the record itself is written. The repository is
free to use this value as the record's ``createdAt`` so the persisted timestamp
reflects when the diagnosis actually completed.

The event (de)serializes to/from a plain JSON-compatible ``dict`` so it survives
the EventBridge -> SQS hop as a message body without any AWS types leaking into
the domain.
"""

from __future__ import annotations

from dataclasses import dataclass

from diagnostics.models import DiagnosticResult

__all__ = [
    "DiagnosisCompleted",
    "EVENT_SOURCE",
    "EVENT_DETAIL_TYPE",
]

# EventBridge envelope identifiers. The persistence rule matches on these so only
# ``DiagnosisCompleted`` events are routed to the persistence queue.
EVENT_SOURCE = "fleetmate.diagnostics"
EVENT_DETAIL_TYPE = "DiagnosisCompleted"


def _result_to_dict(result: DiagnosticResult) -> dict:
    """Serialize a :class:`DiagnosticResult`, omitting absent optional fields."""

    data: dict = {"needsMoreDetail": result.needsMoreDetail}
    if result.probableIssue is not None:
        data["probableIssue"] = result.probableIssue
    if result.repairRecommendation is not None:
        data["repairRecommendation"] = result.repairRecommendation
    if result.estimatedCost is not None:
        data["estimatedCost"] = result.estimatedCost
    if result.confidenceScore is not None:
        data["confidenceScore"] = result.confidenceScore
    if result.message is not None:
        data["message"] = result.message
    return data


def _result_from_dict(data: dict) -> DiagnosticResult:
    return DiagnosticResult(
        needsMoreDetail=bool(data.get("needsMoreDetail", False)),
        probableIssue=data.get("probableIssue"),
        repairRecommendation=data.get("repairRecommendation"),
        estimatedCost=data.get("estimatedCost"),
        confidenceScore=data.get("confidenceScore"),
        message=data.get("message"),
    )


@dataclass(frozen=True, slots=True)
class DiagnosisCompleted:
    """An Interaction_Record draft emitted when a diagnosis completes.

    This is the ``detail`` payload of the EventBridge event. It intentionally
    omits ``recordId`` -- the Interaction_Store mints the UUID v4 id on write
    (Req 4.2) so the id can never be forged or duplicated by an upstream caller.
    """

    sessionId: str
    symptomDescription: str
    result: DiagnosticResult
    createdAt: str  # ISO 8601 UTC (ms), stamped when the diagnosis completed
    customerId: str | None = None

    def to_detail(self) -> dict:
        """Serialize to the plain ``dict`` carried as the EventBridge detail.

        ``customerId`` (the owning Customer's Cognito ``sub``) is included only
        when present, matching the module's "omit absent optional fields"
        convention. It lets the persistence Lambda owner-attribute the newly
        written record (GSI2 keys) for the owner-scoped most-recent-prior lookup
        (Req 13.1, 13.4). A draft or message lacking it stays valid and persists
        exactly as before (forward-only).
        """

        detail = {
            "sessionId": self.sessionId,
            "symptomDescription": self.symptomDescription,
            "result": _result_to_dict(self.result),
            "createdAt": self.createdAt,
        }
        if self.customerId is not None:
            detail["customerId"] = self.customerId
        return detail

    @classmethod
    def from_detail(cls, detail: dict) -> "DiagnosisCompleted":
        """Reconstruct a ``DiagnosisCompleted`` from an EventBridge detail dict.

        Raises ``KeyError``/``TypeError`` for a malformed payload so the
        persistence Lambda can treat an unparseable message as a failure (and let
        SQS redrive it to the DLQ) rather than silently writing a partial record.
        """

        return cls(
            sessionId=detail["sessionId"],
            symptomDescription=detail["symptomDescription"],
            result=_result_from_dict(detail["result"]),
            createdAt=detail["createdAt"],
            customerId=detail.get("customerId"),
        )
