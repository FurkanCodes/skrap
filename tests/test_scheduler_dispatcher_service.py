from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from app.application.tracking_dispatch_queue import TrackingDispatchJob
from app.application.tracking_scheduler_dispatcher_service import (
    TrackingSchedulerDispatcherService,
)
from app.tracking import TrackedItem


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _item(*, item_id: str, now: datetime, next_check_at: str) -> TrackedItem:
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
        status="active",
        check_interval_minutes=60,
        target_amount=None,
        created_at=stamp,
        updated_at=stamp,
        last_checked_at=stamp,
        next_check_at=next_check_at,
        lease_owner=None,
        lease_until=None,
        last_check_started_at=None,
        last_check_finished_at=stamp,
    )


@dataclass
class _RepositoryStub:
    items_by_id: dict[str, TrackedItem]

    def claim_due_items(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[TrackedItem]:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        claimed: list[TrackedItem] = []
        for item in list(self.items_by_id.values()):
            if len(claimed) >= limit:
                break
            if item.status != "active":
                continue
            if item.next_check_at is None:
                continue
            scheduled_dt = datetime.fromisoformat(item.next_check_at.replace("Z", "+00:00"))
            if scheduled_dt > now_dt:
                continue
            updated = replace(
                item,
                lease_owner=lease_owner,
                lease_until=lease_until,
                last_check_started_at=now,
                updated_at=now,
            )
            self.items_by_id[item.item_id] = updated
            claimed.append(updated)
        return claimed

    def get_item(self, item_id: str) -> TrackedItem | None:
        return self.items_by_id.get(item_id)

    def save_item(self, item: TrackedItem) -> TrackedItem:
        self.items_by_id[item.item_id] = item
        return item


@dataclass
class _QueueStub:
    should_fail: bool = False
    jobs: list[TrackingDispatchJob] = field(default_factory=list)

    def enqueue_many(self, jobs: list[TrackingDispatchJob]) -> int:
        if self.should_fail:
            raise RuntimeError("queue unavailable")
        self.jobs.extend(jobs)
        return len(jobs)


def test_dispatcher_enqueues_claimed_items() -> None:
    now = datetime.now(tz=UTC)
    due = _iso(now - timedelta(minutes=1))
    later = _iso(now + timedelta(minutes=5))
    repository = _RepositoryStub(
        items_by_id={
            "trk_due_1": _item(item_id="trk_due_1", now=now, next_check_at=due),
            "trk_due_2": _item(item_id="trk_due_2", now=now, next_check_at=due),
            "trk_later": _item(item_id="trk_later", now=now, next_check_at=later),
        }
    )
    queue = _QueueStub()
    service = TrackingSchedulerDispatcherService(
        repository=repository,
        queue=queue,
        claim_batch_size=10,
        lease_seconds=120,
        lease_owner="scheduler-dispatcher-test",
    )

    result = service.run_once()

    assert result.claimed_count == 2
    assert result.enqueued_count == 2
    assert result.released_count == 0
    assert result.failure_count == 0
    assert {job.item_id for job in queue.jobs} == {"trk_due_1", "trk_due_2"}
    assert repository.items_by_id["trk_due_1"].lease_owner == "scheduler-dispatcher-test"
    assert repository.items_by_id["trk_due_2"].lease_owner == "scheduler-dispatcher-test"
    assert repository.items_by_id["trk_later"].lease_owner is None


def test_dispatcher_releases_claims_when_queue_enqueue_fails() -> None:
    now = datetime.now(tz=UTC)
    due = _iso(now - timedelta(minutes=1))
    repository = _RepositoryStub(
        items_by_id={
            "trk_due_1": _item(item_id="trk_due_1", now=now, next_check_at=due),
        }
    )
    queue = _QueueStub(should_fail=True)
    service = TrackingSchedulerDispatcherService(
        repository=repository,
        queue=queue,
        claim_batch_size=10,
        lease_seconds=120,
        lease_owner="scheduler-dispatcher-test",
    )

    result = service.run_once()

    assert result.claimed_count == 1
    assert result.enqueued_count == 0
    assert result.released_count == 1
    assert result.failure_count >= 1
    assert repository.items_by_id["trk_due_1"].lease_owner is None
    assert repository.items_by_id["trk_due_1"].lease_until is None
