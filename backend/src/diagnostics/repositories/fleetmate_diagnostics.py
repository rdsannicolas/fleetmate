"""The ``FleetMateDiagnostics`` DynamoDB single-table repository.

This module is the *only* place in the codebase that imports and talks to
Boto3/DynamoDB. Callers (the Conversation_Manager and the Interaction_Store
service layer) depend on this repository's typed methods and never import the
AWS SDK themselves, keeping DynamoDB a replaceable implementation detail
(design: "Store owns identity, time ... Boto3 isolated behind this layer").

Single-table layout (design "DynamoDB Single-Table Design"):

| Item type         | PK                     | SK                           |
|-------------------|------------------------|------------------------------|
| Session           | ``SESSION#{sessionId}``| ``META``                     |
| InteractionRecord | ``SESSION#{sessionId}``| ``REC#{createdAt}#{recordId}``|

* The InteractionRecord sort key embeds the UTC-millisecond ``createdAt`` so a
  ``begins_with("REC#")`` query with ``ScanIndexForward=True`` yields records in
  ascending chronological order (Req 3.3, 5.1).
* ``GSI1`` (``GSI1PK`` / ``GSI1SK = {createdAt}#{recordId}``) supports analyst
  date-range queries ordered by creation timestamp (Req 4.4).
* Session items carry a ``ttl`` attribute for DynamoDB TTL idle cleanup.

Identity and time are owned here: record ids are generated as UUID v4 (Req 4.2)
and ``createdAt`` is stamped in ISO 8601 UTC with millisecond precision (Req 4.1)
unless the caller supplies an explicit value.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from diagnostics.models import (
    DiagnosticResult,
    InteractionRecord,
    Page,
    Session,
    SessionStatus,
)
from shared.outcomes import Err, Ok, Outcome

__all__ = [
    "FleetMateDiagnostics",
    "DynamoClient",
    "GSI1_INDEX_NAME",
    "GSI1_PARTITION_VALUE",
    "GSI2_INDEX_NAME",
    "HISTORY_PAGE_SIZE",
    "format_timestamp",
    "new_record_id",
]

# ---------------------------------------------------------------------------
# Table / index constants
# ---------------------------------------------------------------------------

GSI1_INDEX_NAME = "GSI1"
# All interaction records share one GSI partition so the analyst date-range
# query is a single ordered range scan over GSI1SK = "{createdAt}#{recordId}".
GSI1_PARTITION_VALUE = "DIAG"

GSI2_INDEX_NAME = "GSI2"
# GSI2 partitions records by owning Customer (``GSI2PK = CUSTOMER#{customerId}``)
# with ``GSI2SK = {createdAt}#{recordId}`` so the owner-scoped newest-first
# "most-recent prior interaction" lookup is a single descending range scan
# (Req 13.1, 13.3, 13.4). These key attributes are written forward-only: only
# records persisted with a known ``customerId`` carry them.
_GSI2_PARTITION_PREFIX = "CUSTOMER#"

HISTORY_PAGE_SIZE = 50  # Req 5.5: at most 50 records per history page.

_SESSION_SK = "META"
_RECORD_SK_PREFIX = "REC#"


# ---------------------------------------------------------------------------
# Boto3 client seam
# ---------------------------------------------------------------------------


class DynamoClient(Protocol):
    """The narrow slice of the Boto3 DynamoDB client this repository uses.

    Declaring it as a ``Protocol`` lets tests pass a faked in-memory client with
    the same shape -- no real AWS calls, no ``boto3`` import in the test -- while
    production code passes a real ``boto3.client("dynamodb")``.
    """

    def put_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def query(self, **kwargs: Any) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Identity & time helpers (owned by the store)
# ---------------------------------------------------------------------------


def new_record_id() -> str:
    """Return a fresh globally unique UUID v4 string (Req 4.2)."""

    return str(uuid.uuid4())


def format_timestamp(moment: datetime) -> str:
    """Format ``moment`` as ISO 8601 UTC with millisecond precision (Req 4.1).

    The result always ends in ``Z`` and carries exactly three fractional-second
    digits (e.g. ``2024-01-01T00:00:00.123Z``) so lexical ordering of the string
    matches chronological ordering -- which the sort-key design relies on.
    """

    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_conditional_check_failure(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` represents a DynamoDB conditional failure.

    Matches both the real Boto3 ``ConditionalCheckFailedException`` (identified
    by its exception class name, so no SDK import is required) and the test
    fake's equivalent error.
    """

    name = type(exc).__name__
    if name in {"ConditionalCheckFailedException", "FakeDynamoError"}:
        return True
    # Boto3 raises a generic ``ClientError`` whose error code lives in a response
    # mapping; inspect it defensively without importing botocore.
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        return code == "ConditionalCheckFailedException"
    return False


