from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from app.application.tracking_dispatch_queue import ClaimedTrackingDispatchJob, TrackingDispatchQueue
from app.application.tracking_scheduler_service import SchedulerCycleResult, TrackingSchedulerService
from app.tracking import TrackedItem, TrackingRepository


class SchedulerItemProcessor(Protocol):
    """Small contract used by queue workers to process claimed items."""

    def run_for_claimed_items(self, claimed: list[TrackedItem]) -> SchedulerCycleResult:
        ...


@dataclass(frozen=True)
class SchedulerWorkerCycleResult:
    """One queue-worker loop summary."""

    claimed_count: int
    acked_count: int
    processed_count: int
    stale_count: int
    failure_count: int
    duration_seconds: float


class TrackingSchedulerWorkerService:
    """Consumes queued scheduler jobs and executes scrape+persist flow."""

    def __init__(
        self,
        *,
        repository: TrackingRepository,
        queue: TrackingDispatchQueue,
        item_processor: SchedulerItemProcessor,
        consumer_name: str,
        claim_batch_size: int,
        block_seconds: int,
        pending_idle_seconds: int,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._item_processor = item_processor
        self._consumer_name = consumer_name
        self._claim_batch_size = max(1, claim_batch_size)
        self._block_seconds = max(1, block_seconds)
        self._pending_idle_seconds = max(1, pending_idle_seconds)

    @classmethod
    def from_scheduler_service(
        cls,
        *,
        repository: TrackingRepository,
        queue: TrackingDispatchQueue,
        scheduler_service: TrackingSchedulerService,
        consumer_name: str,
        claim_batch_size: int,
        block_seconds: int,
        pending_idle_seconds: int,
    ) -> "TrackingSchedulerWorkerService":
        return cls(
            repository=repository,
            queue=queue,
            item_processor=scheduler_service,
            consumer_name=consumer_name,
            claim_batch_size=claim_batch_size,
            block_seconds=block_seconds,
            pending_idle_seconds=pending_idle_seconds,
        )

    def run_once(self) -> SchedulerWorkerCycleResult:
        started = time.perf_counter()
        claimed_jobs = self._queue.claim(
            consumer_name=self._consumer_name,
            max_items=self._claim_batch_size,
            block_seconds=self._block_seconds,
            pending_idle_seconds=self._pending_idle_seconds,
        )
        if not claimed_jobs:
            return SchedulerWorkerCycleResult(
                claimed_count=0,
                acked_count=0,
                processed_count=0,
                stale_count=0,
                failure_count=0,
                duration_seconds=max(0.0, time.perf_counter() - started),
            )

        ack_ids: list[str] = []
        processed_count = 0
        stale_count = 0
        failure_count = 0
        for queued_job in claimed_jobs:
            outcome = self._process_queued_job(queued_job)
            if outcome == "processed":
                processed_count += 1
                ack_ids.append(queued_job.ack_id)
            elif outcome == "stale":
                stale_count += 1
                ack_ids.append(queued_job.ack_id)
            else:
                failure_count += 1

        acked_count = self._queue.ack_many(ack_ids) if ack_ids else 0
        return SchedulerWorkerCycleResult(
            claimed_count=len(claimed_jobs),
            acked_count=acked_count,
            processed_count=processed_count,
            stale_count=stale_count,
            failure_count=failure_count,
            duration_seconds=max(0.0, time.perf_counter() - started),
        )

    def _process_queued_job(self, queued_job: ClaimedTrackingDispatchJob) -> str:
        job = queued_job.job
        item = self._repository.get_item(job.item_id)
        if item is None:
            return "stale"

        # Skip stale messages when the scheduler slot has already moved.
        if (item.next_check_at or "") != job.scheduled_for:
            self._release_lease(item)
            return "stale"
        if item.status != "active":
            self._release_lease(item)
            return "stale"

        try:
            self._item_processor.run_for_claimed_items([item])
            return "processed"
        except Exception:
            return "failed"

    def _release_lease(self, item: TrackedItem) -> None:
        now = _to_iso(_utcnow())
        try:
            self._repository.save_item(
                replace(
                    item,
                    lease_owner=None,
                    lease_until=None,
                    last_check_finished_at=now,
                    updated_at=now,
                )
            )
        except Exception:
            pass


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
