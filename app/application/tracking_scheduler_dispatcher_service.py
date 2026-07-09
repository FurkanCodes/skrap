from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.application.tracking_dispatch_queue import TrackingDispatchJob, TrackingDispatchQueue
from app.application.tracking_due_index import TrackingDueIndex
from app.infrastructure.redis_tracking_due_index import sync_tracked_item_due_index
from app.tracking import TrackedItem, TrackingRepository


@dataclass(frozen=True)
class SchedulerDispatchCycleResult:
    """One dispatcher-loop summary."""

    claimed_count: int
    enqueued_count: int
    released_count: int
    failure_count: int
    duration_seconds: float
    source: str = "fallback"
    stale_count: int = 0


class TrackingSchedulerDispatcherService:
    """Claims due rows and pushes dispatch jobs to queue transport.

    Primary wake path uses Redis due ZSET; Postgres claim remains the lease SoR.
    A slow reconcile claim recovers orphans when Redis drifts.
    """

    def __init__(
        self,
        *,
        repository: TrackingRepository,
        queue: TrackingDispatchQueue,
        claim_batch_size: int,
        lease_seconds: int,
        lease_owner: str,
        due_index: TrackingDueIndex | None = None,
        due_block_seconds: int = 5,
        reconcile_poll_seconds: int = 60,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._claim_batch_size = max(1, claim_batch_size)
        self._lease_seconds = max(1, lease_seconds)
        self._lease_owner = lease_owner
        self._due_index = due_index
        self._due_block_seconds = max(1, due_block_seconds)
        self._reconcile_poll_seconds = max(1, reconcile_poll_seconds)
        self._last_reconcile_at = 0.0

    def run_once(self) -> SchedulerDispatchCycleResult:
        started = time.perf_counter()
        now = _utcnow()
        now_iso = _to_iso(now)
        now_epoch = now.timestamp()

        if self._due_index is not None:
            due_ids = self._due_index.peek_due(
                now_epoch=now_epoch,
                limit=self._claim_batch_size,
            )
            if due_ids:
                return self._dispatch_claimed(
                    self._repository.claim_due_items_by_ids(
                        item_ids=due_ids,
                        now=now_iso,
                        lease_owner=self._lease_owner,
                        lease_until=_to_iso(now + timedelta(seconds=self._lease_seconds)),
                    ),
                    peeked_ids=due_ids,
                    now_iso=now_iso,
                    cycle_started=started,
                    source="due_index",
                )

            if self._should_reconcile():
                return self._dispatch_fallback(now=now, now_iso=now_iso, cycle_started=started)

            self._wait_for_next_due(now_epoch=now_epoch)
            return SchedulerDispatchCycleResult(
                claimed_count=0,
                enqueued_count=0,
                released_count=0,
                failure_count=0,
                duration_seconds=max(0.0, time.perf_counter() - started),
                source="idle",
            )

        return self._dispatch_fallback(now=now, now_iso=now_iso, cycle_started=started)

    def _should_reconcile(self) -> bool:
        return (time.monotonic() - self._last_reconcile_at) >= float(
            self._reconcile_poll_seconds
        )

    def _wait_for_next_due(self, *, now_epoch: float) -> None:
        if self._due_index is None:
            time.sleep(self._due_block_seconds)
            return
        next_due = self._due_index.next_due_epoch(after_epoch=now_epoch)
        if next_due is None:
            time.sleep(self._due_block_seconds)
            return
        wait_seconds = min(
            float(self._due_block_seconds),
            max(0.05, next_due - now_epoch),
        )
        time.sleep(wait_seconds)

    def _dispatch_fallback(
        self,
        *,
        now: datetime,
        now_iso: str,
        cycle_started: float,
    ) -> SchedulerDispatchCycleResult:
        self._last_reconcile_at = time.monotonic()
        claimed = self._repository.claim_due_items(
            now=now_iso,
            lease_owner=self._lease_owner,
            lease_until=_to_iso(now + timedelta(seconds=self._lease_seconds)),
            limit=self._claim_batch_size,
        )
        return self._dispatch_claimed(
            claimed,
            peeked_ids=[],
            now_iso=now_iso,
            cycle_started=cycle_started,
            source="fallback",
        )

    def _dispatch_claimed(
        self,
        claimed: list[TrackedItem],
        *,
        peeked_ids: list[str],
        now_iso: str,
        cycle_started: float,
        source: str,
    ) -> SchedulerDispatchCycleResult:
        claimed_ids = {item.item_id for item in claimed}
        stale_ids = [item_id for item_id in peeked_ids if item_id not in claimed_ids]
        if stale_ids and self._due_index is not None:
            # Not claimable (paused/leased/already advanced) — drop from wake index.
            self._due_index.remove(stale_ids)

        if not claimed:
            return SchedulerDispatchCycleResult(
                claimed_count=0,
                enqueued_count=0,
                released_count=0,
                failure_count=0,
                duration_seconds=max(0.0, time.perf_counter() - cycle_started),
                source=source,
                stale_count=len(stale_ids),
            )

        # Claimed items are leased; remove from due index until worker reschedules.
        if self._due_index is not None:
            self._due_index.remove([item.item_id for item in claimed])

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
                duration_seconds=max(0.0, time.perf_counter() - cycle_started),
                source=source,
                stale_count=len(stale_ids),
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
                duration_seconds=max(0.0, time.perf_counter() - cycle_started),
                source=source,
                stale_count=len(stale_ids),
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
                saved = self._repository.save_item(
                    replace(
                        current_item,
                        lease_owner=None,
                        lease_until=None,
                        last_check_finished_at=finished_at,
                        updated_at=finished_at,
                    )
                )
                sync_tracked_item_due_index(
                    self._due_index,
                    item_id=saved.item_id,
                    status=saved.status,
                    next_check_at=saved.next_check_at,
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
