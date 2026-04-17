from __future__ import annotations

from typing import Protocol

from app.tracking.domain import PriceSnapshot, TrackedItem


class TrackingRepository(Protocol):
    """Persistence boundary for tracked items and snapshots."""

    def initialize(self) -> None:
        ...

    def list_items(self) -> list[TrackedItem]:
        ...

    def get_item(self, item_id: str) -> TrackedItem | None:
        ...

    def get_item_by_source_url(self, source_url: str) -> TrackedItem | None:
        ...

    def save_item(self, item: TrackedItem) -> TrackedItem:
        ...

    def delete_item(self, item_id: str) -> bool:
        ...

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        ...

    def list_snapshots(self, item_id: str, limit: int) -> list[PriceSnapshot]:
        ...
