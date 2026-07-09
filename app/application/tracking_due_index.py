from __future__ import annotations

from typing import Protocol


class TrackingDueIndex(Protocol):
    """Redis-backed wake index for due tracked items."""

    def upsert(self, *, item_id: str, due_at_epoch: float) -> None:
        """Schedule or reschedule an active item."""
        ...

    def remove(self, item_ids: list[str]) -> int:
        """Remove one or more items from the due index."""
        ...

    def peek_due(self, *, now_epoch: float, limit: int) -> list[str]:
        """Return due item ids without removing them."""
        ...

    def next_due_epoch(self, *, after_epoch: float) -> float | None:
        """Return the next future due score, if any."""
        ...
