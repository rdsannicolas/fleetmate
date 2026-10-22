"""Unit tests for the AI_Engine interface and BedrockAiEngine adapter (Task 6.2).

These tests exercise the adapter's core logic entirely with injected fakes -- a
fake Bedrock invoker, a fake monotonic clock, a captured sleep, and a fixed
jitter -- so nothing here touches AWS. They cover:

* output parsing into the 4-field structured shape and the insufficient-info
  shape (never raw model text) -- Req 2.1, 2.2, 2.5;
* approved-model and mandatory-Guardrail enforcement -- tech.md;
* the 30-second generation timeout -- Req 2.6;
* exponential backoff with jitter and the circuit breaker on the Bedrock
  dependency -- design Reliability.

_Requirements: 2.1, 2.2, 2.3, 2.5, 2.6_
"""

from __future__ import annotations

import pytest

from diagnostics.models import DiagnosticResult
from diagnostics.prompts import ContextTurn
from diagnostics.services import (
    AiEngine,
    BedrockAiEngine,
    BedrockRuntimeInvoker,
    CircuitBreaker,
)
from shared.outcomes import Err, Ok


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock whose value advances only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeInvoker:
    """A fake BedrockInvoker: returns canned bodies and/or raises per attempt.

    ``responses`` is a list of either dicts (returned) or Exception instances
    (raised). It records every request it receives for assertion.
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.calls = 0

    def invoke(self, model_id: str, payload: dict) -> dict:
        self.calls += 1
        self.model_id = model_id
        self.requests.append(payload)
        item = self._responses.pop(0) if self._responses else {}
        if isinstance(item, Exception):
            raise item
        return item


def _engine(
    invoker: FakeInvoker,
    *,
    clock: FakeClock | None = None,
    sleep=None,
    max_attempts: int = 3,
    jitter=lambda: 1.0,
    breaker: CircuitBreaker | None = None,
    timeout_seconds: float = 30.0,
) -> BedrockAiEngine:
    """Construct a BedrockAiEngine wired with injected, deterministic doubles."""

    clock = clock or FakeClock()
    sleeps: list[float] = []
    if sleep is None:
        sleep = sleeps.append
    engine = BedrockAiEngine(
        invoker,
        model_id="amazon.nova-pro-v1:0",
        guardrail_id="gr-123",
        guardrail_version="1",
        max_attempts=max_attempts,
        base_backoff=1.0,
        max_backoff=8.0,
        timeout_seconds=timeout_seconds,
        clock=clock,
        sleep=sleep,
        jitter=jitter,
        circuit_breaker=breaker,
    )
    engine._test_sleeps = sleeps  # type: ignore[attr-defined]
    return engine


# ---------------------------------------------------------------------------
# Interface conformance
# ---------------------------------------------------------------------------


def test_bedrock_engine_satisfies_ai_engine_protocol() -> None:
    """BedrockAiEngine is a structural AiEngine (runtime-checkable Protocol)."""
    engine = _engine(FakeInvoker([{}]))
    assert isinstance(engine, AiEngine)


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


def test_unapproved_model_is_rejected() -> None:
    """Only Nova/Llama model ids are accepted (tech.md approved models)."""
    with pytest.raises(ValueError):
        BedrockAiEngine(
            FakeInvoker([]),
            model_id="anthropic.claude-v2",
            guardrail_id="gr",
            guardrail_version="1",
        )


def test_missing_guardrail_is_rejected() -> None:
    """Guardrails are mandatory; a blank identifier/version is rejected."""
    with pytest.raises(ValueError):
        BedrockAiEngine(
            FakeInvoker([]),
            model_id="meta.llama3-70b-instruct-v1:0",
            guardrail_id="",
            guardrail_version="1",
        )


# ---------------------------------------------------------------------------
# Output parsing -- success / insufficient-info / malformed (Req 2.1, 2.2, 2.5)
# ---------------------------------------------------------------------------


def test_successful_diagnosis_parses_four_field_shape() -> None:
    """A well-formed response parses into exactly one issue + recommendation."""
    invoker = FakeInvoker(
        [
            {
                "probableIssue": "Weak Battery",
                "repairRecommendation": "Replace battery",
                "estimatedCost": "Rs 4,000 - 8,000",
                "confidenceScore": 0.89,
            }
        ]
    )
    outcome = _engine(invoker).generate("car won't start in the morning")
    assert isinstance(outcome, Ok)
    result = outcome.value
    assert isinstance(result, DiagnosticResult)
    assert result.needsMoreDetail is False
    assert result.probableIssue == "Weak Battery"
    assert result.repairRecommendation == "Replace battery"
    assert result.confidenceScore == 0.89


def test_guardrails_attached_on_every_invocation() -> None:
    """Every request carries the Guardrail identifier + version (tech.md)."""
    invoker = FakeInvoker([{"probableIssue": "X", "repairRecommendation": "Y", "confidenceScore": 0.8}])
    _engine(invoker).generate("engine knocking noise")
    assert invoker.requests[0]["guardrailIdentifier"] == "gr-123"
    assert invoker.requests[0]["guardrailVersion"] == "1"
    # Raw user text is never raw-concatenated; it rides inside the built prompt.
    assert "engine knocking noise" in invoker.requests[0]["prompt"]


def test_insufficient_info_when_flag_set() -> None:
    """An explicit needsMoreDetail flag yields the insufficient-info shape."""
    invoker = FakeInvoker([{"needsMoreDetail": True, "message": "Which noise?"}])
    outcome = _engine(invoker).generate("there is a noise")
    assert isinstance(outcome, Ok)
    assert outcome.value.needsMoreDetail is True
    assert outcome.value.probableIssue is None
    assert outcome.value.message == "Which noise?"


def test_insufficient_info_when_issue_missing() -> None:
    """A response lacking a probable issue degrades to insufficient-info."""
    invoker = FakeInvoker([{"repairRecommendation": "something", "confidenceScore": 0.5}])
    outcome = _engine(invoker).generate("it is broken somehow")
    assert isinstance(outcome, Ok)
    assert outcome.value.needsMoreDetail is True
    assert outcome.value.probableIssue is None


def test_malformed_confidence_maps_to_ai_error() -> None:
    """A full result with an out-of-range confidence is treated as malformed."""
    invoker = FakeInvoker(
        [{"probableIssue": "X", "repairRecommendation": "Y", "confidenceScore": 1.5}]
    )
    outcome = _engine(invoker).generate("brakes feel soft when pressed")
    assert isinstance(outcome, Err)
    assert outcome.code == "AI_ERROR"


def test_guardrail_block_maps_to_ai_error() -> None:
    """A Guardrail intervention in the envelope maps to AI_ERROR, not raw text."""
    invoker = FakeInvoker([{"amazon-bedrock-guardrailAction": "INTERVENED", "probableIssue": "x"}])
    outcome = _engine(invoker).generate("how do I disable my brakes")
    assert isinstance(outcome, Err)
    assert outcome.code == "AI_ERROR"


def test_model_text_wrapped_as_json_is_parsed() -> None:
    """Output carried as a JSON string under output_text is parsed, not echoed."""
    import json

    invoker = FakeInvoker(
        [
            {
                "output_text": json.dumps(
                    {
                        "probableIssue": "Worn Brake Pads",
                        "repairRecommendation": "Replace pads",
                        "estimatedCost": "Rs 3,000",
                        "confidenceScore": 0.77,
                    }
                )
            }
        ]
    )
    outcome = _engine(invoker).generate("squealing when braking")
    assert isinstance(outcome, Ok)
    assert outcome.value.probableIssue == "Worn Brake Pads"


def test_context_turns_included_in_prompt() -> None:
    """Prior ascending context is woven into the prompt sent to Bedrock."""
    invoker = FakeInvoker([{"probableIssue": "A", "repairRecommendation": "B", "confidenceScore": 0.9}])
    context = [ContextTurn(symptom="was fine yesterday", diagnosis="monitor it")]
    _engine(invoker).generate("now it stalls", context)
    assert "was fine yesterday" in invoker.requests[0]["prompt"]


def test_invalid_symptom_short_circuits_before_invocation() -> None:
    """An empty symptom returns EMPTY and never calls the model (Req 2.4)."""
    invoker = FakeInvoker([{"probableIssue": "x"}])
    outcome = _engine(invoker).generate("   ")
    assert isinstance(outcome, Err)
    assert outcome.code == "EMPTY"
    assert invoker.calls == 0


# ---------------------------------------------------------------------------
# Timeout (Req 2.6)
# ---------------------------------------------------------------------------


def test_timeout_before_first_attempt_when_budget_exhausted() -> None:
    """If the clock is already past 30s, generation reports AI_TIMEOUT."""
    clock = FakeClock()

    def slow_invoke(model_id, payload):
        clock.advance(31.0)  # model "took" 31 seconds on the first call
        return {"probableIssue": "x", "repairRecommendation": "y", "confidenceScore": 0.9}

    invoker = FakeInvoker([RuntimeError("boom")])  # first attempt fails
    engine = _engine(invoker, clock=clock, max_attempts=3)

    # Simulate elapsed time passing the budget before the retry sleep completes.
    def advancing_sleep(_seconds: float) -> None:
        clock.advance(30.0)

    engine._sleep = advancing_sleep  # type: ignore[attr-defined]
    outcome = engine.generate("engine wont start at all")
    assert isinstance(outcome, Err)
    assert outcome.code == "AI_TIMEOUT"


def test_timeout_when_single_call_exceeds_budget() -> None:
    """A single attempt that runs past 30s is reported as a timeout on retry."""
    clock = FakeClock()

    class SlowInvoker:
        calls = 0

        def invoke(self, model_id, payload):
            type(self).calls += 1
            clock.advance(31.0)
            raise RuntimeError("still working")

    engine = BedrockAiEngine(
        SlowInvoker(),
        model_id="amazon.nova-lite-v1:0",
        guardrail_id="gr",
        guardrail_version="1",
        max_attempts=3,
        clock=clock,
        sleep=lambda s: None,
        jitter=lambda: 0.0,
    )
    outcome = engine.generate("there is smoke from the hood")
    assert isinstance(outcome, Err)
    assert outcome.code == "AI_TIMEOUT"


# ---------------------------------------------------------------------------
# Backoff + jitter
# ---------------------------------------------------------------------------


def test_retries_with_exponential_backoff_and_jitter_then_succeeds() -> None:
    """A transient failure retries with exponential-backoff*jitter, then wins."""
    invoker = FakeInvoker(
        [
            RuntimeError("transient"),
            RuntimeError("transient"),
            {"probableIssue": "Alternator", "repairRecommendation": "Replace", "confidenceScore": 0.8},
        ]
    )
    # jitter fixed at 0.5 so delays are deterministic: base=1 -> 1*2^0*0.5=0.5,
    # then 1*2^1*0.5=1.0.
    engine = _engine(invoker, max_attempts=3, jitter=lambda: 0.5)
    outcome = engine.generate("battery light is on while driving")
    assert isinstance(outcome, Ok)
    assert invoker.calls == 3
    assert engine._test_sleeps == [0.5, 1.0]  # type: ignore[attr-defined]


def test_backoff_is_capped_at_max_backoff() -> None:
    """Nominal backoff is capped at max_backoff before jitter is applied."""
    invoker = FakeInvoker(
        [RuntimeError("t"), RuntimeError("t"), RuntimeError("t"), RuntimeError("t"),
         {"probableIssue": "A", "repairRecommendation": "B", "confidenceScore": 0.9}]
    )
    engine = _engine(invoker, max_attempts=5, jitter=lambda: 1.0)
    outcome = engine.generate("intermittent electrical fault somewhere")
    assert isinstance(outcome, Ok)
    # base=1: 1, 2, 4, 8 (capped at max_backoff=8).
    assert engine._test_sleeps == [1.0, 2.0, 4.0, 8.0]  # type: ignore[attr-defined]


def test_all_attempts_fail_maps_to_ai_error() -> None:
    """Exhausting retries on persistent failure yields AI_ERROR (not a crash)."""
    invoker = FakeInvoker([RuntimeError("down")] * 3)
    outcome = _engine(invoker, max_attempts=3).generate("car makes grinding noise")
    assert isinstance(outcome, Err)
    assert outcome.code == "AI_ERROR"
    assert invoker.calls == 3


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_and_short_circuits() -> None:
    """After the failure threshold the breaker opens and short-circuits calls."""
    breaker_clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=30.0, clock=breaker_clock)

    # First generate: two attempts both fail -> breaker records 2 failures -> open.
    invoker1 = FakeInvoker([RuntimeError("x"), RuntimeError("x")])
    engine1 = _engine(invoker1, max_attempts=2, breaker=breaker)
    out1 = engine1.generate("engine overheats in traffic")
    assert isinstance(out1, Err) and out1.code == "AI_ERROR"
    assert breaker.is_open is True

    # Next generate while open: the invoker is never called (short-circuited).
    invoker2 = FakeInvoker([{"probableIssue": "A", "repairRecommendation": "B", "confidenceScore": 0.9}])
    engine2 = _engine(invoker2, max_attempts=2, breaker=breaker)
    out2 = engine2.generate("coolant leaking under the car")
    assert isinstance(out2, Err) and out2.code == "AI_ERROR"
    assert invoker2.calls == 0


def test_circuit_breaker_half_opens_after_cooldown() -> None:
    """After the cooldown elapses, a probe call is allowed and can close it."""
    breaker_clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30.0, clock=breaker_clock)
    breaker.record_failure()  # open immediately
    assert breaker.is_open is True

    breaker_clock.advance(31.0)  # cooldown elapsed -> half-open probe allowed
    invoker = FakeInvoker([{"probableIssue": "A", "repairRecommendation": "B", "confidenceScore": 0.9}])
    engine = _engine(invoker, max_attempts=2, breaker=breaker)
    outcome = engine.generate("steering feels loose at speed")
    assert isinstance(outcome, Ok)
    assert invoker.calls == 1
    assert breaker.is_open is False


# ---------------------------------------------------------------------------
# Production Boto3 invoker (exercised with a fake bedrock-runtime client)
# ---------------------------------------------------------------------------


class _FakeStreamingBody:
    """Mimics the Boto3 StreamingBody returned under response['body']."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeBedrockClient:
    """A stand-in for boto3.client('bedrock-runtime') -- records the call."""

    def __init__(self, body: bytes, *, extra: dict | None = None) -> None:
        self._body = body
        self._extra = extra or {}
        self.kwargs: dict = {}

    def invoke_model(self, **kwargs) -> dict:
        self.kwargs = kwargs
        return {"body": _FakeStreamingBody(self._body), **self._extra}


