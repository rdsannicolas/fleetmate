"""Dependency wiring for the thin Lambda handlers (Task 13.1).

Handlers must delegate to the service layer, but they also need those services
constructed. To keep the handlers thin *and* unit-testable without AWS, the
wiring lives behind an injectable factory seam instead of being built inline in
each entry point:

* In production, each handler calls a module-level ``get_*`` accessor
  (:func:`get_diagnostics_service` / :func:`get_interaction_store`) which
  **lazily** builds the service graph the first time it is needed -- importing
  Boto3 and reading configuration from environment variables only then. Nothing
  touches AWS at import time, so the module imports cleanly in any environment.
* In tests, :func:`set_diagnostics_service` / :func:`set_interaction_store`
  inject fakes (a fake service / a store over the in-memory DynamoDB client), so
  the handlers' outcome -> HTTP mapping is exercised with no AWS access. The
  overrides are cleared with :func:`reset_dependencies`.

The lazy builders deliberately construct the real adapters
(:class:`FleetMateDiagnostics`, :class:`BedrockAiEngine`,
:class:`EventBridgePublisher`) but import Boto3 only inside the builder, matching
how :class:`BedrockRuntimeInvoker` isolates the SDK. Configuration (table name,
model/guardrail ids, event-bus name) comes from environment variables set by the
CloudFormation stack (Task 14).
"""

from __future__ import annotations

import os
from typing import Optional

from diagnostics.repositories import FleetMateDiagnostics
from diagnostics.services import (
    BedrockAiEngine,
    ConversationManager,
    DiagnosticsService,
    InteractionStore,
)

__all__ = [
    "get_diagnostics_service",
    "get_interaction_store",
    "set_diagnostics_service",
    "set_interaction_store",
    "reset_dependencies",
]

# Module-level singletons. ``None`` means "not built / not injected yet"; the
# accessors lazily build the real graph on first use and tests overwrite them.
_diagnostics_service: Optional[DiagnosticsService] = None
_interaction_store: Optional[InteractionStore] = None


# ---------------------------------------------------------------------------
# Accessors (used by the handlers)
# ---------------------------------------------------------------------------


def get_diagnostics_service() -> DiagnosticsService:
    """Return the :class:`DiagnosticsService`, lazily building it on first use."""

    global _diagnostics_service
    if _diagnostics_service is None:
        _diagnostics_service = _build_diagnostics_service()
    return _diagnostics_service


def get_interaction_store() -> InteractionStore:
    """Return the :class:`InteractionStore`, lazily building it on first use."""

    global _interaction_store
    if _interaction_store is None:
        _interaction_store = _build_interaction_store()
    return _interaction_store


# ---------------------------------------------------------------------------
# Test injection seam
# ---------------------------------------------------------------------------


def set_diagnostics_service(service: Optional[DiagnosticsService]) -> None:
    """Inject a (possibly fake) diagnostics service for tests."""

    global _diagnostics_service
    _diagnostics_service = service


def set_interaction_store(store: Optional[InteractionStore]) -> None:
    """Inject a (possibly fake) interaction store for tests."""

    global _interaction_store
    _interaction_store = store


def reset_dependencies() -> None:
    """Clear all injected/lazily-built dependencies (test teardown)."""

    global _diagnostics_service, _interaction_store
    _diagnostics_service = None
    _interaction_store = None


# ---------------------------------------------------------------------------
# Lazy production builders (import Boto3 only here, at first use)
# ---------------------------------------------------------------------------


def _repository() -> FleetMateDiagnostics:
    """Build the DynamoDB repository from environment configuration.

    Boto3 is imported inside the builder so module import never requires the SDK
    or AWS credentials.
    """

    import boto3  # local import: no AWS dependency at module import time.

    table_name = os.environ.get("FLEETMATE_TABLE_NAME", "FleetMateDiagnostics")
    region = os.environ.get("AWS_REGION")
    client = boto3.client("dynamodb", region_name=region)
    return FleetMateDiagnostics(client, table_name)


def _build_diagnostics_service() -> DiagnosticsService:
    """Construct the full ``analyze`` service graph from environment config.

    Wires the Conversation_Manager (over the repository), the Bedrock AI_Engine
    (with its Guardrail + approved model), and the EventBridge publisher for
    non-blocking persistence. All AWS clients are created here, lazily.
    """

    import boto3  # local import: SDK only loaded when the real graph is built.

    from diagnostics.events import EventBridgePublisher
    from diagnostics.services import BedrockRuntimeInvoker

    region = os.environ.get("AWS_REGION")
    repo = _repository()
    conversation = ConversationManager(repo)

    ai_engine = BedrockAiEngine(
        BedrockRuntimeInvoker(region_name=region),
        model_id=os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0"),
        guardrail_id=os.environ["BEDROCK_GUARDRAIL_ID"],
        guardrail_version=os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
    )

    publisher = EventBridgePublisher(
        boto3.client("events", region_name=region),
        os.environ.get("FLEETMATE_EVENT_BUS", "default"),
    )

    return DiagnosticsService(conversation, ai_engine, publisher)


def _build_interaction_store() -> InteractionStore:
    """Construct the read-side :class:`InteractionStore` over the repository."""

    return InteractionStore(_repository())
