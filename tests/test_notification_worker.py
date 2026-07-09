from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from app.application.notification_dispatch_queue import (
    ClaimedNotificationDispatchJob,
    NotificationDispatchJob,
)
from app.application.notification_worker_service import (
    NotificationSendResult,
    NotificationWorkerService,
)
from app.tracking import NotificationEvent


@dataclass(frozen=True)
class _TrackedItemStub:
    status: str


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _event(
    *,
    notification_id: str,
    now: datetime,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    next_attempt_at: str | None = None,
) -> NotificationEvent:
    stamp = _iso(now)
    return NotificationEvent(
        notification_id=notification_id,
        idempotency_key=f"{notification_id}:log",
        alert_event_id=f"aev_{notification_id}",
        item_id="trk_test",
        channel="log",
        payload='{"hello":"world"}',
        status=status,  # type: ignore[arg-type]
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        next_attempt_at=next_attempt_at or stamp,
        last_attempt_at=None,
        sent_at=None,
        provider_name=None,
        provider_message_id=None,
        last_error=None,
        lease_owner=None,
        lease_until=None,
        created_at=stamp,
        updated_at=stamp,
    )


@dataclass
class _FakeSender:
    result_by_notification_id: dict[str, NotificationSendResult]
    raise_for_notification_ids: set[str] = field(default_factory=set)
    sent_notification_ids: list[str] = field(default_factory=list)

    def send(self, event: NotificationEvent) -> NotificationSendResult:
        self.sent_notification_ids.append(event.notification_id)
        if event.notification_id in self.raise_for_notification_ids:
            raise RuntimeError("sender exploded")
        return self.result_by_notification_id[event.notification_id]