# ---------------------------------------------------------------------------
# Item (de)serialization -- DynamoDB attribute-value maps
# ---------------------------------------------------------------------------


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: float | int) -> dict[str, str]:
    return {"N": str(value)}


def _session_pk(session_id: str) -> str:
    return f"SESSION#{session_id}"


def _record_sk(created_at: str, record_id: str) -> str:
    return f"{_RECORD_SK_PREFIX}{created_at}#{record_id}"


def _gsi2_pk(customer_id: str) -> str:
    return f"{_GSI2_PARTITION_PREFIX}{customer_id}"


def _result_to_item(result: DiagnosticResult) -> dict[str, Any]:
    """Serialize a :class:`DiagnosticResult` into a DynamoDB map attribute."""

    fields: dict[str, Any] = {"needsMoreDetail": {"BOOL": result.needsMoreDetail}}
    if result.probableIssue is not None:
        fields["probableIssue"] = _s(result.probableIssue)
    if result.repairRecommendation is not None:
        fields["repairRecommendation"] = _s(result.repairRecommendation)
    if result.estimatedCost is not None:
        fields["estimatedCost"] = _s(result.estimatedCost)
    if result.confidenceScore is not None:
        fields["confidenceScore"] = _n(result.confidenceScore)
    if result.message is not None:
        fields["message"] = _s(result.message)
    return {"M": fields}


def _result_from_item(attr: dict[str, Any]) -> DiagnosticResult:
    fields = attr["M"]

    def opt_s(key: str) -> str | None:
        return fields[key]["S"] if key in fields else None

    confidence = (
        float(fields["confidenceScore"]["N"]) if "confidenceScore" in fields else None
    )
    return DiagnosticResult(
        needsMoreDetail=fields["needsMoreDetail"]["BOOL"],
        probableIssue=opt_s("probableIssue"),
        repairRecommendation=opt_s("repairRecommendation"),
        estimatedCost=opt_s("estimatedCost"),
        confidenceScore=confidence,
        message=opt_s("message"),
    )


def _session_to_item(session: Session) -> dict[str, Any]:
    item: dict[str, Any] = {
        "PK": _s(_session_pk(session.sessionId)),
        "SK": _s(_SESSION_SK),
        "entityType": _s("Session"),
        "sessionId": _s(session.sessionId),
        "customerId": _s(session.customerId),
        "status": _s(session.status.value),
        "createdAt": _s(session.createdAt),
        "lastSubmissionAt": _s(session.lastSubmissionAt),
        "interactionCount": _n(session.interactionCount),
    }
    if session.ttl is not None:
        item["ttl"] = _n(session.ttl)
    return item


def _session_from_item(item: dict[str, Any]) -> Session:
    return Session(
        sessionId=item["sessionId"]["S"],
        customerId=item["customerId"]["S"],
        status=SessionStatus(item["status"]["S"]),
        createdAt=item["createdAt"]["S"],
        lastSubmissionAt=item["lastSubmissionAt"]["S"],
        interactionCount=int(item["interactionCount"]["N"]),
        ttl=int(item["ttl"]["N"]) if "ttl" in item else None,
    )


