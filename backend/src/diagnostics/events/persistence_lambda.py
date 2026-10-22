"""The dedicated persistence Lambda: SQS -> DynamoDB write of Interaction_Records.

This is the consumer end of the asynchronous write path
(EventBridge -> SQS -> this Lambda, design "Non-blocking, event-driven
persistence"). It is deliberately off the customer response thread, so nothing
here can block or alter a customer's diagnosis (Req 4.3).

Per-record behavior (design "Write side"):

* Parse the SQS message body into a :class:`DiagnosisCompleted` draft and write
  it through :meth:`FleetMateDiagnostics.append_record`.
* On a failed write, retry **once within 5 seconds**. The wait and the clock are
  injected (:class:`Sleeper` / ``clock``) so tests exercise the retry timing
  without actually sleeping.
* If the retry also fails (or the message is unparseable), the record is **not**
  partially written -- ``append_record`` writes a single item atomically, so a
  failure leaves nothing behind (Req 4.3) -- the failure is logged with the
  ``sessionId`` and the ``createdAt`` timestamp, and the message is routed to the
  DLQ by reporting it as a batch item failure (SQS partial-batch-response), which
  lets SQS redrive it after the configured attempts.

The handler uses the SQS *partial batch response* contract: it returns
``{"batchItemFailures": [{"itemIdentifier": <messageId>}, ...]}`` so only the
failed messages are retried/redriven while successful ones are deleted. This is
how a single poisoned message is sent to the DLQ without re-processing the whole
batch.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from diagnostics.events.diagnosis_completed import DiagnosisCompleted
from diagnostics.repositories import FleetMateDiagnostics
from shared.logging import StructuredLogger, get_logger
from shared.outcomes import Err, Ok, Outcome, is_err

__all__ = [
    "RETRY_DELAY_SECONDS",
    "SERVICE_NAME",
    "Sleeper",
    "PersistenceLambda",
    "persist_draft",
]

# Req 4.3 / design "retries a failed write once within 5 seconds": the single
# retry must happen inside a 5-second window. We wait a short, bounded delay well
# under that budget before the one retry.
RETRY_DELAY_SECONDS = 1.0

SERVICE_NAME = "diagnostic-persistence"


class Sleeper(Protocol):
    """A blocking wait of ``seconds``; injected so tests never actually sleep."""

    def __call__(self, seconds: float) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_ms(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def persist_draft(
    repository: FleetMateDiagnostics, draft: DiagnosisCompleted
) -> Outcome[str]:
    """Write one Interaction_Record draft, returning ``Ok(recordId)`` or ``Err``.

    Delegates identity (UUID v4) and storage to the repository and reuses the
    draft's ``createdAt`` so the persisted timestamp reflects when the diagnosis
    completed (Req 4.1). The draft's ``customerId`` is forwarded so a newly
    persisted record is owner-attributed with its GSI2 keys for the owner-scoped
    most-recent-prior lookup (Req 13.1, 13.4); a draft lacking it persists
    exactly as before (forward-only). Any repository exception is translated into
    a typed ``Err(PERSIST_FAILED)`` so the caller's retry/DLQ logic sees a value,
    not an exception crossing the boundary.
    """

    try:
        outcome = repository.append_record(
            draft.sessionId,
            draft.symptomDescription,
            draft.result,
            customer_id=draft.customerId,
            created_at=draft.createdAt,
        )
    except Exception:  # noqa: BLE001
        return Err("PERSIST_FAILED", "Interaction record write failed.")

    if is_err(outcome):
        return Err("PERSIST_FAILED", "Interaction record write failed.")
    return Ok(outcome.value.recordId)  # type: ignore[union-attr]


class PersistenceLambda:
    """Consumes SQS messages and persists Interaction_Records with retry + DLQ.

    All collaborators are injected (repository, sleeper, clock, logger factory)
    so the whole handler runs in a unit test with a fake DynamoDB client and no
    real waiting or AWS access.
    """

    __slots__ = ("_repository", "_sleep", "_clock", "_logger_factory")

    def __init__(
        self,
        repository: FleetMateDiagnostics,
        *,
        sleeper: Sleeper = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
        logger_factory: Callable[[str, str], StructuredLogger] = get_logger,
    ) -> None:
        self._repository = repository
        self._sleep = sleeper
        self._clock = clock
        self._logger_factory = logger_factory

    def handle(self, event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        """Process an SQS batch; return the SQS partial-batch-failure response.

        Each record is persisted independently. A record whose write fails even
        after the single retry (or whose body is unparseable) is reported in
        ``batchItemFailures`` so SQS redrives just that message toward the DLQ;
        the rest of the batch is acknowledged and deleted.
        """

        failures: list[dict[str, str]] = []
        for record in event.get("Records", []):
            message_id = record.get("messageId", "")
            outcome = self._process_record(record)
            if is_err(outcome):
                failures.append({"itemIdentifier": message_id})

        return {"batchItemFailures": failures}

    # -- internals ---------------------------------------------------------

    def _process_record(self, record: dict[str, Any]) -> Outcome[str]:
        correlation_id = self._correlation_id(record)
        logger = self._logger_factory(SERVICE_NAME, correlation_id)

        draft = self._parse(record)
        if draft is None:
            # Unparseable message: there is no record to write (no partial
            # record) and no sessionId to attribute, but the failure must still
            # surface so SQS can redrive to the DLQ (Req 4.3).
            logger.error(
                "Discarded unparseable DiagnosisCompleted message; routing to DLQ.",
                timestamp=_iso_ms(self._clock()),
            )
            return Err("PERSIST_FAILED", "Unparseable message.")

        # First attempt.
        first = persist_draft(self._repository, draft)
        if isinstance(first, Ok):
            return first

        # Retry exactly once, within the 5-second window (Req 4.3).
        self._sleep(RETRY_DELAY_SECONDS)
        second = persist_draft(self._repository, draft)
        if isinstance(second, Ok):
            return second

        # Repeated failure: no partial record was written (append_record writes a
        # single item atomically). Log with sessionId + timestamp and signal DLQ
        # routing to the caller (Req 4.3).
        logger.error(
            "Interaction record persistence failed after retry; routing to DLQ.",
            sessionId=draft.sessionId,
            timestamp=draft.createdAt,
        )
        return Err("PERSIST_FAILED", "Persistence failed after one retry.")

    @staticmethod
    def _parse(record: dict[str, Any]) -> DiagnosisCompleted | None:
        """Decode the SQS message body into a ``DiagnosisCompleted`` draft.

        The body is the JSON EventBridge event; its ``detail`` holds the draft.
        A plain ``detail`` dict (no EventBridge envelope) is also accepted so the
        same handler works for direct SQS producers. Returns ``None`` for any
        malformed body so the caller treats it as a failure (DLQ), never a
        partial write.
        """

        body = record.get("body")
        if not isinstance(body, str):
            return None
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None

        detail = payload.get("detail", payload)
        if not isinstance(detail, dict):
            return None
        try:
            return DiagnosisCompleted.from_detail(detail)
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _correlation_id(record: dict[str, Any]) -> str:
        """Derive a correlation id for the record's logs.

        Prefers an SQS message attribute if the producer set one, else falls back
        to the ``messageId`` so every log line for this message is correlatable.
        """

        attrs = record.get("messageAttributes") or {}
        corr = attrs.get("correlationId")
        if isinstance(corr, dict):
            value = corr.get("stringValue")
            if isinstance(value, str) and value:
                return value
        message_id = record.get("messageId")
        return message_id if isinstance(message_id, str) and message_id else "unknown"