@dataclass
class _InMemoryNotificationRepository:
    events_by_id: dict[str, NotificationEvent]
    items_by_id: dict[str, object] = field(default_factory=dict)

    def claim_due_notification_events(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[NotificationEvent]:
        if limit < 1:
            return []
        candidates = [
            event
            for event in self.events_by_id.values()
            if event.status in {"queued", "failed", "sending"}
            and event.status not in {"sent", "dead"}
            and event.next_attempt_at <= now
            and (event.lease_until is None or event.lease_until <= now)
        ]
        candidates.sort(key=lambda event: (event.next_attempt_at, event.notification_id))
        claimed = candidates[:limit]

        updated: list[NotificationEvent] = []
        for event in claimed:
            next_event = replace(
                event,
                status="sending",
                lease_owner=lease_owner,
                lease_until=lease_until,
                last_attempt_at=now,
                updated_at=now,
            )
            self.events_by_id[event.notification_id] = next_event
            updated.append(next_event)
        return updated

    def claim_notification_events_by_ids(
        self,
        *,
        notification_ids: list[str],
        now: str,
        lease_owner: str,
        lease_until: str,
    ) -> list[NotificationEvent]:
        claimed: list[NotificationEvent] = []
        for notification_id in notification_ids:
            event = self.events_by_id.get(notification_id)
            if event is None:
                continue
            if event.status not in {"queued", "failed", "sending"}:
                continue
            if event.next_attempt_at > now:
                continue
            if event.lease_until is not None and event.lease_until > now:
                continue
            next_event = replace(
                event,
                status="sending",
                lease_owner=lease_owner,
                lease_until=lease_until,
                last_attempt_at=now,
                updated_at=now,
            )
            self.events_by_id[notification_id] = next_event
            claimed.append(next_event)
        return claimed

    def finalize_notification_event(
        self,
        *,
        notification_id: str,
        status: str,
        attempt_count: int,
        now: str,
        next_attempt_at: str | None,
        sent_at: str | None,
        provider_name: str | None,
        provider_message_id: str | None,
        error: str | None,
    ) -> None:
        current = self.events_by_id[notification_id]
        self.events_by_id[notification_id] = replace(
            current,
            status=status,  # type: ignore[arg-type]
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at or current.next_attempt_at,
            sent_at=sent_at,
            provider_name=provider_name,
            provider_message_id=provider_message_id,
            last_error=error,
            lease_owner=None,
            lease_until=None,
            updated_at=now,
        )

    def get_item(self, item_id: str):
        item = self.items_by_id.get(item_id)
        if item is not None:
            return item
        return _TrackedItemStub(status="active")


def test_notification_worker_marks_sent_on_success() -> None:
    now = datetime.now(tz=UTC)
    event = _event(notification_id="nev_success", now=now)
    repository = _InMemoryNotificationRepository(events_by_id={event.notification_id: event})
    sender = _FakeSender(
        result_by_notification_id={
            event.notification_id: NotificationSendResult(
                success=True,
                provider_name="log",
                provider_message_id="msg-1",
                retryable=False,
            )
        }
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=5,
        backoff_max_seconds=60,
        retry_jitter_percent=0,
    )

    result = service.run_once()
    saved = repository.events_by_id[event.notification_id]

    assert result.claimed_count == 1
    assert result.sent_count == 1
    assert result.retry_count == 0
    assert result.dead_count == 0
    assert saved.status == "sent"
    assert saved.attempt_count == 1
    assert saved.provider_message_id == "msg-1"
    assert saved.sent_at is not None


def test_notification_worker_retries_with_backoff_on_retryable_failure() -> None:
    now = datetime.now(tz=UTC)
    event = _event(notification_id="nev_retry", now=now, max_attempts=4)
    repository = _InMemoryNotificationRepository(events_by_id={event.notification_id: event})
    sender = _FakeSender(
        result_by_notification_id={
            event.notification_id: NotificationSendResult(
                success=False,
                provider_name="webhook",
                error="timeout",
                retryable=True,
            )
        }
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=10,
        backoff_max_seconds=300,
        retry_jitter_percent=0,
    )

    result = service.run_once()
    saved = repository.events_by_id[event.notification_id]

    assert result.claimed_count == 1
    assert result.sent_count == 0
    assert result.retry_count == 1
    assert result.dead_count == 0
    assert result.failure_count == 1
    assert saved.status == "failed"
    assert saved.attempt_count == 1
    assert saved.last_error == "timeout"
    next_attempt = datetime.fromisoformat(saved.next_attempt_at.replace("Z", "+00:00"))
    assert next_attempt > now


def test_notification_worker_moves_to_dead_after_max_attempts() -> None:
    now = datetime.now(tz=UTC)
    event = _event(
        notification_id="nev_dead",
        now=now - timedelta(minutes=1),
        attempt_count=2,
        max_attempts=3,
    )
    repository = _InMemoryNotificationRepository(events_by_id={event.notification_id: event})
    sender = _FakeSender(
        result_by_notification_id={
            event.notification_id: NotificationSendResult(
                success=False,
                provider_name="webhook",
                error="rejected",
                retryable=True,
            )
        }
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=10,
        backoff_max_seconds=300,
        retry_jitter_percent=0,
    )

    result = service.run_once()
    saved = repository.events_by_id[event.notification_id]

    assert result.dead_count == 1
    assert result.retry_count == 0
    assert saved.status == "dead"
    assert saved.attempt_count == 3
    assert saved.last_error == "rejected"


def test_notification_worker_moves_to_dead_on_non_retryable_error() -> None:
    now = datetime.now(tz=UTC)
    event = _event(notification_id="nev_terminal", now=now, max_attempts=5)
    repository = _InMemoryNotificationRepository(events_by_id={event.notification_id: event})
    sender = _FakeSender(
        result_by_notification_id={
            event.notification_id: NotificationSendResult(
                success=False,
                provider_name="email",
                error="invalid_destination",
                retryable=False,
            )
        }
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=10,
        backoff_max_seconds=300,
        retry_jitter_percent=0,
    )

    result = service.run_once()
    saved = repository.events_by_id[event.notification_id]

    assert result.dead_count == 1
    assert saved.status == "dead"
    assert saved.attempt_count == 1
    assert saved.last_error == "invalid_destination"


def test_notification_worker_drops_event_when_item_is_paused() -> None:
    now = datetime.now(tz=UTC)
    event = _event(notification_id="nev_paused", now=now, max_attempts=5)
    repository = _InMemoryNotificationRepository(
        events_by_id={event.notification_id: event},
        items_by_id={
            "trk_test": _TrackedItemStub(status="paused")
        },
    )
    sender = _FakeSender(
        result_by_notification_id={
            event.notification_id: NotificationSendResult(
                success=True,
                provider_name="log",
                provider_message_id="msg-1",
                retryable=False,
            )
        }
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=10,
        backoff_max_seconds=300,
        retry_jitter_percent=0,
    )

    result = service.run_once()
    saved = repository.events_by_id[event.notification_id]

    assert result.dead_count == 1
    assert result.sent_count == 0
    assert saved.status == "dead"
    assert saved.last_error == "item_inactive"
    assert sender.sent_notification_ids == []


def test_notification_worker_processes_batch_in_parallel_when_inflight_enabled() -> None:
    now = datetime.now(tz=UTC)
    events = {
        "nev_1": _event(notification_id="nev_1", now=now),
        "nev_2": _event(notification_id="nev_2", now=now),
        "nev_3": _event(notification_id="nev_3", now=now),
    }
    repository = _InMemoryNotificationRepository(events_by_id=events.copy())
    sender = _FakeSender(
        result_by_notification_id={
            notification_id: NotificationSendResult(
                success=True,
                provider_name="log",
                provider_message_id=f"msg-{notification_id}",
                retryable=False,
            )
            for notification_id in events
        }
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=10,
        backoff_max_seconds=300,
        retry_jitter_percent=0,
        max_inflight=3,
    )

    result = service.run_once()

    assert result.claimed_count == 3
    assert result.sent_count == 3
    assert result.retry_count == 0
    assert result.dead_count == 0
    assert result.failure_count == 0
    assert set(sender.sent_notification_ids) == {"nev_1", "nev_2", "nev_3"}
    for event in repository.events_by_id.values():
        assert event.status == "sent"
        assert event.sent_at is not None


def test_notification_worker_parallel_send_exception_becomes_retryable_failure() -> None:
    now = datetime.now(tz=UTC)
    success_event = _event(notification_id="nev_ok", now=now)
    failing_event = _event(notification_id="nev_boom", now=now)
    repository = _InMemoryNotificationRepository(
        events_by_id={
            success_event.notification_id: success_event,
            failing_event.notification_id: failing_event,
        }
    )
    sender = _FakeSender(
        result_by_notification_id={
            success_event.notification_id: NotificationSendResult(
                success=True,
                provider_name="log",
                provider_message_id="msg-ok",
                retryable=False,
            ),
            failing_event.notification_id: NotificationSendResult(
                success=True,
                provider_name="log",
                provider_message_id="msg-unused",
                retryable=False,
            ),
        },
        raise_for_notification_ids={failing_event.notification_id},
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=10,
        backoff_max_seconds=300,
        retry_jitter_percent=0,
        max_inflight=2,
    )

    result = service.run_once()

    assert result.claimed_count == 2
    assert result.sent_count == 1
    assert result.retry_count == 1
    assert result.dead_count == 0
    assert result.failure_count == 1
    saved_success = repository.events_by_id[success_event.notification_id]
    assert saved_success.status == "sent"
    saved_failure = repository.events_by_id[failing_event.notification_id]
    assert saved_failure.status == "failed"
    assert saved_failure.last_error is not None
    assert "internal_send_error" in saved_failure.last_error


@dataclass
class _FakeNotificationQueue:
    claim_batches: list[list[ClaimedNotificationDispatchJob]]
    enqueued: list[NotificationDispatchJob] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)

    def enqueue_many(self, jobs: list[NotificationDispatchJob]) -> int:
        self.enqueued.extend(jobs)
        return len(jobs)

    def claim(
        self,
        *,
        consumer_name: str,
        max_items: int,
        block_seconds: int,
        pending_idle_seconds: int,
    ) -> list[ClaimedNotificationDispatchJob]:
        del consumer_name, max_items, block_seconds, pending_idle_seconds
        if not self.claim_batches:
            return []
        return self.claim_batches.pop(0)

    def ack_many(self, ack_ids: list[str]) -> int:
        self.acked.extend(ack_ids)
        return len(ack_ids)


