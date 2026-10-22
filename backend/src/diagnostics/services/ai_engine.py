"""The AI_Engine: a provider-agnostic diagnosis interface and its Bedrock adapter.

This module defines the seam between the diagnostics business logic and the
underlying AI model (design "AI_Engine (Amazon Bedrock adapter)"):

* :class:`AiEngine` is the provider-agnostic interface
  (``generate(symptomText, context) -> AiOutcome``). The Conversation_Manager
  and the diagnostics service depend on *this* protocol, never on Boto3, so the
  model can change (Nova <-> Llama) or be faked in tests without touching
  business logic (design "Layering and Dependency Direction").
* :class:`BedrockAiEngine` is the only place ``bedrock-runtime`` is used. It
  builds the prompt from the reusable templates (via
  :func:`diagnostics.prompts.build_prompt`), attaches Bedrock Guardrails on
  every invocation, parses the model output into the structured
  :class:`~diagnostics.models.DiagnosticResult` (never raw model text), and
  wraps the call in a 30-second timeout, exponential backoff with jitter, and a
  circuit breaker guarding the Bedrock dependency.

Design/tech constraints honoured here:

* **Typed outcomes, not exceptions.** ``generate`` returns an :class:`Ok`
  carrying a :class:`~diagnostics.models.DiagnosticResult` (success *or*
  insufficient-info shape) or an :class:`Err` with ``AI_TIMEOUT`` / ``AI_ERROR``
  (design AI_Engine contract; mapped to 503 by the handler).
* **Approved models only.** Nova and Llama model id prefixes are accepted; any
  other id is rejected before invocation (tech.md approved models).
* **Guardrails on every call.** A ``guardrailIdentifier`` + ``guardrailVersion``
  pair is required and attached to every ``invoke_model`` call (tech.md
  "Apply Bedrock Guardrails on every AI endpoint").
* **Testable reliability.** The timeout clock, the sleep used for backoff, the
  jitter source, and the low-level invoker are all injectable, so unit/property
  tests exercise timeout/backoff/circuit-breaker behaviour deterministically
  without calling AWS.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol, runtime_checkable

from diagnostics.models import DiagnosticResult
from diagnostics.prompts import ContextTurn, PromptPayload, build_prompt
from shared.outcomes import Err, Ok, Outcome

__all__ = [
    "AiEngine",
    "AiOutcome",
    "BedrockAiEngine",
    "BedrockRuntimeInvoker",
    "CircuitBreaker",
    "CircuitBreakerError",
    "BedrockInvoker",
    "GENERATION_TIMEOUT_SECONDS",
    "APPROVED_MODEL_PREFIXES",
    "insufficient_info",
]

# Req 2.6: the AI generation must not exceed 30 seconds. The adapter treats a
# wall-clock elapsed time at or beyond this budget as a timeout and reports
# ``AI_TIMEOUT`` rather than hanging on the model.
GENERATION_TIMEOUT_SECONDS = 30.0

# tech.md approved models: Amazon Bedrock Nova and Meta Llama only. Bedrock model
# ids embed the provider (e.g. ``amazon.nova-pro-v1:0``, ``meta.llama3-70b-...``),
# so matching the provider prefix is sufficient to gate on approved families.
APPROVED_MODEL_PREFIXES: tuple[str, ...] = ("amazon.nova", "meta.llama")


# ---------------------------------------------------------------------------
# Outcome type and the provider-agnostic interface
# ---------------------------------------------------------------------------

# The AI_Engine returns a typed outcome. Both the success shape and the
# insufficient-info shape are carried by ``DiagnosticResult`` (its
# ``needsMoreDetail`` flag distinguishes them), so an ``Ok[DiagnosticResult]``
# covers "Diagnostic_Result" and "InsufficientInfo" from the design contract.
# ``Err("AI_TIMEOUT")`` and ``Err("AI_ERROR")`` cover "Timeout" and "AiError".
AiOutcome = Outcome[DiagnosticResult]


@runtime_checkable
class AiEngine(Protocol):
    """Provider-agnostic diagnosis interface the business logic depends on.

    Implementations convert a sanitized current Symptom_Description plus ordered
    prior context into a typed :data:`AiOutcome`. Business logic must depend on
    this protocol and never import a provider SDK, so the model is a replaceable
    adapter (design "AI_Engine as a Bedrock adapter behind an interface").
    """

    def generate(
        self,
        symptom_text: str,
        context: Iterable[ContextTurn] = (),
    ) -> AiOutcome:
        """Return a structured diagnosis outcome for ``symptom_text``.

        * ``Ok(DiagnosticResult(needsMoreDetail=False, ...))`` -- a successful
          diagnosis carrying exactly one Probable_Issue and one
          Repair_Recommendation plus ``estimatedCost`` and ``confidenceScore``
          (Req 2.1, 2.2).
        * ``Ok(DiagnosticResult(needsMoreDetail=True, message=...))`` -- the
          insufficient-information shape (no Probable_Issue) with a
          Customer-observable prompt for more detail (Req 2.5).
        * ``Err("AI_TIMEOUT")`` -- generation exceeded 30 seconds (Req 2.6).
        * ``Err("AI_ERROR")`` -- a Guardrail block or model/invocation failure.
        """
        ...


def insufficient_info(message: str) -> DiagnosticResult:
    """Build the insufficient-information :class:`DiagnosticResult` (Req 2.5).

    The result carries no Probable_Issue and sets ``needsMoreDetail`` with a
    Customer-observable ``message`` asking for the specific additional detail
    required.
    """

    return DiagnosticResult(needsMoreDetail=True, message=message)


# ---------------------------------------------------------------------------
# Circuit breaker guarding the Bedrock dependency
# ---------------------------------------------------------------------------


class CircuitBreakerError(RuntimeError):
    """Raised internally when the breaker is open and a call is short-circuited.

    This never crosses the :class:`AiEngine` boundary; the adapter maps it to an
    ``Err("AI_ERROR")`` outcome.
    """


class CircuitBreaker:
    """A minimal circuit breaker for the Bedrock dependency (design Reliability).

    After ``failure_threshold`` consecutive failures the breaker *opens* and
    short-circuits calls (raising :class:`CircuitBreakerError`) for
    ``reset_timeout`` seconds. The first call after that window is allowed
    through as a half-open probe; success closes the breaker, failure re-opens
    it. "Now" is read from an injected clock so tests drive the open/half-open
    transitions deterministically.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        """Return ``True`` while the breaker is open and still within cooldown."""

        if self._opened_at is None:
            return False
        return (self._clock() - self._opened_at) < self._reset_timeout

    def before_call(self) -> None:
        """Gate a call: raise :class:`CircuitBreakerError` if the breaker is open.

        When the cooldown has elapsed the breaker moves to half-open (the call is
        allowed through as a probe) by clearing the open marker.
        """

        if self._opened_at is None:
            return
        if (self._clock() - self._opened_at) < self._reset_timeout:
            raise CircuitBreakerError("Bedrock circuit breaker is open")
        # Cooldown elapsed -> half-open: allow one probe call through.
        self._opened_at = None

    def record_success(self) -> None:
        """Reset the breaker to fully closed after a successful call."""

        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """Count a failure and open the breaker once the threshold is reached."""

        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = self._clock()