def _record_to_item(record: InteractionRecord) -> dict[str, Any]:
    item: dict[str, Any] = {
        "PK": _s(_session_pk(record.sessionId)),
        "SK": _s(_record_sk(record.createdAt, record.recordId)),
        "entityType": _s("InteractionRecord"),
        "recordId": _s(record.recordId),
        "sessionId": _s(record.sessionId),
        "symptomDescription": _s(record.symptomDescription),
        "result": _result_to_item(record.result),
        "createdAt": _s(record.createdAt),
        # GSI1 enables the analyst date-range query ordered by timestamp.
        "GSI1PK": _s(GSI1_PARTITION_VALUE),
        "GSI1SK": _s(f"{record.createdAt}#{record.recordId}"),
    }
    # Forward-only owner attribution: only records that know their owning
    # Customer carry ``customerId`` and the GSI2 keys that back the owner-scoped
    # newest-first lookup (Req 13.1, 13.3, 13.4). Records without a customerId
    # (e.g. pre-existing items) simply omit these attributes and never appear in
    # the GSI2 partition -- there is deliberately no backfill.
    if record.customerId is not None:
        item["customerId"] = _s(record.customerId)
        item["GSI2PK"] = _s(_gsi2_pk(record.customerId))
        item["GSI2SK"] = _s(f"{record.createdAt}#{record.recordId}")
    return item


def _record_from_item(item: dict[str, Any]) -> InteractionRecord:
    return InteractionRecord(
        recordId=item["recordId"]["S"],
        sessionId=item["sessionId"]["S"],
        symptomDescription=item["symptomDescription"]["S"],
        result=_result_from_item(item["result"]),
        createdAt=item["createdAt"]["S"],
        # Tolerant read: older records predate owner attribution and have no
        # ``customerId`` attribute, so default to ``None`` rather than raising.
        customerId=item["customerId"]["S"] if "customerId" in item else None,
    )


# ---------------------------------------------------------------------------
# Opaque pagination token (DynamoDB ExclusiveStartKey <-> string)
# ---------------------------------------------------------------------------


def _encode_token(last_key: dict[str, Any] | None) -> str | None:
    """Encode a DynamoDB ``LastEvaluatedKey`` into an opaque page token.

    The token round-trips the two string key attributes of a record item
    (``PK``/``SK``). It is intentionally opaque to callers (design: Page token is
    "an opaque encoding of the DynamoDB ExclusiveStartKey").
    """

    if not last_key:
        return None
    pk = last_key["PK"]["S"]
    sk = last_key["SK"]["S"]
    # SK already encodes "REC#{createdAt}#{recordId}" and never contains '|';
    # the PK is reconstructable from the session but we carry it to be explicit.
    return f"{pk}|{sk}"


