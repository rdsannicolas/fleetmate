"""Domain enumerations for the diagnostics domain."""

from __future__ import annotations

from enum import Enum

__all__ = ["SessionStatus"]


class SessionStatus(str, Enum):
    """Lifecycle status of a conversation :class:`Session`.

    A session is ``ACTIVE`` while it accepts submissions and becomes ``CLOSED``
    when idle for 30 continuous minutes (Req 3.5) or, optionally, when the
    50-interaction limit is reached (Req 3.4). Inheriting from ``str`` keeps the
    value JSON-serializable and comparable to the stored string.
    """

    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