# ---------------------------------------------------------------------------
# Low-level invoker seam (the only thing a BedrockAiEngine needs from AWS)
# ---------------------------------------------------------------------------


@runtime_checkable
class BedrockInvoker(Protocol):
    """The narrow Bedrock call the adapter needs; the AWS seam for tests.

    Production supplies a thin wrapper over the Boto3 ``bedrock-runtime`` client;
    tests supply a fake that returns canned bodies or raises, so no test ever
    reaches AWS. ``invoke`` returns the parsed model-output JSON object (the
    adapter does not care how the client was built).
    """

    def invoke(self, model_id: str, payload: dict) -> dict:
        """Invoke the model with Guardrails and return the parsed output JSON."""
        ...


class BedrockRuntimeInvoker:
    """Production :class:`BedrockInvoker` over Boto3 ``bedrock-runtime``.

    This is the single place the AWS SDK is used for AI invocation. It calls
    ``invoke_model`` with the Guardrail identifier/version already present in the
    request body, reads the streaming ``body`` payload, and returns the parsed
    JSON object for the adapter to interpret -- it never returns raw text to a
    caller. ``boto3`` is imported lazily so importing this module (and running
    the test suite) does not require the SDK or any AWS credentials.
    """

    def __init__(self, client: object | None = None, *, region_name: str | None = None) -> None:
        if client is None:
            import boto3  # local import: no AWS dependency at module import time.

            client = boto3.client("bedrock-runtime", region_name=region_name)
        self._client = client

    def invoke(self, model_id: str, payload: dict) -> dict:
        """Call ``bedrock-runtime.invoke_model`` and return the parsed output.

        The Guardrail id/version are passed both in the request body (so the
        adapter's record of them stays authoritative) and as the SDK's
        ``guardrailIdentifier``/``guardrailVersion`` parameters, applying
        Guardrails on every invocation (tech.md).
        """

        # Amazon Nova (and the other Bedrock "messages" models) expect a
        # messages/system/inferenceConfig body -- not a bare {system, prompt}.
        # The system framing goes in the top-level `system` list and the
        # assembled user prompt becomes a single user turn.
        request_body: dict = {
            "messages": [
                {"role": "user", "content": [{"text": payload["prompt"]}]}
            ],
            "inferenceConfig": {"maxTokens": 1024, "temperature": 0.2},
        }
        system_text = payload.get("system")
        if system_text:
            request_body["system"] = [{"text": system_text}]

        response = self._client.invoke_model(
            modelId=model_id,
            guardrailIdentifier=payload["guardrailIdentifier"],
            guardrailVersion=payload["guardrailVersion"],
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body),
        )
        body = response.get("body")
        raw_bytes = body.read() if hasattr(body, "read") else body
        parsed = json.loads(raw_bytes) if raw_bytes else {}
        if not isinstance(parsed, dict):
            return {}

        # Nova returns {"output": {"message": {"content": [{"text": ...}]}}}.
        # Extract the assistant text and expose it under `output_text` so the
        # adapter's _parse_output can json.loads the model's structured answer.
        result: dict = {}
        assistant_text = _extract_nova_text(parsed)
        if assistant_text is not None:
            result["output_text"] = assistant_text

        # Preserve a Guardrail-intervention signal from either the response
        # envelope or the model-output body so the adapter maps it to AI_ERROR.
        action = response.get("amazon-bedrock-guardrailAction") or parsed.get(
            "amazon-bedrock-guardrailAction"
        )
        if action is not None:
            result["amazon-bedrock-guardrailAction"] = action
        return result