def _decode_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    pk, _, sk = token.partition("|")
    return {"PK": _s(pk), "SK": _s(sk)}


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class FleetMateDiagnostics:
    """Single-table repository for Session and InteractionRecord items.

    All DynamoDB access flows through the injected :class:`DynamoClient`, so this
    class is the sole Boto3 boundary. Methods return typed
    :class:`~shared.outcomes.Outcome` values rather than raising across
    boundaries, matching the project's typed-outcome error model.
    """

    def __init__(
        self,
        client: DynamoClient,
        table_name: str,
        *,
        clock: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], str] = new_record_id,
    ) -> None:
        self._client = client
        self._table = table_name
        # Injectable so tests can use a fake clock (Property 9) and deterministic
        # ids without monkeypatching module globals.
        self._clock = clock
        self._new_id = id_factory

    # -- Session CRUD ------------------------------------------------------

    def put_session(self, session: Session) -> Outcome[Session]:
        """Create or overwrite a Session item. Returns the stored Session."""

        self._client.put_item(
            TableName=self._table, Item=_session_to_item(session)
        )
        return Ok(session)

    def get_session(self, session_id: str) -> Outcome[Session]:
        """Fetch a Session by id, or ``Err(NOT_FOUND)`` if it does not exist."""

        resp = self._client.get_item(
            TableName=self._table,
            Key={"PK": _s(_session_pk(session_id)), "SK": _s(_SESSION_SK)},
        )
        item = resp.get("Item")
        if not item:
            return Err("NOT_FOUND", "Session not found.")
        return Ok(_session_from_item(item))

    def update_session(
        self,
        session_id: str,
        *,
        status: SessionStatus | None = None,
        last_submission_at: str | None = None,
        interaction_count: int | None = None,
        ttl: int | None = None,
    ) -> Outcome[Session]:
        """Patch mutable Session attributes; returns the updated Session.

        Only the provided fields are written. ``Err(NOT_FOUND)`` is returned when
        no Session item exists for ``session_id`` (via a conditional update).
        """

        set_parts: list[str] = []
        names: dict[str, str] = {}
        values: dict[str, Any] = {}

        def add(attr: str, value: dict[str, Any]) -> None:
            placeholder_name = f"#{attr}"
            placeholder_value = f":{attr}"
            set_parts.append(f"{placeholder_name} = {placeholder_value}")
            names[placeholder_name] = attr
            values[placeholder_value] = value

        if status is not None:
            add("status", _s(status.value))
        if last_submission_at is not None:
            add("lastSubmissionAt", _s(last_submission_at))
        if interaction_count is not None:
            add("interactionCount", _n(interaction_count))
        if ttl is not None:
            add("ttl", _n(ttl))

        if not set_parts:
            # Nothing to change -- just return the current session.
            return self.get_session(session_id)

        try:
            resp = self._client.update_item(
                TableName=self._table,
                Key={"PK": _s(_session_pk(session_id)), "SK": _s(_SESSION_SK)},
                UpdateExpression="SET " + ", ".join(set_parts),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(PK)",
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001
            # The ``attribute_exists(PK)`` guard fails when no Session item
            # exists. Boto3 raises ``ConditionalCheckFailedException``; the fake
            # client raises its own error. Translate either into a typed
            # ``NOT_FOUND`` outcome rather than letting it cross the boundary.
            if _is_conditional_check_failure(exc):
                return Err("NOT_FOUND", "Session not found.")
            raise

        attrs = resp.get("Attributes")
        if not attrs:
            return self.get_session(session_id)
        return Ok(_session_from_item(attrs))

    # -- Interaction records ----------------------------------------------

    def append_record(
        self,
        session_id: str,
        symptom_description: str,
        result: DiagnosticResult,
        *,
        customer_id: str | None = None,
        created_at: str | None = None,
        record_id: str | None = None,
    ) -> Outcome[InteractionRecord]:
        """Persist a new InteractionRecord and return it.

        The store owns identity and time: a UUID v4 ``recordId`` is minted
        (Req 4.2) and ``createdAt`` is stamped in UTC-millisecond ISO 8601
        (Req 4.1) unless the caller supplies explicit values (used by tests with
        a fake clock).

        When ``customer_id`` (the owning Customer's Cognito ``sub``) is supplied,
        the persisted item additionally carries ``customerId`` and the GSI2 key
        attributes (``GSI2PK = CUSTOMER#{customerId}``,
        ``GSI2SK = {createdAt}#{recordId}``) that back the owner-scoped
        most-recent-prior lookup (Req 13.1, 13.3, 13.4). This is forward-only:
        omitting ``customer_id`` writes a record exactly as before, and no
        existing records are touched or backfilled.
        """

        stamp = created_at if created_at is not None else format_timestamp(self._clock())
        rid = record_id if record_id is not None else self._new_id()
        record = InteractionRecord(
            recordId=rid,
            sessionId=session_id,
            symptomDescription=symptom_description,
            result=result,
            createdAt=stamp,
            customerId=customer_id,
        )
        self._client.put_item(TableName=self._table, Item=_record_to_item(record))
        return Ok(record)

    def get_session_history(
        self, session_id: str, page_token: str | None = None
    ) -> Outcome[Page]:
        """Return one ascending page (<=50) of a Session's InteractionRecords.

        Records are queried ``begins_with(SK, "REC#")`` with
        ``ScanIndexForward=True`` so they come back in ascending creation-time
        order (Req 3.3, 5.1), and ``Limit=50`` caps the page (Req 5.5). An opaque
        ``nextPageToken`` is returned only when more records remain.

        A known-but-empty Session yields an empty page (Req 5.2); callers that
        must distinguish an unknown Session (Req 5.3) check :meth:`get_session`.
        """

        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :rec)",
            "ExpressionAttributeValues": {
                ":pk": _s(_session_pk(session_id)),
                ":rec": _s(_RECORD_SK_PREFIX),
            },
            "ScanIndexForward": True,
            "Limit": HISTORY_PAGE_SIZE,
        }
        start_key = _decode_token(page_token)
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key

        resp = self._client.query(**kwargs)
        records = tuple(_record_from_item(i) for i in resp.get("Items", []))
        next_token = _encode_token(resp.get("LastEvaluatedKey"))
        return Ok(Page(records=records, nextPageToken=next_token))

    def query_most_recent_prior(
        self,
        customer_id: str,
        current_session_id: str,
        limit: int = 2,
    ) -> InteractionRecord | None:
        """Return the Customer's most-recent InteractionRecord outside the session.

        Resolves the Most_Recent_Prior_Interaction (Req 13.1, 13.3): the single
        record owned by ``customer_id`` whose ``sessionId`` differs from
        ``current_session_id`` with the latest ``createdAt``, breaking ties on the
        greater ``recordId``. Returns ``None`` when the Customer owns no such
        record (Req 13.5).

        The query runs over ``GSI2`` (``GSI2PK = CUSTOMER#{customerId}``,
        ``GSI2SK = {createdAt}#{recordId}``) with ``ScanIndexForward=False`` so
        the owner's records come back newest-first; descending ``GSI2SK`` order
        already encodes the "(createdAt, recordId) descending" tie-break. A small
        ``Limit`` (default 2) is enough because at most the first record can
        belong to ``current_session_id`` -- skipping it leaves the newest Prior
        Interaction next in line.

        Unlike the other read methods this returns ``InteractionRecord | None``
        directly (per the task's stated signature); the owner-scope
        authorization that precedes it lives in the service layer.
        """

        resp = self._client.query(
            TableName=self._table,
            IndexName=GSI2_INDEX_NAME,
            KeyConditionExpression="GSI2PK = :pk",
            ExpressionAttributeValues={":pk": _s(_gsi2_pk(customer_id))},
            ScanIndexForward=False,
            Limit=limit,
        )
        for item in resp.get("Items", []):
            record = _record_from_item(item)
            if record.sessionId == current_session_id:
                continue
            return record
        return None

    def query_by_date_range(self, start: str, end: str) -> Outcome[list[InteractionRecord]]:
        """Return InteractionRecords with ``start <= createdAt <= end`` ascending.

        Runs over ``GSI1`` keyed by ``GSI1SK = "{createdAt}#{recordId}"`` using a
        ``BETWEEN`` range on the inclusive bounds, ``ScanIndexForward=True`` for
        ascending order (Req 4.4). The upper bound is widened with a high
        code-point suffix so every ``recordId`` sharing the ``end`` timestamp is
        included (BETWEEN is inclusive but ``"{end}"`` alone sorts before
        ``"{end}#..."``).

        Inverted ranges (``start > end``) are rejected with ``INVALID_RANGE`` and
        no records (Req 4.6).

        Note: authorization (analyst RBAC) is enforced by the service layer
        before this method is called; the repository is the authless data seam.
        """

        if start > end:
            return Err("INVALID_RANGE", "The date range start must not be after its end.")

        items: list[dict[str, Any]] = []
        exclusive_start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table,
                "IndexName": GSI1_INDEX_NAME,
                "KeyConditionExpression": (
                    "GSI1PK = :pk AND GSI1SK BETWEEN :start AND :end"
                ),
                "ExpressionAttributeValues": {
                    ":pk": _s(GSI1_PARTITION_VALUE),
                    ":start": _s(start),
                    # '\uffff' ensures "{end}#<any recordId>" <= upper bound.
                    ":end": _s(f"{end}#\uffff"),
                },
                "ScanIndexForward": True,
            }
            if exclusive_start_key is not None:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            resp = self._client.query(**kwargs)
            items.extend(resp.get("Items", []))
            exclusive_start_key = resp.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        return Ok([_record_from_item(i) for i in items])
