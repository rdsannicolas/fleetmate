"""Event emission and the asynchronous persistence Lambda (EventBridge -> SQS).

The write side of the Interaction_Store is event-driven: the diagnostics service
emits a :class:`DiagnosisCompleted` event through a provider-agnostic
:class:`DiagnosisPublisher` (the :class:`EventBridgePublisher` adapter isolates
Boto3), EventBridge routes it to an SQS queue, and the :class:`PersistenceLambda`
consumes the queue and writes the Interaction_Record via the repository -- with a
single retry inside 5 seconds and DLQ routing on repeated failure. This keeps all
persistence off the customer response path (Req 4.1, 4.2, 4.3).
"""

from __future__ import annotations

from .diagnosis_completed import (
    EVENT_DETAIL_TYPE,
    EVENT_SOURCE,
    DiagnosisCompleted,
)
from .persistence_lambda import (
    RETRY_DELAY_SECONDS,
    SERVICE_NAME,
    PersistenceLambda,
    Sleeper,
    persist_draft,
)
from .publisher import (
    DiagnosisPublisher,
    EventBridgeClient,
    EventBridgePublisher,
    InMemoryPublisher,
)

__all__ = [
    "DiagnosisCompleted",
    "EVENT_SOURCE",
    "EVENT_DETAIL_TYPE",
    "DiagnosisPublisher",
    "EventBridgeClient",
    "EventBridgePublisher",
    "InMemoryPublisher",
    "PersistenceLambda",
    "Sleeper",
    "RETRY_DELAY_SECONDS",
    "SERVICE_NAME",
    "persist_draft",
]
