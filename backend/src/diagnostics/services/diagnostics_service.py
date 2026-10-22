"""The Diagnostics_Service: `analyze(...)` orchestration (Task 11.1).

This is the orchestration boundary behind the thin ``POST /diagnostics/analyze``
handler (design "Diagnostic_Assistant"). It coordinates the four cooperating
components -- the shared validator, the Conversation_Manager, the AI_Engine, and
the escalation decision -- and emits the asynchronous ``DiagnosisCompleted`` event
for off-the-response-path persistence. The handler stays thin: it parses the
request, attaches a ``correlationId``, calls :meth:`DiagnosticsService.analyze`,
and maps the returned :class:`SubmissionResult` to an HTTP response.

The orchestration order mirrors the design sequence exactly:

1. **Fail-fast validation** (Req 1.2, 1.3, 2.4). The current Symptom_Description
   is checked with the shared :func:`~shared.validation.validate_symptom` *before
   any* Conversation_Manager, AI_Engine, or storage call. On ``EMPTY``/``TOO_LONG``
   the service returns a :class:`Rejection` and makes **no** AI or storage call,
   so the Session and its context are preserved and no record is created (Req 3.2,
   Property 3).
2. **Session resolution** (Req 1.4). ``ensure_session`` creates a unique Session
   on demand when none is active, or reuses an existing one. ``SESSION_CLOSED``
   (idle-closed, Req 3.5) and ``LIMIT_REACHED`` (50-interaction cap, Req 3.4) are
   surfaced as a :class:`Rejection`.
3. **Context + generation** (Req 2.1, 2.3, 2.5, 2.6). The ascending prior context
   is read via ``get_context`` and mapped to :class:`ContextTurn` values, then the
   AI_Engine ``generate`` is invoked. ``AI_TIMEOUT``/``AI_ERROR`` are surfaced as a
   :class:`Failure`; the insufficient-info result is returned unchanged inside a
   :class:`DiagnosticResponse` (``escalated`` is still computed for it).
4. **Escalation** (Req 2.3). The result is evaluated against the three escalation
   triggers and the resulting ``escalated`` flag is placed on the response.
5. **Non-blocking persistence** (Req 4.3). A ``DiagnosisCompleted`` draft is
   emitted through the injected :class:`DiagnosisPublisher`. A publish failure is
   **logged and swallowed** -- it never changes the customer response, because
   persistence (and its emission) is off the response path. The interaction is
   also appended to the Session so the in-conversation context stays current and
   the 50-interaction limit advances.

Design constraints honoured here:

* **Dependency injection for testability.** Every collaborator -- the
  Conversation_Manager, the AI_Engine (as the provider-agnostic interface), the
  publisher, the logger factory, and the clock -- is injected, so the whole
  orchestration runs in a unit test with fakes and no AWS access (design
  "Layering and Dependency Direction").
* **Typed outcomes, never exceptions.** ``analyze`` always returns a
  :class:`SubmissionResult`; it never raises across the boundary. Collaborator
  failures arrive as :class:`~shared.outcomes.Err` values and are mapped to the
  appropriate result shape.
* **No SDK imports.** This module depends only on the service-layer interfaces and
  domain models; it never imports Boto3.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Union

from diagnostics.events import DiagnosisCompleted, DiagnosisPublisher
from diagnostics.models import DiagnosticResult, Session
from diagnostics.prompts import ContextTurn
from shared.logging import StructuredLogger, get_logger
from shared.outcomes import Err, Ok

from .ai_engine import AiEngine
from .conversation_manager import ConversationManager
from .escalation import evaluate_escalation

__all__ = [
    "DiagnosticsService",
    "SubmissionResult",
    "DiagnosticResponse",
    "Rejection",
    "Failure",
    "Vehicle",
    "SERVICE_NAME",
]

# The service name stamped on this component's structured logs (tech.md log shape).
SERVICE_NAME = "diagnostic-service"


# ---------------------------------------------------------------------------
# Result shapes (the SubmissionResult union from the design)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Vehicle:
    """Optional vehicle context supplied with a submission.

    All fields are optional -- partial vehicle details are accepted and only the
    provided fields are meaningful. The vehicle context is carried through to the
    AI prompt via the current symptom turn and is otherwise opaque to the
    orchestration.
    """

    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None


@dataclass(frozen=True, slots=True)
class DiagnosticResponse:
    """A successfully processed submission carrying the structured diagnosis.

    ``result`` is the AI_Engine's :class:`DiagnosticResult` -- either a success
    shape (one Probable_Issue + one Repair_Recommendation) or the
    insufficient-info shape (``needsMoreDetail``) returned unchanged (Req 2.5).
    ``escalated`` is the escalation flag computed from the result (Req 2.3).
    """

    sessionId: str
    result: DiagnosticResult
    escalated: bool


@dataclass(frozen=True, slots=True)
class Rejection:
    """A submission rejected before (or at) session resolution.

    ``reason`` is one of ``EMPTY``/``TOO_LONG`` (invalid symptom, before any AI or
    storage call) or ``SESSION_CLOSED``/``LIMIT_REACHED`` (session not usable). A
    rejection means nothing was generated and no record was created; the
    ``sessionId`` is populated when a session was resolved before the rejection.
    """

    reason: str
    suggestion: str = ""
    sessionId: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Failure:
    """An AI generation failure after a usable session was resolved.

    ``reason`` is ``AI_TIMEOUT`` (generation exceeded 30s, Req 2.6) or
    ``AI_ERROR`` (Guardrail block / model failure). No diagnosis is returned and
    no interaction is recorded.
    """

    reason: str
    suggestion: str = ""
    sessionId: Optional[str] = None


# A SubmissionResult is exactly one of the four shapes above.
SubmissionResult = Union[DiagnosticResponse, Rejection, Failure]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_ms(moment: datetime) -> str:
    """ISO 8601 UTC timestamp with millisecond precision (matches the store)."""

    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class DiagnosticsService:
    """Orchestrates one ``analyze`` request across the diagnostics components.

    All collaborators are injected so the orchestration is fully unit-testable
    with fakes and no AWS: ``conversation`` owns the Session lifecycle and context,
    ``ai_engine`` is the provider-agnostic diagnosis interface, ``publisher`` emits
    the ``DiagnosisCompleted`` event for non-blocking persistence, ``clock`` stamps
    the event's ``createdAt``, and ``logger_factory`` builds a per-request
    structured logger bound to the request's ``correlationId``.
    """

    __slots__ = (
        "_conversation",
        "_ai_engine",
        "_publisher",
        "_clock",
        "_logger_factory",
    )

    def __init__(
        self,
        conversation: ConversationManager,
        ai_engine: AiEngine,
        publisher: DiagnosisPublisher,
        *,
        clock: Callable[[], datetime] = _utcnow,
        logger_factory: Callable[[str, str], StructuredLogger] = get_logger,
    ) -> None:
        self._conversation = conversation
        self._ai_engine = ai_engine
        self._publisher = publisher
        self._clock = clock
        self._logger_factory = logger_factory

    # -- Public orchestration ---------------------------------------------

    def analyze(
        self,
        customer_id: str,
        symptom_text: Optional[str],
        *,
        session_id: Optional[str] = None,
        vehicle: Optional[Vehicle] = None,
        correlation_id: Optional[str] = None,
    ) -> SubmissionResult:
        """Process one Symptom_Description submission end to end.

        Returns a :class:`SubmissionResult`:

        * :class:`Rejection` -- invalid symptom (``EMPTY``/``TOO_LONG``, with no AI
          or storage side effects) or an unusable session
          (``SESSION_CLOSED``/``LIMIT_REACHED``).
        * :class:`Failure` -- AI generation ``AI_TIMEOUT``/``AI_ERROR``.
        * :class:`DiagnosticResponse` -- a success or insufficient-info diagnosis
          plus the ``escalated`` flag.

        Never raises across the boundary.
        """

        logger = self._logger_factory(
            SERVICE_NAME, correlation_id or _new_correlation_id()
        )

        # 1. Fail-fast validation BEFORE any session/AI/storage call (Req 1.2/1.3,
        #    2.4, 3.2). On rejection nothing is created and context is preserved.
        validated = self._conversation.validate_follow_up(symptom_text)
        if isinstance(validated, Err):
            logger.info(
                "Submission rejected by validation; no AI or storage call made.",
                reason=validated.code,
            )
            return Rejection(reason=validated.code, suggestion=validated.message)
        symptom = validated.value

        # 2. Resolve/create the session (Req 1.4); surface SESSION_CLOSED/
        #    LIMIT_REACHED unchanged.
        session_outcome = self._conversation.ensure_session(customer_id, session_id)
        if isinstance(session_outcome, Err):
            logger.info(
                "Submission rejected at session resolution.",
                reason=session_outcome.code,
                sessionId=session_id,
            )
            return Rejection(
                reason=session_outcome.code,
                suggestion=session_outcome.message,
                sessionId=session_id,
            )
        session = session_outcome.value

        # 3. Build ascending context and generate (Req 2.1, 2.3, 2.5, 2.6).
        context = self._build_context(session.sessionId)
        ai_outcome = self._ai_engine.generate(symptom, context)
        if isinstance(ai_outcome, Err):
            logger.error(
                "AI generation failed.",
                reason=ai_outcome.code,
                sessionId=session.sessionId,
            )
            return Failure(
                reason=ai_outcome.code,
                suggestion=ai_outcome.message,
                sessionId=session.sessionId,
            )
        result = ai_outcome.value

        # 4. Escalation decision (Req 2.3).
        decision = evaluate_escalation(result)
        if decision.escalated:
            logger.info(
                "Diagnosis escalated for human review.",
                sessionId=session.sessionId,
                reasons=",".join(decision.reasons),
            )

        # 5. Non-blocking persistence: emit the event, then append to the session.
        #    A publish failure is logged and swallowed -- it must not alter the
        #    customer response (Req 4.3).
        created_at = _iso_ms(self._clock())
        self._emit_diagnosis_completed(session, symptom, result, created_at, logger)
        self._append_interaction(session, symptom, result, created_at, logger)

        logger.info(
            "Vehicle diagnosis completed.",
            sessionId=session.sessionId,
            escalated=decision.escalated,
            needsMoreDetail=result.needsMoreDetail,
        )
        return DiagnosticResponse(
            sessionId=session.sessionId,
            result=result,
            escalated=decision.escalated,
        )

    # -- Internals ---------------------------------------------------------

    def _build_context(self, session_id: str) -> list[ContextTurn]:
        """Map the Session's ascending Interaction_Records to prompt context.

        ``get_context`` already returns records ascending by ``createdAt`` (Req
        3.1, 3.3). Each record's prior reply text is taken from the stored
        :class:`DiagnosticResult` -- the Probable_Issue + Repair_Recommendation for
        a success, or the ``message`` for an insufficient-info turn -- so the model
        sees a faithful ascending transcript.
        """

        turns: list[ContextTurn] = []
        for record in self._conversation.get_context(session_id):
            turns.append(
                ContextTurn(
                    symptom=record.symptomDescription,
                    diagnosis=_diagnosis_text(record.result),
                )
            )
        return turns

    def _emit_diagnosis_completed(
        self,
        session: Session,
        symptom: str,
        result: DiagnosticResult,
        created_at: str,
        logger: StructuredLogger,
    ) -> None:
        """Emit the ``DiagnosisCompleted`` draft; log and swallow any failure.

        Emission is off the response path (Req 4.3): if the publisher reports an
        error -- or even raises -- the failure is logged with the ``sessionId`` and
        timestamp and the customer response is returned unchanged.
        """

        event = DiagnosisCompleted(
            sessionId=session.sessionId,
            symptomDescription=symptom,
            result=result,
            createdAt=created_at,
            customerId=session.customerId,
        )
        try:
            outcome = self._publisher.publish(event)
        except Exception:  # noqa: BLE001 -- emission never breaks the response.
            logger.error(
                "DiagnosisCompleted emission raised; persistence may be delayed.",
                sessionId=session.sessionId,
                timestamp=created_at,
            )
            return
        if isinstance(outcome, Err):
            logger.error(
                "DiagnosisCompleted emission failed; persistence may be delayed.",
                sessionId=session.sessionId,
                timestamp=created_at,
                reason=outcome.code,
            )

    def _append_interaction(
        self,
        session: Session,
        symptom: str,
        result: DiagnosticResult,
        created_at: str,
        logger: StructuredLogger,
    ) -> None:
        """Append the interaction to the Session, keeping in-conversation context.

        This advances the Session's ``lastSubmissionAt``/``interactionCount`` so
        the next turn sees this exchange as context and the 50-interaction limit
        progresses. A failure here is logged and swallowed: like emission, it is
        off the response path and must never alter the customer's diagnosis
        (Req 4.3). The same ``createdAt`` as the emitted event is used so the
        in-conversation record and the asynchronously persisted record align.
        """

        appended = self._conversation.append_interaction(
            session.sessionId, symptom, result, created_at=created_at
        )
        if isinstance(appended, Err):
            logger.error(
                "Appending interaction to session failed; context may lag.",
                sessionId=session.sessionId,
                timestamp=created_at,
                reason=appended.code,
            )


def _diagnosis_text(result: DiagnosticResult) -> str:
    """Render a prior turn's diagnosis as a single context string.

    For a success shape this is the Probable_Issue and Repair_Recommendation; for
    an insufficient-info shape it is the ``message`` asking for more detail. The
    text is sanitized downstream by the prompt builder before it reaches the model.
    """

    if result.needsMoreDetail:
        return result.message or "More detail was requested."
    issue = result.probableIssue or ""
    recommendation = result.repairRecommendation or ""
    parts = [part for part in (issue, recommendation) if part]
    return " - ".join(parts) if parts else "No diagnosis was produced."


def _new_correlation_id() -> str:
    """Fallback correlation id when the handler did not supply one."""

    return str(uuid.uuid4())
