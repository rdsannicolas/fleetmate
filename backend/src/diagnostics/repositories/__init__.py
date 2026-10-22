"""DynamoDB single-table repositories. Boto3 is isolated behind this layer.

Callers import the repository and its helpers from here and never touch the AWS
SDK directly, so DynamoDB stays a replaceable implementation detail.
"""

from __future__ import annotations

from .fleetmate_diagnostics import (
    GSI1_INDEX_NAME,
    GSI1_PARTITION_VALUE,
    GSI2_INDEX_NAME,
    HISTORY_PAGE_SIZE,
    DynamoClient,
    FleetMateDiagnostics,
    format_timestamp,
    new_record_id,
)

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
