"""Reusable AI prompt templates kept separate from business logic.

Public API for the AI_Engine (Task 6.2):

* :class:`ContextTurn` -- one prior conversation turn supplied as context.
* :class:`PromptPayload` -- a validated prompt (system + user) ready for Bedrock.
* :func:`build_prompt` -- validate the current symptom, sanitize all user input,
  and assemble the prompt from reusable templates via named placeholders.
* :func:`sanitize_text` -- the input sanitizer used for placeholder values.

The raw template strings live in :mod:`diagnostics.prompts.templates` and are
intentionally free of orchestration/SDK code so prompts can evolve independently
of the service layer.
"""

from diagnostics.prompts.builder import ContextTurn, PromptPayload, build_prompt
from diagnostics.prompts.sanitize import sanitize_text

__all__ = ["ContextTurn", "PromptPayload", "build_prompt", "sanitize_text"]