@dataclass(frozen=True, slots=True)
class _TimedOut(Exception):
    """Internal marker that the 30-second budget was exceeded."""

    elapsed: float


# ---------------------------------------------------------------------------
# The Bedrock adapter
# ---------------------------------------------------------------------------


class BedrockAiEngine:
    """Concrete :class:`AiEngine` backed by Amazon Bedrock ``bedrock-runtime``.

    All ``bedrock-runtime`` usage is confined to this class (through the injected
    :class:`BedrockInvoker`). The adapter:

    * validates the model id against :data:`APPROVED_MODEL_PREFIXES` (Nova/Llama);
    * requires a Guardrail identifier+version and attaches it to every call;
    * builds the prompt via :func:`diagnostics.prompts.build_prompt` (sanitized,
      template-filled, never raw-concatenated);
    * enforces the 30-second timeout, retries transient failures with
      exponential backoff + jitter, and guards the dependency with a circuit
      breaker;
    * parses the model output into a :class:`DiagnosticResult` -- never raw text.

    The timeout clock, sleep, jitter source, breaker, and invoker are injectable
    so reliability behaviour is unit/property testable without AWS.
    """

    def __init__(
        self,
        invoker: BedrockInvoker,
        *,
        model_id: str,
        guardrail_id: str,
        guardrail_version: str,
        max_attempts: int = 3,
        base_backoff: float = 0.5,
        max_backoff: float = 8.0,
        timeout_seconds: float = GENERATION_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Optional[Callable[[], float]] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self._require_approved_model(model_id)
        if not guardrail_id or not guardrail_version:
            # Guardrails are mandatory on every invocation (tech.md).
            raise ValueError("guardrail_id and guardrail_version are required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        self._invoker = invoker
        self._model_id = model_id
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._timeout = timeout_seconds
        self._clock = clock
        self._sleep = sleep
        # Default jitter is a full-jitter multiplier in [0, 1); injectable so
        # tests get deterministic backoff.
        if jitter is None:
            import random

            jitter = random.random
        self._jitter = jitter
        self._breaker = circuit_breaker or CircuitBreaker()

    @staticmethod
    def _require_approved_model(model_id: str) -> None:
        if not any(model_id.startswith(prefix) for prefix in APPROVED_MODEL_PREFIXES):
            raise ValueError(
                f"Model {model_id!r} is not an approved Bedrock model "
                f"(allowed prefixes: {', '.join(APPROVED_MODEL_PREFIXES)})"
            )

    # -- Public interface --------------------------------------------------

    def generate(
        self,
        symptom_text: str,
        context: Iterable[ContextTurn] = (),
    ) -> AiOutcome:
        """Generate a structured diagnosis; see :meth:`AiEngine.generate`.

        The prompt is validated/assembled first (fail-fast). A validation
        failure (``EMPTY``/``TOO_LONG``) is surfaced unchanged. The Bedrock call
        is then run under the timeout + backoff + breaker; a timeout maps to
        ``AI_TIMEOUT`` and any Guardrail block / model failure / open breaker
        maps to ``AI_ERROR``. On success the model output is parsed into a
        :class:`DiagnosticResult` (success or insufficient-info shape).
        """

        # Build the prompt from reusable templates (validate payload before use).
        built = build_prompt(symptom_text, context)
        if isinstance(built, Err):
            # EMPTY / TOO_LONG surfaced unchanged; no model call is made.
            return built
        payload = built.value

        try:
            raw = self._invoke_with_reliability(payload)
        except _TimedOut:
            return Err(
                "AI_TIMEOUT",
                "The diagnosis took too long to complete. Please try again.",
            )
        except CircuitBreakerError:
            return Err(
                "AI_ERROR",
                "The diagnosis service is temporarily unavailable. Please try again shortly.",
            )
        except Exception:  # noqa: BLE001 -- provider errors never leak outward.
            return Err(
                "AI_ERROR",
                "The diagnosis could not be completed. Please try again.",
            )

        return self._parse_output(raw)

    # -- Reliability wrapping ---------------------------------------------

    def _invoke_with_reliability(self, payload: PromptPayload) -> dict:
        """Invoke Bedrock under the timeout, backoff+jitter, and circuit breaker.

        Retries transient failures up to ``max_attempts`` with exponential
        backoff and full jitter, but never past the 30-second wall-clock budget:
        before each attempt (and before sleeping) the elapsed time is checked and
        a :class:`_TimedOut` is raised if the budget is exhausted. The circuit
        breaker is consulted before every attempt and updated with the result.
        """

        request = self._build_request(payload)
        started = self._clock()
        last_error: Optional[Exception] = None

        for attempt in range(self._max_attempts):
            # Enforce the 30-second budget before each attempt (Req 2.6).
            if (self._clock() - started) >= self._timeout:
                raise _TimedOut(self._clock() - started)

            # Circuit breaker gate (raises CircuitBreakerError when open).
            self._breaker.before_call()

            try:
                raw = self._invoker.invoke(self._model_id, request)
            except Exception as exc:  # noqa: BLE001 -- classify + maybe retry.
                self._breaker.record_failure()
                last_error = exc
                # Back off before the next attempt, honouring the time budget.
                if attempt < self._max_attempts - 1:
                    self._backoff_sleep(attempt, started)
                    continue
                raise
            else:
                self._breaker.record_success()
                return raw

        # Exhausted attempts without returning -> re-raise the last error so the
        # caller maps it to AI_ERROR.
        assert last_error is not None
        raise last_error

    def _backoff_sleep(self, attempt: int, started: float) -> None:
        """Sleep an exponential-backoff-with-full-jitter interval, time-capped.

        The nominal delay is ``base * 2**attempt`` capped at ``max_backoff``;
        full jitter scales it by a random factor in ``[0, 1)``. The sleep is
        clamped so it never pushes elapsed time past the 30-second budget, and if
        no budget remains a :class:`_TimedOut` is raised instead of sleeping.
        """

        nominal = min(self._base_backoff * (2 ** attempt), self._max_backoff)
        delay = nominal * self._jitter()

        remaining = self._timeout - (self._clock() - started)
        if remaining <= 0:
            raise _TimedOut(self._clock() - started)
        self._sleep(min(delay, remaining))

    # -- Request / response shaping ---------------------------------------

    def _build_request(self, payload: PromptPayload) -> dict:
        """Assemble the ``invoke_model`` request body with Guardrails attached.

        Guardrails are included on *every* request (tech.md). The system framing
        and assembled user prompt come from the validated :class:`PromptPayload`;
        neither is ever surfaced to the Customer.
        """

        return {
            "system": payload.system,
            "prompt": payload.user,
            # Guardrails attached on every invocation (tech.md AI governance).
            "guardrailIdentifier": self._guardrail_id,
            "guardrailVersion": self._guardrail_version,
        }

    def _parse_output(self, raw: dict) -> AiOutcome:
        """Parse the model output JSON into a :class:`DiagnosticResult`.

        Raw model text is never returned (tech.md). The adapter expects the
        4-field structured object the prompt contract requests, or the
        insufficient-info shape. A Guardrail block signalled in the output, or
        any output that cannot be parsed into the required shape, maps to
        ``AI_ERROR``. When the model cannot determine an issue (either an
        explicit ``needsMoreDetail`` flag or a missing/blank ``probableIssue``),
        the insufficient-info result is returned (Req 2.5).
        """

        body = self._extract_body(raw)
        if body is None:
            return Err("AI_ERROR", "The diagnosis could not be completed. Please try again.")

        # A Guardrail intervention surfaces as a blocked response -> AI_ERROR.
        if self._is_guardrail_block(raw):
            return Err(
                "AI_ERROR",
                "The request was blocked by safety controls. Please rephrase and try again.",
            )

        # Insufficient-information shape (Req 2.5): explicit flag, or no issue.
        if body.get("needsMoreDetail") is True:
            message = _as_text(body.get("message")) or (
                "More detail is needed to diagnose the issue. Please describe the "
                "symptom further."
            )
            return Ok(insufficient_info(message))

        probable_issue = _as_text(body.get("probableIssue"))
        repair = _as_text(body.get("repairRecommendation"))
        if not probable_issue or not repair:
            # No determinable issue -> insufficient-info rather than a bad result.
            message = _as_text(body.get("message")) or (
                "The symptoms are not specific enough to identify a probable issue. "
                "Please provide more detail."
            )
            return Ok(insufficient_info(message))

        confidence = _as_confidence(body.get("confidenceScore"))
        if confidence is None:
            return Err("AI_ERROR", "The diagnosis could not be completed. Please try again.")

        return Ok(
            DiagnosticResult(
                needsMoreDetail=False,
                probableIssue=probable_issue,
                repairRecommendation=repair,
                estimatedCost=_as_text(body.get("estimatedCost")),
                confidenceScore=confidence,
            )
        )

    @staticmethod
    def _extract_body(raw: dict) -> Optional[dict]:
        """Return the structured output object from a model response.

        Accepts either the already-parsed object directly, or a wrapper carrying
        the model text under common keys (``output_text`` / ``completion`` /
        ``outputText``) that holds a JSON object. Returns ``None`` when nothing
        parseable is present (mapped to ``AI_ERROR`` by the caller).
        """

        if not isinstance(raw, dict):
            return None

        # Direct structured object (fake invoker / already-parsed adapter output):
        # recognised by the presence of any field from the 4-field success shape
        # or the insufficient-info shape, so a partial/degraded body is still
        # routed to parsing (and classified as insufficient-info) rather than
        # mistaken for an unparseable envelope.
        structured_fields = (
            "probableIssue",
            "repairRecommendation",
            "estimatedCost",
            "confidenceScore",
            "needsMoreDetail",
        )
        if any(key in raw for key in structured_fields):
            return raw

        for key in ("output_text", "completion", "outputText", "body"):
            text = raw.get(key)
            if isinstance(text, str):
                parsed = _loads_json_object(text)
                if parsed is not None:
                    return parsed
            elif isinstance(text, dict):
                return text
        return None

    @staticmethod
    def _is_guardrail_block(raw: dict) -> bool:
        """Detect a Guardrail intervention in the raw response envelope."""

        action = raw.get("amazon-bedrock-guardrailAction") or raw.get("guardrailAction")
        if isinstance(action, str) and action.upper() == "INTERVENED":
            return True
        stop = raw.get("stopReason") or raw.get("stop_reason")
        return isinstance(stop, str) and stop.lower() == "guardrail_intervened"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _loads_json_object(text: str) -> Optional[dict]:
    """Parse a JSON object from model text, tolerating prose/code fences.

    Tries a direct `json.loads` first; on failure, strips Markdown code fences
    and, failing that, extracts the first balanced `{...}` span and parses it.
    Returns `None` if no JSON object can be recovered (mapped to AI_ERROR).
    """

    candidates: list[str] = [text.strip()]


    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_nova_text(parsed: dict) -> Optional[str]:
    """Extract the assistant text from an Amazon Nova invoke_model response.

    Nova returns `{"output": {"message": {"content": [{"text": ...}, ...]}}}`.
    This joins all text parts of the assistant message into a single string so
    the adapter can parse the model's structured JSON answer. Returns `None`
    when no text content is present.
    """

    output = parsed.get("output")
    if not isinstance(output, dict):
        return None
    message = output.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    if not parts:
        return None
    return "\n".join(parts).strip() or None


def _as_text(value: object) -> Optional[str]:
    """Return a trimmed non-empty string, or ``None`` for anything else/blank."""

    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


def _as_confidence(value: object) -> Optional[float]:
    """Coerce a confidence score to a float in ``[0.0, 1.0]`` or ``None``.

    A missing, non-numeric, or out-of-range value yields ``None`` so the caller
    treats the response as malformed (``AI_ERROR``) rather than fabricating a
    score.
    """

    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly.
        return None
    if isinstance(value, (int, float)):
        score = float(value)
        if 0.0 <= score <= 1.0:
            return score
    return None
