from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.application.tracking_dispatch_queue import TrackingDispatchJob, TrackingDispatchQueue
from app.tracking import TrackedItem, TrackingRepository


@dataclass(frozen=True)
class SchedulerDispatchCycleResult:
    """One dispatcher-loop summary."""

    claimed_count: int
    enqueued_count: int
    released_count: int
    failure_count: int
    duration_seconds: float


class TrackingSchedulerDispatcherService:
    """Claims due rows and pushes dispatch jobs to queue transport."""

    def __init__(
        self,
        *,
        repository: TrackingRepository,
        queue: TrackingDispatchQueue,
        claim_batch_size: int,
        lease_seconds: int,
        lease_owner: str,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._claim_batch_size = max(1, claim_batch_size)
        self._lease_seconds = max(1, lease_seconds)
        self._lease_owner = lease_owner

    def run_once(self) -> SchedulerDispatchCycleResult:
        started = time.perf_counter()
        now = _utcnow()
        now_iso = _to_iso(now)
        claimed = self._repository.claim_due_items(
            now=now_iso,
            lease_owner=self._lease_owner,
            lease_until=_to_iso(now + timedelta(seconds=self._lease_seconds)),
            limit=self._claim_batch_size,
        )
        if not claimed:
            return SchedulerDispatchCycleResult(
                claimed_count=0,
                enqueued_count=0,
                released_count=0,
                failure_count=0,
                duration_seconds=max(0.0, time.perf_counter() - started),
            )

        jobs = [
            TrackingDispatchJob(
                item_id=item.item_id,
                scheduled_for=item.next_check_at or now_iso,
                claimed_at=now_iso,
                lease_owner=self._lease_owner,
            )
            for item in claimed
        ]
        try:
            enqueued_count = self._queue.enqueue_many(jobs)
            if enqueued_count != len(jobs):
                raise RuntimeError(
                    f"enqueue_count_mismatch expected={len(jobs)} actual={enqueued_count}"
                )
            return SchedulerDispatchCycleResult(
                claimed_count=len(claimed),
                enqueued_count=enqueued_count,
                released_count=0,
                failure_count=0,
                duration_seconds=max(0.0, time.perf_counter() - started),
            )
        except Exception:
            released_count, release_failures = self._release_claimed_items(
                items=claimed,
                finished_at=now_iso,
            )
            return SchedulerDispatchCycleResult(
                claimed_count=len(claimed),
                enqueued_count=0,
                released_count=released_count,
                failure_count=max(1, release_failures),
                duration_seconds=max(0.0, time.perf_counter() - started),
            )

    def _release_claimed_items(
        self,
        *,
        items: list[TrackedItem],
        finished_at: str,
    ) -> tuple[int, int]:
        released_count = 0
        release_failures = 0
        for item in items:
            current_item = self._get_latest_item(item)
            try:
                self._repository.save_item(
                    replace(
                        current_item,
                        lease_owner=None,
                        lease_until=None,
                        last_check_finished_at=finished_at,
                        updated_at=finished_at,
                    )
                )
                released_count += 1
            except Exception:
                release_failures += 1
        return released_count, release_failures

    def _get_latest_item(self, item: TrackedItem) -> TrackedItem:
        get_item = getattr(self._repository, "get_item", None)
        if not callable(get_item):
            return item
        try:
            latest = get_item(item.item_id)
        except Exception:
            return item
        if latest is None:
            return item
        return latest


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
