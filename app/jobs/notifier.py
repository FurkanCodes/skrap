from __future__ import annotations

import json
import logging
import os
import socket
import time

from app.application.notification_worker_service import (
    NotificationSendResult,
    NotificationWorkerService,
)
from app.core.config import get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository
from app.tracking import NotificationEvent


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class LoggingNotificationSender:
    """MVP sender that logs payloads and acknowledges immediately."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("scraper_api.notifier.sender")

    def send(self, event: NotificationEvent) -> NotificationSendResult:
        payload: object
        try:
            payload = json.loads(event.payload)
        except json.JSONDecodeError:
            payload = {"raw_payload": event.payload}

        self._logger.info(
            "notification_send channel=%s notification_id=%s alert_event_id=%s payload=%s",
            event.channel,
            event.notification_id,
            event.alert_event_id,
            payload,
        )
        return NotificationSendResult(
            success=True,
            provider_name="log",
            provider_message_id=event.notification_id,
            error=None,
            retryable=False,
        )


def _resolve_lease_owner() -> str:
    configured = os.getenv("NOTIFIER_LEASE_OWNER", "").strip()
    if configured:
        return configured
    host = socket.gethostname()
    return f"{host}-{os.getpid()}"


def _build_worker_service() -> tuple[NotificationWorkerService, int]:
    settings = get_settings()
    repository = PostgresTrackingRepository(
        database_url=settings.tracking_database_url,
        min_pool_size=settings.tracking_db_pool_min_size,
        max_pool_size=settings.tracking_db_pool_max_size,
    )
    repository.initialize()
    worker_service = NotificationWorkerService(
        repository=repository,
        sender=LoggingNotificationSender(),
        claim_batch_size=settings.notifier_claim_batch_size,
        lease_seconds=settings.notifier_lease_seconds,
        lease_owner=_resolve_lease_owner(),
        max_attempts=settings.notifier_max_attempts,
        base_backoff_seconds=settings.notifier_base_backoff_seconds,
        backoff_max_seconds=settings.notifier_backoff_max_seconds,
        retry_jitter_percent=settings.notifier_retry_jitter_percent,
    )
    return worker_service, settings.notifier_poll_seconds


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("scraper_api.notifier")
    worker_service, poll_seconds = _build_worker_service()

    logger.info("notifier_started poll_seconds=%s", poll_seconds)
    while True:
        started = time.perf_counter()
        try:
            result = worker_service.run_once()
            logger.info(
                (
                    "notifier_cycle claimed=%s sent=%s retried=%s dead=%s failures=%s max_attempt_depth=%s duration_ms=%.2f"
                ),
                result.claimed_count,
                result.sent_count,
                result.retry_count,
                result.dead_count,
                result.failure_count,
                result.max_attempt_depth,
                result.duration_seconds * 1000,
            )
        except Exception:
            logger.exception("notifier_cycle_failed")

        elapsed = time.perf_counter() - started
        sleep_seconds = max(0.0, float(poll_seconds) - elapsed)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
