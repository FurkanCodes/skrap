from __future__ import annotations

from dataclasses import dataclass, field

from app.application.notification_dispatch_queue import NotificationDispatchJob
from app.application.tracking_scheduler_service import TrackingSchedulerService
from app.tracking import NotificationEvent


@dataclass
class _FakeNotificationQueue:
    enqueued: list[NotificationDispatchJob] = field(default_factory=list)
    fail: bool = False

    def enqueue_many(self, jobs: list[NotificationDispatchJob]) -> int:
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.enqueued.extend(jobs)
        return len(jobs)

    def claim(self, **_kwargs):  # noqa: ANN003
        return []

    def ack_many(self, ack_ids: list[str]) -> int:
        return len(ack_ids)


@dataclass
class _FakeRepository:
    inserted: bool = True
    events: list[NotificationEvent] = field(default_factory=list)

    def enqueue_notification_event(self, event: NotificationEvent) -> bool:
        self.events.append(event)
        return self.inserted


class _DummyScrapeRunner:
    def scrape(self, payload):  # noqa: ANN001, ARG002
        raise AssertionError("scrape should not run in dual-write unit test")


def _service(*, repository: _FakeRepository, queue: _FakeNotificationQueue | None) -> TrackingSchedulerService:
    return TrackingSchedulerService(
        repository=repository,  # type: ignore[arg-type]
        scrape_runner=_DummyScrapeRunner(),  # type: ignore[arg-type]
        timeout_seconds=30,
        claim_batch_size=10,
        max_inflight=1,
        lease_seconds=30,
        jitter_percent=0,
        lease_owner="test-owner",
        notification_queue=queue,
    )


def _notification() -> NotificationEvent:
    return NotificationEvent(
        notification_id="nev_dual",
        idempotency_key="nev_dual:log",
        alert_event_id=None,
        item_id="trk_1",
        channel="log",
        payload="{}",
        status="queued",
        attempt_count=0,
        max_attempts=5,
        next_attempt_at="2026-07-09T12:00:00Z",
        last_attempt_at=None,
        sent_at=None,
        provider_name=None,
        provider_message_id=None,
        last_error=None,
        lease_owner=None,
        lease_until=None,
        created_at="2026-07-09T12:00:00Z",
        updated_at="2026-07-09T12:00:00Z",
    )


def test_persist_and_wake_enqueues_on_successful_insert() -> None:
    repository = _FakeRepository(inserted=True)
    queue = _FakeNotificationQueue()
    service = _service(repository=repository, queue=queue)

    service._persist_and_wake_notification(_notification())

    assert len(repository.events) == 1
    assert [job.notification_id for job in queue.enqueued] == ["nev_dual"]


def test_persist_and_wake_skips_queue_when_insert_conflicts() -> None:
    repository = _FakeRepository(inserted=False)
    queue = _FakeNotificationQueue()
    service = _service(repository=repository, queue=queue)

    service._persist_and_wake_notification(_notification())

    assert len(repository.events) == 1
    assert queue.enqueued == []


def test_persist_and_wake_swallows_queue_errors() -> None:
    repository = _FakeRepository(inserted=True)
    queue = _FakeNotificationQueue(fail=True)
    service = _service(repository=repository, queue=queue)

    service._persist_and_wake_notification(_notification())

    assert len(repository.events) == 1
