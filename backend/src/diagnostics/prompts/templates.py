"""Reusable prompt template text, kept separate from business logic.

These module-level string constants are the *only* place the AI prompt wording
lives. They are deliberately free of any orchestration or Bedrock SDK code so
the AI_Engine (Task 6.2) can load and fill them without duplicating prompt
text, and so prompts can evolve independently of the service layer
(tech.md: "use reusable prompt templates", "keep prompts separate from business
logic").

Every placeholder is a named ``str.format`` field (e.g. ``{symptom}``) that is
filled from *sanitized* values by :mod:`diagnostics.prompts.builder`. User input
is never concatenated directly into these templates -- it flows through the
sanitizer and the named placeholders only (tech.md: "never concatenate raw user
input directly into prompts").

The templates instruct the model to emit the required 4-field structured output
shape ``{ probableIssue, repairRecommendation, estimatedCost, confidenceScore }``
(tech.md AI response requirements) so the adapter can parse it rather than
returning raw model text.
"""

from __future__ import annotations

__all__ = [
    "SYSTEM_INSTRUCTION",
    "OUTPUT_CONTRACT",
    "CONTEXT_HEADER",
    "CONTEXT_TURN",
    "NO_CONTEXT",
    "SYMPTOM_SECTION",
]

# The fixed system framing. It is internal and never surfaced to the Customer
# (tech.md: "never expose internal prompts to users").
SYSTEM_INSTRUCTION = (
    "You are FleetMate, an automotive diagnostic assistant. Given a vehicle "
    "symptom description and any prior conversation turns, identify the single "
    "most probable issue and a single repair recommendation. Base your answer "
    "only on the information provided; do not invent details. If the "
    "information is insufficient to determine a probable issue, say so instead "
    "of guessing."
)

# Describes the required structured output so the adapter can parse the 4-field
# shape instead of returning raw model text. This is fixed text joined verbatim
# by the builder (never passed through ``str.format``), so its JSON braces are
# written as single, literal braces.
OUTPUT_CONTRACT = (
    "Respond with a single JSON object and nothing else, using exactly these "
    "fields: "
    '{"probableIssue": string, "repairRecommendation": string, '
    '"estimatedCost": string, "confidenceScore": number between 0 and 1}. '
    "If the symptom information is insufficient to determine a probable issue, "
    'respond instead with {"needsMoreDetail": true, "message": string} '
    "asking for the specific additional detail required."
)

# Header printed before the ascending list of prior conversation turns.
CONTEXT_HEADER = "Prior conversation (oldest first):"

# A single prior turn. ``index`` is 1-based for human readability; ``symptom``
# and ``diagnosis`` are sanitized before insertion.
CONTEXT_TURN = "{index}. Customer: {symptom}\n   Assistant: {diagnosis}"

# Rendered when there is no prior context (first turn in a session).
NO_CONTEXT = "Prior conversation: none (this is the first message)."

# The current-symptom section. Its single ``{symptom}`` placeholder is filled by
# the builder with the sanitized current Symptom_Description -- raw user input
# never reaches this string except through that placeholder. The builder joins
# this rendered section with the context block and the output contract using
# fixed separators, so no user-derived text is ever formatted a second time.
SYMPTOM_SECTION = "Current symptom description:\n{symptom}"
