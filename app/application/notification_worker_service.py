from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.application.notification_dispatch_queue import (
    ClaimedNotificationDispatchJob,
    NotificationDispatchQueue,
)
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
    source: str = "fallback"
    acked_count: int = 0
    stale_count: int = 0


class NotificationWorkerService:
    """Delivers outbox rows via Redis wake path with Postgres fallback."""

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
        max_inflight: int = 1,
        notification_queue: NotificationDispatchQueue | None = None,
        consumer_name: str = "notifier",
        block_seconds: int = 5,
        pending_idle_seconds: int = 60,
        fallback_poll_seconds: int = 60,
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
        self._max_inflight = max(1, max_inflight)
        self._notification_queue = notification_queue
        self._consumer_name = consumer_name
        self._block_seconds = max(1, block_seconds)
        self._pending_idle_seconds = max(1, pending_idle_seconds)
        self._fallback_poll_seconds = max(1, fallback_poll_seconds)
        self._last_fallback_at = 0.0

    def run_once(self) -> NotificationWorkerCycleResult:
        started = time.perf_counter()
        now = _utcnow()
        if self._notification_queue is not None:
            claimed_jobs = self._notification_queue.claim(
                consumer_name=self._consumer_name,
                max_items=self._claim_batch_size,
                block_seconds=self._block_seconds,
                pending_idle_seconds=self._pending_idle_seconds,
            )
            if claimed_jobs:
                return self._process_stream_jobs(
                    claimed_jobs,
                    now=now,
                    cycle_started=started,
                )
            if self._should_run_fallback():
                return self._process_fallback_claim(now=now, cycle_started=started)
            return NotificationWorkerCycleResult(
                claimed_count=0,
                sent_count=0,
                retry_count=0,
                dead_count=0,
                failure_count=0,
                max_attempt_depth=0,
                duration_seconds=max(0.0, time.perf_counter() - started),
                source="idle",
            )

        return self._process_fallback_claim(now=now, cycle_started=started)

    def _should_run_fallback(self) -> bool:
        return (time.monotonic() - self._last_fallback_at) >= float(
            self._fallback_poll_seconds
        )

    def _process_stream_jobs(
        self,
        claimed_jobs: list[ClaimedNotificationDispatchJob],
        *,
        now: datetime,
        cycle_started: float,
    ) -> NotificationWorkerCycleResult:
        notification_ids = [job.job.notification_id for job in claimed_jobs]
        ack_by_notification_id = {
            job.job.notification_id: job.ack_id for job in claimed_jobs
        }
        claimed = self._repository.claim_notification_events_by_ids(
            notification_ids=notification_ids,
            now=_to_iso(now),
            lease_owner=self._lease_owner,
            lease_until=_to_iso(now + timedelta(seconds=self._lease_seconds)),
        )
        claimed_ids = {event.notification_id for event in claimed}
        stale_ack_ids = [
            ack_by_notification_id[notification_id]
            for notification_id in notification_ids
            if notification_id not in claimed_ids
            and notification_id in ack_by_notification_id
        ]
        result = self._deliver_claimed(
            claimed,
            cycle_started=cycle_started,
            source="stream",
        )
        processed_ack_ids = [
            ack_by_notification_id[event.notification_id]
            for event in claimed
            if event.notification_id in ack_by_notification_id
        ]
        ack_ids = list(dict.fromkeys([*processed_ack_ids, *stale_ack_ids]))
        acked_count = 0
        if ack_ids and self._notification_queue is not None:
            acked_count = self._notification_queue.ack_many(ack_ids)
        return NotificationWorkerCycleResult(
            claimed_count=result.claimed_count,
            sent_count=result.sent_count,
            retry_count=result.retry_count,
            dead_count=result.dead_count,
            failure_count=result.failure_count,
            max_attempt_depth=result.max_attempt_depth,
            duration_seconds=result.duration_seconds,
            source="stream",
            acked_count=acked_count,
            stale_count=len(stale_ack_ids),
        )

    def _process_fallback_claim(
        self,
        *,
        now: datetime,
        cycle_started: float,
    ) -> NotificationWorkerCycleResult:
        self._last_fallback_at = time.monotonic()
        claimed = self._repository.claim_due_notification_events(
            now=_to_iso(now),
            lease_owner=self._lease_owner,
            lease_until=_to_iso(now + timedelta(seconds=self._lease_seconds)),
            limit=self._claim_batch_size,
        )
        return self._deliver_claimed(
            claimed,
            cycle_started=cycle_started,
            source="fallback",
        )

    def _deliver_claimed(
        self,
        claimed: list[NotificationEvent],
        *,
        cycle_started: float,
        source: str,
    ) -> NotificationWorkerCycleResult:
        if not claimed:
            return NotificationWorkerCycleResult(
                claimed_count=0,
                sent_count=0,
                retry_count=0,
                dead_count=0,
                failure_count=0,
                max_attempt_depth=0,
                duration_seconds=max(0.0, time.perf_counter() - cycle_started),
                source=source,
            )

        sent_count = 0
        retry_count = 0
        dead_count = 0
        failure_count = 0
        max_attempt_depth = 0
        active_events: list[NotificationEvent] = []
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

            active_events.append(event)

        send_result_by_id = self._send_events(active_events)
        for event in active_events:
            send_result = send_result_by_id.get(event.notification_id) or NotificationSendResult(
                success=False,
                provider_name="internal",
                error="internal_send_error: missing_send_result",
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
            duration_seconds=max(0.0, time.perf_counter() - cycle_started),
            source=source,
        )

    def _send_events(self, events: list[NotificationEvent]) -> dict[str, NotificationSendResult]:
        if not events:
            return {}
        if self._max_inflight <= 1 or len(events) <= 1:
            return {
                event.notification_id: self._send_with_guard(event)
                for event in events
            }

        results: dict[str, NotificationSendResult] = {}
        max_workers = min(self._max_inflight, len(events))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._send_with_guard, event): event.notification_id
                for event in events
            }
            for future in as_completed(futures):
                notification_id = futures[future]
                try:
                    results[notification_id] = future.result()
                except Exception as exc:
                    # `_send_with_guard` is defensive and should not raise.
                    # Keep a fallback in case future internals fail.
                    results[notification_id] = NotificationSendResult(
                        success=False,
                        provider_name="internal",
                        error=f"internal_send_error: {exc}",
                        retryable=True,
                    )
        return results

    def _send_with_guard(self, event: NotificationEvent) -> NotificationSendResult:
        try:
            return self._sender.send(event)
        except Exception as exc:
            return NotificationSendResult(
                success=False,
                provider_name="internal",
                error=f"internal_send_error: {exc}",
                retryable=True,
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
