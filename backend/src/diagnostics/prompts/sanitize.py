"""Input sanitization for prompt placeholders.

User-supplied text (the current Symptom_Description and any prior turn text)
must be neutralized before it is inserted into a prompt template, so that it is
treated as *data* rather than as instructions to the model (tech.md: "sanitize
user input", "prompt injection prevention"; design: "user input sanitized ...
never concatenating raw user input into prompts").

Sanitization here is intentionally conservative and lossless-in-meaning:

* Strip characters that could break the structured prompt or smuggle control
  sequences (NULs, most C0/C1 control characters), keeping common whitespace
  (newline, carriage return, tab) which the model handles as plain text.
* Collapse runs of blank lines so a long block of injected newlines cannot push
  the real instructions out of the model's attention window.

Note on template safety: sanitized values are always inserted as *arguments* to
``str.format`` (filling a named placeholder), never spliced into a format
string, so ``{`` / ``}`` in user text are inert and cannot introduce or collide
with our placeholders. The builder guarantees each value is formatted exactly
once; the sanitizer therefore leaves braces untouched so they render verbatim.

The sanitizer is pure: it takes a string and returns a string, performing no
I/O. It does not decide validity (length/emptiness) -- that is the shared
validator's job and is re-checked on the payload by :mod:`.builder`.
"""

from __future__ import annotations

import re

__all__ = ["sanitize_text"]

# C0 and C1 control characters except the common whitespace we keep: tab (\t,
# 0x09), line feed (\n, 0x0A) and carriage return (\r, 0x0D).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Three or more consecutive line breaks collapse to two (a single blank line).
_EXCESS_BLANK_LINES = re.compile(r"(?:\r?\n){3,}")


def sanitize_text(text: str) -> str:
    """Return a prompt-safe version of ``text``.

    The transformation preserves the human-readable meaning of the input while
    removing vectors that could disturb the structured prompt:

    * control characters (other than tab/newline/carriage-return) are removed;
    * runs of 3+ line breaks collapse to a single blank line;
    * surrounding whitespace is trimmed.

    Braces are intentionally left as-is: sanitized text is only ever inserted as
    a ``str.format`` argument, where ``{`` / ``}`` are inert.

    Args:
        text: Raw user-supplied text to be embedded in a prompt placeholder.

    Returns:
        The sanitized text, safe to insert via a named template placeholder.
    """

    # Remove disallowed control characters first.
    cleaned = _CONTROL_CHARS.sub("", text)

    # Collapse excessive blank lines to limit attention-flooding.
    cleaned = _EXCESS_BLANK_LINES.sub("\n\n", cleaned)

    return cleaned.strip()
