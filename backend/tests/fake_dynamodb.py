"""A tiny in-memory fake of the Boto3 DynamoDB client for repository tests.

It implements only the operations and features the ``FleetMateDiagnostics``
repository uses: ``put_item``, ``get_item``, ``update_item`` (SET + a simple
``attribute_exists(PK)`` condition), and ``query`` (base-table ``begins_with``
range on SK, GSI1 ``BETWEEN`` range on GSI1SK, GSI2 ``GSI2PK`` owner-partition
equality, ``ScanIndexForward``, ``Limit`` with
``LastEvaluatedKey``/``ExclusiveStartKey`` pagination).

Items are stored exactly as the DynamoDB attribute-value maps the repository
produces, so this seam proves the repository's (de)serialization and key
construction without importing ``boto3`` or touching AWS. No business logic
lives here -- it is purely a faithful storage/query stand-in.
"""

from __future__ import annotations

from typing import Any


class FakeDynamoError(Exception):
    """Raised to mimic a conditional-check failure in ``update_item``."""


class FakeDynamoClient:
    """In-memory DynamoDB stand-in keyed by ``(PK, SK)`` string tuples."""

    def __init__(self) -> None:
        # key: (pk, sk) -> item (attribute-value map)
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _key_tuple(key: dict[str, Any]) -> tuple[str, str]:
        return (key["PK"]["S"], key["SK"]["S"])

    # -- API surface used by the repository --------------------------------

    def put_item(self, *, TableName: str, Item: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(Item["PK"]["S"], Item["SK"]["S"])] = dict(Item)
        return {}

    def get_item(self, *, TableName: str, Key: dict[str, Any], **_: Any) -> dict[str, Any]:
        item = self._items.get(self._key_tuple(Key))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(
        self,
        *,
        TableName: str,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeNames: dict[str, str],
        ExpressionAttributeValues: dict[str, Any],
        ConditionExpression: str | None = None,
        ReturnValues: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        key = self._key_tuple(Key)
        existing = self._items.get(key)
        if ConditionExpression == "attribute_exists(PK)" and existing is None:
            raise FakeDynamoError("ConditionalCheckFailed")

        item = dict(existing) if existing is not None else {
            "PK": Key["PK"],
            "SK": Key["SK"],
        }

        # Parse "SET #a = :a, #b = :b" into attribute assignments.
        assert UpdateExpression.startswith("SET ")
        assignments = UpdateExpression[len("SET "):].split(", ")
        for assignment in assignments:
            name_ph, value_ph = (part.strip() for part in assignment.split("="))
            attr = ExpressionAttributeNames[name_ph]
            item[attr] = ExpressionAttributeValues[value_ph]

        self._items[key] = item
        if ReturnValues == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    def query(
        self,
        *,
        TableName: str,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        IndexName: str | None = None,
        ScanIndexForward: bool = True,
        Limit: int | None = None,
        ExclusiveStartKey: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        if IndexName == "GSI1":
            matched = self._query_gsi1(ExpressionAttributeValues)
            sort_key = lambda it: it["GSI1SK"]["S"]  # noqa: E731
            cursor_of = lambda it: (it["GSI1SK"]["S"],)  # noqa: E731
            start_cursor = (
                (ExclusiveStartKey["GSI1SK"]["S"],) if ExclusiveStartKey else None
            )
        elif IndexName == "GSI2":
            matched = self._query_gsi2(ExpressionAttributeValues)
            sort_key = lambda it: it["GSI2SK"]["S"]  # noqa: E731
            cursor_of = lambda it: (it["GSI2SK"]["S"],)  # noqa: E731
            start_cursor = (
                (ExclusiveStartKey["GSI2SK"]["S"],) if ExclusiveStartKey else None
            )
        else:
            matched = self._query_base(ExpressionAttributeValues)
            sort_key = lambda it: it["SK"]["S"]  # noqa: E731
            cursor_of = lambda it: (it["PK"]["S"], it["SK"]["S"])  # noqa: E731
            start_cursor = (
                (ExclusiveStartKey["PK"]["S"], ExclusiveStartKey["SK"]["S"])
                if ExclusiveStartKey
                else None
            )

        matched.sort(key=sort_key, reverse=not ScanIndexForward)

        if start_cursor is not None:
            matched = [it for it in matched if cursor_of(it) > start_cursor]

        last_key: dict[str, Any] | None = None
        if Limit is not None and len(matched) > Limit:
            page = matched[:Limit]
            boundary = page[-1]
            if IndexName == "GSI1":
                last_key = {
                    "GSI1PK": boundary["GSI1PK"],
                    "GSI1SK": boundary["GSI1SK"],
                    "PK": boundary["PK"],
                    "SK": boundary["SK"],
                }
            elif IndexName == "GSI2":
                last_key = {
                    "GSI2PK": boundary["GSI2PK"],
                    "GSI2SK": boundary["GSI2SK"],
                    "PK": boundary["PK"],
                    "SK": boundary["SK"],
                }
            else:
                last_key = {"PK": boundary["PK"], "SK": boundary["SK"]}
            matched = page

        resp: dict[str, Any] = {"Items": [dict(it) for it in matched]}
        if last_key is not None:
            resp["LastEvaluatedKey"] = last_key
        return resp

    # -- query matchers ----------------------------------------------------

    def _query_base(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        pk = values[":pk"]["S"]
        rec_prefix = values[":rec"]["S"]
        return [
            dict(it)
            for (ipk, isk), it in self._items.items()
            if ipk == pk and isk.startswith(rec_prefix)
        ]

    def _query_gsi1(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        pk = values[":pk"]["S"]
        lo = values[":start"]["S"]
        hi = values[":end"]["S"]
        out: list[dict[str, Any]] = []
        for it in self._items.values():
            if it.get("GSI1PK", {}).get("S") != pk:
                continue
            gsi_sk = it["GSI1SK"]["S"]
            if lo <= gsi_sk <= hi:
                out.append(dict(it))
        return out

    def _query_gsi2(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        # GSI2 is an owner partition query (``GSI2PK = :pk``) with no range
        # condition; only records that carry GSI2 keys (owner-attributed records)
        # participate.
        pk = values[":pk"]["S"]
        return [
            dict(it)
            for it in self._items.values()
            if it.get("GSI2PK", {}).get("S") == pk
        ]
