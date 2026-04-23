from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.application.tracking_dispatch_queue import (
    ClaimedTrackingDispatchJob,
    TrackingDispatchJob,
)
from app.application.tracking_scheduler_service import SchedulerCycleResult
from app.application.tracking_scheduler_worker_service import TrackingSchedulerWorkerService
from app.tracking import TrackedItem


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _item(*, item_id: str, now: datetime, next_check_at: str, status: str = "active") -> TrackedItem:
    stamp = _iso(now)
    return TrackedItem(
        item_id=item_id,
        source_url=f"https://example.test/{item_id}",
        canonical_url=f"https://example.test/{item_id}",
        product_id=f"prd_{item_id}",
        site_key="generic",
        adapter_key="generic",
        product_name="Product",
        brand=None,
        model=None,
        sku=None,
        gtin=None,
        image_url=None,
        availability=None,
        currency="USD",
        last_amount="100.00",
        last_raw_text="$100.00",
        last_source="test:init",
        status=status,
        check_interval_minutes=60,
        target_amount=None,
        created_at=stamp,
        updated_at=stamp,
        last_checked_at=stamp,
        next_check_at=next_check_at,
        lease_owner="scheduler-dispatcher-test",
        lease_until=_iso(now),
        last_check_started_at=stamp,
        last_check_finished_at=stamp,
    )


@dataclass
class _RepositoryStub:
    items_by_id: dict[str, TrackedItem]
    saved_items: list[TrackedItem] = field(default_factory=list)

    def get_item(self, item_id: str) -> TrackedItem | None:
        return self.items_by_id.get(item_id)

    def save_item(self, item: TrackedItem) -> TrackedItem:
        self.saved_items.append(item)
        self.items_by_id[item.item_id] = item
        return item


@dataclass
class _QueueStub:
    claimed_jobs: list[ClaimedTrackingDispatchJob]
    acked_ids: list[str] = field(default_factory=list)

    def enqueue_many(self, jobs: list[TrackingDispatchJob]) -> int:
        return len(jobs)

    def claim(
        self,
        *,
        consumer_name: str,
        max_items: int,
        block_seconds: int,
        pending_idle_seconds: int,
    ) -> list[ClaimedTrackingDispatchJob]:
        del consumer_name, max_items, block_seconds, pending_idle_seconds
        jobs = self.claimed_jobs[:]
        self.claimed_jobs.clear()
        return jobs

    def ack_many(self, ack_ids: list[str]) -> int:
        self.acked_ids.extend(ack_ids)
        return len(ack_ids)


@dataclass
class _ProcessorStub:
    should_raise: bool = False
    processed_item_ids: list[str] = field(default_factory=list)

    def run_for_claimed_items(self, claimed: list[TrackedItem]) -> SchedulerCycleResult:
        if self.should_raise:
            raise RuntimeError("processor failed")
        self.processed_item_ids.extend(item.item_id for item in claimed)
        return SchedulerCycleResult(
            claimed_count=len(claimed),
            processed_count=len(claimed),
            skipped_count=0,
            success_count=len(claimed),
            failure_count=0,
            duration_seconds=0.0,
        )


def _queued_job(*, item_id: str, scheduled_for: str) -> ClaimedTrackingDispatchJob:
    return ClaimedTrackingDispatchJob(
        ack_id=f"{item_id}-ack",
        job=TrackingDispatchJob(
            item_id=item_id,
            scheduled_for=scheduled_for,
            claimed_at=scheduled_for,
            lease_owner="scheduler-dispatcher-test",
        ),
    )


def test_worker_processes_matching_job_and_acks() -> None:
    now = datetime.now(tz=UTC)
    scheduled_for = _iso(now)
    repository = _RepositoryStub(
        items_by_id={"trk_1": _item(item_id="trk_1", now=now, next_check_at=scheduled_for)}
    )
    queue = _QueueStub(claimed_jobs=[_queued_job(item_id="trk_1", scheduled_for=scheduled_for)])
    processor = _ProcessorStub()
    service = TrackingSchedulerWorkerService(
        repository=repository,
        queue=queue,
        item_processor=processor,
        consumer_name="worker-a",
        claim_batch_size=10,
        block_seconds=1,
        pending_idle_seconds=30,
    )

    result = service.run_once()

    assert result.claimed_count == 1
    assert result.processed_count == 1
    assert result.stale_count == 0
    assert result.failure_count == 0
    assert result.acked_count == 1
    assert processor.processed_item_ids == ["trk_1"]
    assert queue.acked_ids == ["trk_1-ack"]


def test_worker_marks_stale_job_and_releases_lease() -> None:
    now = datetime.now(tz=UTC)
    item_scheduled_for = _iso(now)
    queued_scheduled_for = _iso(now - timedelta(seconds=1))
    item = _item(item_id="trk_1", now=now, next_check_at=item_scheduled_for)
    repository = _RepositoryStub(items_by_id={"trk_1": item})
    queue = _QueueStub(
        claimed_jobs=[_queued_job(item_id="trk_1", scheduled_for=queued_scheduled_for)]
    )
    processor = _ProcessorStub()
    service = TrackingSchedulerWorkerService(
        repository=repository,
        queue=queue,
        item_processor=processor,
        consumer_name="worker-a",
        claim_batch_size=10,
        block_seconds=1,
        pending_idle_seconds=30,
    )

    result = service.run_once()

    assert queued_scheduled_for != item_scheduled_for
    assert result.claimed_count == 1
    assert result.processed_count == 0
    assert result.stale_count == 1
    assert result.failure_count == 0
    assert result.acked_count == 1
    assert processor.processed_item_ids == []
    assert repository.items_by_id["trk_1"].lease_owner is None
    assert repository.items_by_id["trk_1"].lease_until is None


def test_worker_does_not_ack_when_processor_fails() -> None:
    now = datetime.now(tz=UTC)
    scheduled_for = _iso(now)
    repository = _RepositoryStub(
        items_by_id={"trk_1": _item(item_id="trk_1", now=now, next_check_at=scheduled_for)}
    )
    queue = _QueueStub(claimed_jobs=[_queued_job(item_id="trk_1", scheduled_for=scheduled_for)])
    processor = _ProcessorStub(should_raise=True)
    service = TrackingSchedulerWorkerService(
        repository=repository,
        queue=queue,
        item_processor=processor,
        consumer_name="worker-a",
        claim_batch_size=10,
        block_seconds=1,
        pending_idle_seconds=30,
    )

    result = service.run_once()

    assert result.claimed_count == 1
    assert result.processed_count == 0
    assert result.stale_count == 0
    assert result.failure_count == 1
    assert result.acked_count == 0
    assert queue.acked_ids == []
