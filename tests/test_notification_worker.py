from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

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
    sent_notification_ids: list[str] = field(default_factory=list)

    def send(self, event: NotificationEvent) -> NotificationSendResult:
        self.sent_notification_ids.append(event.notification_id)
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
