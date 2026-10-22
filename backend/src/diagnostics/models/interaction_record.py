"""The persisted record of a single request-response exchange."""

from __future__ import annotations

from dataclasses import dataclass

from .diagnostic_result import DiagnosticResult

__all__ = ["InteractionRecord"]


@dataclass(frozen=True, slots=True)
class InteractionRecord:
    """A stored record of one Symptom_Description and its Diagnostic_Result.

    ``recordId`` is a globally unique UUID v4 (Req 4.2). ``createdAt`` is an
    ISO 8601 UTC timestamp with millisecond precision (Req 4.1) and is the sole
    ordering key for retrieval (Req 3.3, 4.4, 5.1).

    ``customerId`` is the owning Customer's Cognito ``sub``. It enables the
    owner-scoped "most-recent prior interaction" access pattern (Req 13.1, 13.4)
    and backs the GSI2 key attributes written for newly persisted records. It is
    optional and defaults to ``None`` because the attribute is forward-only:
    records written before this change carry no ``customerId``, so read paths
    must tolerate its absence rather than assume every record is owner-attributed.
    """

    recordId: str
    sessionId: str
    symptomDescription: str
    result: DiagnosticResult
    createdAt: str
    customerId: str | None = None
