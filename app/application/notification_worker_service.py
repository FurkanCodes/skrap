from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.tracking import NotificationEvent, TrackingRepository


@dataclass(frozen=True)
class NotificationSendResult:
    """Provider response for one notification delivery attempt."""

    success: bool
    provider_name: str
    provider_message_id: str | None = None
    error: str | None = None
    retryable: bool = True


class NotificationSender(Protocol):
    """Provider adapter contract used by notifier worker."""

    def send(self, event: NotificationEvent) -> NotificationSendResult:
        ...


@dataclass(frozen=True)
class NotificationWorkerCycleResult:
    """Aggregated notifier cycle metrics for logs and dashboards."""

    claimed_count: int
    sent_count: int
    retry_count: int
    dead_count: int
    failure_count: int
    max_attempt_depth: int
    duration_seconds: float


class NotificationWorkerService:
    """Claims due outbox rows and delivers them with retry/backoff policy."""

    def __init__(
        self,
        *,
        repository: TrackingRepository,
        sender: NotificationSender,
        claim_batch_size: int,
        lease_seconds: int,
        lease_owner: str,
        max_attempts: int,
        base_backoff_seconds: int,
        backoff_max_seconds: int,
        retry_jitter_percent: int,
    ) -> None:
        self._repository = repository
        self._sender = sender
        self._claim_batch_size = max(1, claim_batch_size)
        self._lease_seconds = max(1, lease_seconds)
        self._lease_owner = lease_owner
        self._max_attempts = max(1, max_attempts)
        self._base_backoff_seconds = max(1, base_backoff_seconds)
        self._backoff_max_seconds = max(self._base_backoff_seconds, backoff_max_seconds)
        self._retry_jitter_percent = max(0, retry_jitter_percent)

    def run_once(self) -> NotificationWorkerCycleResult:
        started = time.perf_counter()
        now = _utcnow()
        claimed = self._repository.claim_due_notification_events(
            now=_to_iso(now),
            lease_owner=self._lease_owner,
            lease_until=_to_iso(now + timedelta(seconds=self._lease_seconds)),
            limit=self._claim_batch_size,
        )
        if not claimed:
            return NotificationWorkerCycleResult(
                claimed_count=0,
                sent_count=0,
                retry_count=0,
                dead_count=0,
                failure_count=0,
                max_attempt_depth=0,
                duration_seconds=max(0.0, time.perf_counter() - started),
            )

        sent_count = 0
        retry_count = 0
        dead_count = 0
        failure_count = 0
        max_attempt_depth = 0
        for event in claimed:
            if not self._is_item_active(event.item_id):
                now_iso = _to_iso(_utcnow())
                self._repository.finalize_notification_event(
                    notification_id=event.notification_id,
                    status="dead",
                    attempt_count=event.attempt_count,
                    now=now_iso,
                    next_attempt_at=None,
                    sent_at=None,
                    provider_name="scheduler",
                    provider_message_id=None,
                    error="item_inactive",
                )
                dead_count += 1
                continue

            try:
                send_result = self._sender.send(event)
            except Exception as exc:
                send_result = NotificationSendResult(
                    success=False,
                    provider_name="internal",
                    error=f"internal_send_error: {exc}",
                    retryable=True,
                )

            attempt_count = event.attempt_count + 1
            max_attempt_depth = max(max_attempt_depth, attempt_count)
            now_iso = _to_iso(_utcnow())

            if send_result.success:
                self._repository.finalize_notification_event(
                    notification_id=event.notification_id,
                    status="sent",
                    attempt_count=attempt_count,
                    now=now_iso,
                    next_attempt_at=None,
                    sent_at=now_iso,
                    provider_name=send_result.provider_name,
                    provider_message_id=send_result.provider_message_id,
                    error=None,
                )
                sent_count += 1
                continue

            max_allowed_attempts = min(event.max_attempts, self._max_attempts)
            is_dead = (attempt_count >= max_allowed_attempts) or (not send_result.retryable)
            if is_dead:
                self._repository.finalize_notification_event(
                    notification_id=event.notification_id,
                    status="dead",
                    attempt_count=attempt_count,
                    now=now_iso,
                    next_attempt_at=None,
                    sent_at=None,
                    provider_name=send_result.provider_name,
                    provider_message_id=None,
                    error=send_result.error or "delivery_failed",
                )
                dead_count += 1
                continue

            retry_at = _compute_retry_at(
                base_time=now_iso,
                attempt_count=attempt_count,
                base_backoff_seconds=self._base_backoff_seconds,
                max_backoff_seconds=self._backoff_max_seconds,
                jitter_percent=self._retry_jitter_percent,
            )
            self._repository.finalize_notification_event(
                notification_id=event.notification_id,
                status="failed",
                attempt_count=attempt_count,
                now=now_iso,
                next_attempt_at=retry_at,
                sent_at=None,
                provider_name=send_result.provider_name,
                provider_message_id=None,
                error=send_result.error or "delivery_failed",
            )
            retry_count += 1
            failure_count += 1

        return NotificationWorkerCycleResult(
            claimed_count=len(claimed),
            sent_count=sent_count,
            retry_count=retry_count,
            dead_count=dead_count,
            failure_count=failure_count,
            max_attempt_depth=max_attempt_depth,
            duration_seconds=max(0.0, time.perf_counter() - started),
        )

    def _is_item_active(self, item_id: str) -> bool:
        get_item = getattr(self._repository, "get_item", None)
        if not callable(get_item):
            return True
        try:
            item = get_item(item_id)
        except Exception:
            # Fail-open for notifier availability; scheduler already guards
            # active-only creation of new notifications.
            return True
        if item is None:
            return False
        return getattr(item, "status", "active") == "active"


def _compute_retry_at(
    *,
    base_time: str,
    attempt_count: int,
    base_backoff_seconds: int,
    max_backoff_seconds: int,
    jitter_percent: int,
) -> str:
    now = datetime.fromisoformat(base_time.replace("Z", "+00:00"))
    exponent = max(0, attempt_count - 1)
    raw_backoff = base_backoff_seconds * (2**exponent)
    bounded = min(max_backoff_seconds, raw_backoff)
    jitter_ratio = max(0.0, float(jitter_percent) / 100.0)
    jitter = random.uniform(-bounded * jitter_ratio, bounded * jitter_ratio)
    delay = max(1.0, bounded + jitter)
    return _to_iso(now + timedelta(seconds=delay))


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
