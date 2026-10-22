"""A page of Interaction_Records for paginated history retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field

from .interaction_record import InteractionRecord

__all__ = ["Page"]


@dataclass(frozen=True, slots=True)
class Page:
    """A single page of history results.

    ``records`` never exceeds 50 entries (Req 5.5). ``nextPageToken`` is an
    opaque encoding of the DynamoDB ``ExclusiveStartKey``, present only when more
    records remain.
    """

    records: tuple[InteractionRecord, ...] = field(default_factory=tuple)
    nextPageToken: str | None = None