def test_runtime_invoker_passes_guardrails_and_parses_body() -> None:
    """The Boto3 invoker attaches Guardrails and returns the parsed JSON body."""
    import json

    diagnosis_json = json.dumps(
        {
            "probableIssue": "Weak Battery",
            "repairRecommendation": "Replace battery",
            "estimatedCost": "Rs 5,000",
            "confidenceScore": 0.9,
        }
    )
    # Amazon Nova returns the assistant answer under output.message.content[].text.
    client = _FakeBedrockClient(
        json.dumps(
            {"output": {"message": {"content": [{"text": diagnosis_json}]}}}
        ).encode("utf-8")
    )
    invoker = BedrockRuntimeInvoker(client=client)
    engine = BedrockAiEngine(
        invoker,
        model_id="amazon.nova-pro-v1:0",
        guardrail_id="gr-xyz",
        guardrail_version="2",
        clock=FakeClock(),
        sleep=lambda s: None,
        jitter=lambda: 0.0,
    )
    outcome = engine.generate("car cranks but will not start")
    assert isinstance(outcome, Ok)
    assert outcome.value.probableIssue == "Weak Battery"
    # Guardrails were applied on the SDK call.
    assert client.kwargs["guardrailIdentifier"] == "gr-xyz"
    assert client.kwargs["guardrailVersion"] == "2"
    assert client.kwargs["modelId"] == "amazon.nova-pro-v1:0"


def test_runtime_invoker_surfaces_guardrail_action_from_envelope() -> None:
    """A Guardrail intervention in the SDK envelope reaches the adapter."""
    import json

    client = _FakeBedrockClient(
        json.dumps(
            {"output": {"message": {"content": [{"text": json.dumps({"probableIssue": "x"})}]}}}
        ).encode("utf-8"),
        extra={"amazon-bedrock-guardrailAction": "INTERVENED"},
    )
    engine = BedrockAiEngine(
        BedrockRuntimeInvoker(client=client),
        model_id="meta.llama3-70b-instruct-v1:0",
        guardrail_id="gr",
        guardrail_version="1",
        clock=FakeClock(),
        sleep=lambda s: None,
        jitter=lambda: 0.0,
    )
    outcome = engine.generate("how to bypass the safety system")
    assert isinstance(outcome, Err)
    assert outcome.code == "AI_ERROR"
