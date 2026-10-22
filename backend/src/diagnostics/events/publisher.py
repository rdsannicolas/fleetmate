"""The event-emission seam: a provider-agnostic publisher plus its EventBridge
adapter.

The diagnostics service depends on the small :class:`DiagnosisPublisher`
``Protocol`` -- never on Boto3 -- so the emission side is unit-testable with an
in-memory fake and no AWS calls (design "component independently testable with
the AWS dependencies ... mocked"). The concrete :class:`EventBridgePublisher`
is the *only* place in this module that imports and talks to the EventBridge
SDK, mirroring how :class:`FleetMateDiagnostics` isolates DynamoDB.

Emission never raises across the boundary: ``publish`` returns a typed
:class:`~shared.outcomes.Outcome`. A failed emit is reported to the caller as
``Err(PERSIST_FAILED)`` so the diagnostics service can log it and still return
the customer's diagnosis unchanged (persistence, and its emission, is off the
response path and must never block or alter the response -- Req 4.3).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Protocol

from shared.outcomes import Err, Ok, Outcome

from .diagnosis_completed import EVENT_DETAIL_TYPE, EVENT_SOURCE, DiagnosisCompleted

__all__ = [
    "DiagnosisPublisher",
    "EventBridgeClient",
    "EventBridgePublisher",
    "InMemoryPublisher",
]


class DiagnosisPublisher(Protocol):
    """What the diagnostics service needs to emit a ``DiagnosisCompleted`` event.

    Declaring this as a ``Protocol`` lets business logic depend on the capability
    rather than on EventBridge, so tests inject :class:`InMemoryPublisher` and
    production injects :class:`EventBridgePublisher`.
    """

    def publish(self, event: DiagnosisCompleted) -> Outcome[str]:
        """Emit ``event``; return ``Ok(eventId)`` or ``Err(PERSIST_FAILED)``."""
        ...


class EventBridgeClient(Protocol):
    """The narrow slice of the Boto3 EventBridge client used here.

    A fake with this shape stands in for ``boto3.client("events")`` in tests, so
    the adapter's envelope construction is verifiable without AWS.
    """

    def put_events(self, **kwargs: Any) -> dict[str, Any]: ...


class EventBridgePublisher:
    """Publishes ``DiagnosisCompleted`` events to an EventBridge bus via Boto3.

    The adapter builds the EventBridge envelope -- ``Source`` and
    ``DetailType`` are fixed so the persistence rule can match on them -- and
    serializes the event detail to a JSON string (EventBridge requires a string
    ``Detail``). ``put_events`` can partially fail without raising, so the
    adapter inspects ``FailedEntryCount`` and reports a typed failure rather than
    assuming success.
    """

    __slots__ = ("_client", "_bus_name")

    def __init__(self, client: EventBridgeClient, bus_name: str) -> None:
        self._client = client
        self._bus_name = bus_name

    def publish(self, event: DiagnosisCompleted) -> Outcome[str]:
        entry = {
            "Source": EVENT_SOURCE,
            "DetailType": EVENT_DETAIL_TYPE,
            "Detail": json.dumps(event.to_detail(), separators=(",", ":")),
            "EventBusName": self._bus_name,
        }
        try:
            resp = self._client.put_events(Entries=[entry])
        except Exception as exc:  # noqa: BLE001
            # Never let an SDK/transport error cross into business logic; the
            # emission is off the response path (Req 4.3).
            return Err("PERSIST_FAILED", "Failed to emit DiagnosisCompleted event.")

        if resp.get("FailedEntryCount", 0):
            return Err("PERSIST_FAILED", "EventBridge rejected the DiagnosisCompleted event.")

        entries = resp.get("Entries") or [{}]
        event_id = entries[0].get("EventId", "")
        return Ok(event_id)


class InMemoryPublisher:
    """A test publisher that records published events in order.

    Set ``fail=True`` (or pass a ``should_fail`` predicate) to simulate an
    emission failure and assert the service still returns the diagnosis.
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        should_fail: Callable[[DiagnosisCompleted], bool] | None = None,
    ) -> None:
        self.published: list[DiagnosisCompleted] = []
        self._fail = fail
        self._should_fail = should_fail

    def publish(self, event: DiagnosisCompleted) -> Outcome[str]:
        if self._fail or (self._should_fail is not None and self._should_fail(event)):
            return Err("PERSIST_FAILED", "Simulated emission failure.")
        self.published.append(event)
        return Ok(f"evt-{len(self.published)}")
