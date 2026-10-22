"""Service-layer orchestration for the diagnostics domain."""

from __future__ import annotations

from .ai_engine import (
    APPROVED_MODEL_PREFIXES,
    GENERATION_TIMEOUT_SECONDS,
    AiEngine,
    AiOutcome,
    BedrockAiEngine,
    BedrockInvoker,
    BedrockRuntimeInvoker,
    CircuitBreaker,
    CircuitBreakerError,
    insufficient_info,
)
from .conversation_manager import (
    IDLE_TIMEOUT,
    MAX_INTERACTIONS,
    ConversationManager,
    new_session_id,
)
from .diagnostics_service import (
    SERVICE_NAME,
    DiagnosticResponse,
    DiagnosticsService,
    Failure,
    Rejection,
    SubmissionResult,
    Vehicle,
)
from .escalation import (
    CONFIDENCE_THRESHOLD,
    EscalationDecision,
    evaluate_escalation,
    should_escalate,
)
from .interaction_store import InteractionStore

__all__ = [
    "AiEngine",
    "AiOutcome",
    "APPROVED_MODEL_PREFIXES",
    "BedrockAiEngine",
    "BedrockInvoker",
    "BedrockRuntimeInvoker",
    "CircuitBreaker",
    "CircuitBreakerError",
    "CONFIDENCE_THRESHOLD",
    "ConversationManager",
    "DiagnosticResponse",
    "DiagnosticsService",
    "EscalationDecision",
    "Failure",
    "GENERATION_TIMEOUT_SECONDS",
    "IDLE_TIMEOUT",
    "InteractionStore",
    "MAX_INTERACTIONS",
    "Rejection",
    "SERVICE_NAME",
    "SubmissionResult",
    "Vehicle",
    "evaluate_escalation",
    "insufficient_info",
    "new_session_id",
    "should_escalate",
]