def test_notification_worker_stream_path_sends_and_acks() -> None:
    now = datetime.now(tz=UTC)
    event = _event(notification_id="nev_stream", now=now)
    repository = _InMemoryNotificationRepository(events_by_id={event.notification_id: event})
    sender = _FakeSender(
        result_by_notification_id={
            event.notification_id: NotificationSendResult(
                success=True,
                provider_name="log",
                provider_message_id="msg-stream",
                retryable=False,
            )
        }
    )
    queue = _FakeNotificationQueue(
        claim_batches=[
            [
                ClaimedNotificationDispatchJob(
                    ack_id="1-0",
                    job=NotificationDispatchJob(notification_id=event.notification_id),
                )
            ]
        ]
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=5,
        backoff_max_seconds=60,
        retry_jitter_percent=0,
        notification_queue=queue,
        fallback_poll_seconds=60,
    )

    result = service.run_once()
    saved = repository.events_by_id[event.notification_id]

    assert result.source == "stream"
    assert result.claimed_count == 1
    assert result.sent_count == 1
    assert result.acked_count == 1
    assert result.stale_count == 0
    assert queue.acked == ["1-0"]
    assert saved.status == "sent"


def test_notification_worker_acks_stale_stream_entries_missing_in_postgres() -> None:
    now = datetime.now(tz=UTC)
    repository = _InMemoryNotificationRepository(events_by_id={})
    sender = _FakeSender(result_by_notification_id={})
    queue = _FakeNotificationQueue(
        claim_batches=[
            [
                ClaimedNotificationDispatchJob(
                    ack_id="9-0",
                    job=NotificationDispatchJob(notification_id="nev_missing"),
                )
            ]
        ]
    )
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=5,
        backoff_max_seconds=60,
        retry_jitter_percent=0,
        notification_queue=queue,
        fallback_poll_seconds=60,
    )

    result = service.run_once()

    assert result.source == "stream"
    assert result.claimed_count == 0
    assert result.stale_count == 1
    assert result.acked_count == 1
    assert queue.acked == ["9-0"]
    assert sender.sent_notification_ids == []


