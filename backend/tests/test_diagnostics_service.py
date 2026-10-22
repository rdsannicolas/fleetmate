"""Unit tests for the Diagnostics_Service `analyze` orchestration (Task 28.2).

These exercise :class:`DiagnosticsService.analyze` with lightweight fakes for its
collaborators (the Conversation_Manager and the AI_Engine) and the real
:class:`InMemoryPublisher` so the emitted ``DiagnosisCompleted`` event can be
inspected. No AWS access is involved.

The focus here is the owner-attribution thread (Req 13.1, 13.4): the emitted
``DiagnosisCompleted`` draft must carry the authenticated Customer's id taken
from the resolved Session (``session.customerId``) so the persistence Lambda can
owner-attribute the newly written record.
"""

from __future__ import annotations

from datetime import datetime, timezone

from diagnostics.events import InMemoryPublisher
from diagnostics.models import DiagnosticResult, Session, SessionStatus
from diagnostics.services import DiagnosticResponse, DiagnosticsService
from shared.outcomes import Ok

STAMP = "2024-01-01T00:00:00.123Z"


def _success_result() -> DiagnosticResult:
    return DiagnosticResult(
        needsMoreDetail=False,
        probableIssue="Weak Battery",
        repairRecommendation="Replace battery",
        estimatedCost="INR 4,000 - 8,000",
        confidenceScore=0.89,
    )


class _FakeConversation:
    """Minimal Conversation_Manager stand-in returning a canned Session.

    Only the four methods the service calls are implemented: ``validate_follow_up``
    (echoes the text as valid), ``ensure_session`` (returns the canned Session),
    ``get_context`` (no prior turns), and ``append_interaction`` (no-op success).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def validate_follow_up(self, text):
        return Ok(text)

    def ensure_session(self, customer_id, session_id=None):
        return Ok(self._session)

    def get_context(self, session_id):
        return []

    def append_interaction(self, session_id, symptom, result, *, created_at=None):
        return Ok(None)


class _FakeAiEngine:
    def __init__(self, result: DiagnosticResult) -> None:
        self._result = result

    def generate(self, symptom_text, context=()):
        return Ok(self._result)


def _session(customer_id: str = "cust-42") -> Session:
    return Session(
        sessionId="s1",
        customerId=customer_id,
        status=SessionStatus.ACTIVE,
        createdAt=STAMP,
        lastSubmissionAt=STAMP,
        interactionCount=0,
    )


def _service(session: Session, publisher: InMemoryPublisher) -> DiagnosticsService:
    return DiagnosticsService(
        conversation=_FakeConversation(session),
        ai_engine=_FakeAiEngine(_success_result()),
        publisher=publisher,
        clock=lambda: datetime(2024, 1, 1, 0, 0, 0, 123000, tzinfo=timezone.utc),
    )


def test_emitted_event_carries_session_customer_id():
    # The DiagnosisCompleted draft must carry the resolved Session's customerId
    # so the persisted record is owner-attributed (Req 13.1, 13.4).
    publisher = InMemoryPublisher()
    session = _session("cust-42")
    service = _service(session, publisher)

    result = service.analyze("cust-42", "Car won't start in the morning.")

    assert isinstance(result, DiagnosticResponse)
    assert len(publisher.published) == 1
    assert publisher.published[0].customerId == "cust-42"
    # The event carries the same session id it was attributed to.
    assert publisher.published[0].sessionId == "s1"
