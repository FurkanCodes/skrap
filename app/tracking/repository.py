from __future__ import annotations

from typing import Protocol

from app.tracking.domain import (
    PriceSnapshot,
    TrackingBootstrapIdempotencyRecord,
    TrackedItem,
    TrackingCheckRun,
    TrackingCheckRunStatus,
)


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

    def save_item_and_snapshot(self, item: TrackedItem, snapshot: PriceSnapshot) -> TrackedItem:
        ...

    def delete_item(self, item_id: str) -> bool:
        ...

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        ...

    def list_snapshots(self, item_id: str, limit: int) -> list[PriceSnapshot]:
        ...

    def claim_due_items(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[TrackedItem]:
        ...

    def start_check_run(
        self,
        *,
        run_id: str,
        item_id: str,
        scheduled_for: str,
        job_key: str,
        started_at: str,
    ) -> TrackingCheckRun:
        ...

    def finish_check_run(
        self,
        *,
        run_id: str,
        status: TrackingCheckRunStatus,
        finished_at: str,
        error: str | None,
    ) -> None:
        ...

    def list_check_runs(self, item_id: str, limit: int) -> list[TrackingCheckRun]:
        ...

    def get_bootstrap_idempotency_record(
        self,
        idempotency_key: str,
    ) -> TrackingBootstrapIdempotencyRecord | None:
        ...

    def save_bootstrap_idempotency_record(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        ...