def test_notification_worker_uses_fallback_when_stream_empty() -> None:
    now = datetime.now(tz=UTC)
    event = _event(notification_id="nev_fallback", now=now)
    repository = _InMemoryNotificationRepository(events_by_id={event.notification_id: event})
    sender = _FakeSender(
        result_by_notification_id={
            event.notification_id: NotificationSendResult(
                success=True,
                provider_name="log",
                provider_message_id="msg-fallback",
                retryable=False,
            )
        }
    )
    queue = _FakeNotificationQueue(claim_batches=[[]])
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=5,
        backoff_max_seconds=60,
        retry_jitter_percent=0,
        notification_queue=queue,
        fallback_poll_seconds=1,
    )
    service._last_fallback_at = 0.0

    result = service.run_once()
    saved = repository.events_by_id[event.notification_id]

    assert result.source == "fallback"
    assert result.claimed_count == 1
    assert result.sent_count == 1
    assert saved.status == "sent"


def test_notification_worker_skips_fallback_when_recently_run() -> None:
    now = datetime.now(tz=UTC)
    event = _event(notification_id="nev_idle", now=now)
    repository = _InMemoryNotificationRepository(events_by_id={event.notification_id: event})
    sender = _FakeSender(result_by_notification_id={})
    queue = _FakeNotificationQueue(claim_batches=[[]])
    service = NotificationWorkerService(
        repository=repository,
        sender=sender,
        claim_batch_size=10,
        lease_seconds=30,
        lease_owner="notifier-test",
        max_attempts=5,
        base_backoff_seconds=5,
        backoff_max_seconds=60,
        retry_jitter_percent=0,
        notification_queue=queue,
        fallback_poll_seconds=60,
    )
    service._last_fallback_at = time.monotonic()

    result = service.run_once()

    assert result.source == "idle"
    assert result.claimed_count == 0
    assert sender.sent_notification_ids == []
    assert repository.events_by_id[event.notification_id].status == "queued"
