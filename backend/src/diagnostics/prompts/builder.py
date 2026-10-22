"""Prompt payload validation and assembly.

This module turns a current Symptom_Description plus the Session's ascending
conversation context into a validated, ready-to-send prompt. It is the seam the
AI_Engine (Task 6.2) depends on: the engine supplies raw inputs and receives a
typed outcome -- either an :class:`Ok` carrying a :class:`PromptPayload` ready
for Bedrock, or an :class:`Err` with a :data:`~shared.outcomes.ErrorCode`
describing why the payload is unusable.

Responsibilities (tech.md prompt-engineering rules + design "AI_Engine"):

* **Validate the payload before use.** The current symptom is re-checked with
  the shared fail-fast validator so a prompt is never built from empty or
  over-length input (``EMPTY`` / ``TOO_LONG``). This runs before any template
  filling.
* **Sanitize user input.** Every user-supplied string (the current symptom and
  each prior turn's symptom/diagnosis text) is passed through
  :func:`~diagnostics.prompts.sanitize.sanitize_text`.
* **Insert via placeholders, never raw-concatenate.** Sanitized values are
  placed into the named ``str.format`` placeholders of the templates in
  :mod:`diagnostics.prompts.templates`; user text is never ``+``-joined into
  the prompt.
* **Keep prompts internal.** The returned :class:`PromptPayload` is for the
  Bedrock call only and is never surfaced to the Customer.

The assembled prompt instructs the model to emit the 4-field structured shape
``{ probableIssue, repairRecommendation, estimatedCost, confidenceScore }`` so
the adapter can parse it (Task 6.2) rather than returning raw model text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from shared.outcomes import Err, Ok, Outcome
from shared.validation import validate_symptom

from .sanitize import sanitize_text
from .templates import (
    CONTEXT_HEADER,
    CONTEXT_TURN,
    NO_CONTEXT,
    OUTPUT_CONTRACT,
    SYMPTOM_SECTION,
    SYSTEM_INSTRUCTION,
)

__all__ = ["ContextTurn", "PromptPayload", "build_prompt"]


@dataclass(frozen=True, slots=True)
class ContextTurn:
    """One prior conversation turn supplied as context, in ascending order.

    ``symptom`` is the Customer's earlier Symptom_Description and ``diagnosis``
    is the assistant's earlier reply text. Both are user/model-facing strings
    that are sanitized before being placed into the prompt.
    """

    symptom: str
    diagnosis: str


@dataclass(frozen=True, slots=True)
class PromptPayload:
    """A validated prompt ready to send to the Bedrock adapter.

    ``system`` is the fixed internal framing; ``user`` is the assembled,
    sanitized prompt body carrying the context block, the current symptom, and
    the output contract. Neither field is ever returned to the Customer.
    """

    system: str
    user: str


def _render_context_block(context: Iterable[ContextTurn]) -> str:
    """Render the ascending context turns into a single prompt block.

    Each turn's user/model text is sanitized, then placed into the
    ``CONTEXT_TURN`` template via named placeholders. When there is no prior
    context the ``NO_CONTEXT`` sentinel line is used instead.
    """

    lines: list[str] = []
    for index, turn in enumerate(context, start=1):
        lines.append(
            CONTEXT_TURN.format(
                index=index,
                symptom=sanitize_text(turn.symptom),
                diagnosis=sanitize_text(turn.diagnosis),
            )
        )

    if not lines:
        return NO_CONTEXT

    return "\n".join([CONTEXT_HEADER, *lines])


def build_prompt(
    symptom_text: str | None,
    context: Iterable[ContextTurn] = (),
) -> Outcome[PromptPayload]:
    """Validate inputs and assemble a prompt payload for the AI_Engine.

    The current ``symptom_text`` is validated with the shared fail-fast
    validator *before* any template work, so a prompt is never built from empty
    or over-length input. On success, the current symptom and every prior turn
    are sanitized and inserted into the reusable templates via named
    placeholders (never raw-concatenated).

    Args:
        symptom_text: The current Symptom_Description to diagnose.
        context: Prior conversation turns in ascending chronological order
            (oldest first). Defaults to no prior context (first message).

    Returns:
        ``Ok(PromptPayload)`` with the system framing and assembled user prompt
        when the symptom is valid; otherwise the shared validator's
        ``Err("EMPTY")`` or ``Err("TOO_LONG")`` outcome, unchanged.
    """

    # Validate the payload before use (fail-fast, before any template filling).
    validated = validate_symptom(symptom_text)
    if isinstance(validated, Err):
        return validated

    # Insert each sanitized, user-derived value as a ``str.format`` argument
    # (filling a named placeholder) exactly once: the context turns are rendered
    # in ``_render_context_block`` and the current symptom here. Because the
    # values are format *arguments* and not part of any format string, their
    # braces are inert. The already-rendered sections are then joined with fixed
    # separators, never re-formatted, so no value is formatted twice. User input
    # reaches the prompt only through named placeholders (``{symptom}``,
    # ``{diagnosis}``), never raw-concatenated.
    context_block = _render_context_block(context)
    sanitized_symptom = sanitize_text(validated.value)

    user_prompt = SYMPTOM_SECTION.format(symptom=sanitized_symptom)
    user_prompt = "\n\n".join([context_block, user_prompt, OUTPUT_CONTRACT])

    return Ok(PromptPayload(system=SYSTEM_INSTRUCTION, user=user_prompt))
