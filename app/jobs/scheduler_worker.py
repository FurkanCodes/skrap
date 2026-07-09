from __future__ import annotations

import logging
import os
import socket
import time

from app.application.scrape_service import ScrapeBatchService
from app.application.tracking_scheduler_service import TrackingSchedulerService
from app.application.tracking_scheduler_worker_service import (
    SchedulerWorkerCycleResult,
    TrackingSchedulerWorkerService,
)
from app.core.config import get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository
from app.infrastructure.redis_notification_dispatch_queue import RedisNotificationDispatchQueue
from app.infrastructure.redis_tracking_dispatch_queue import RedisTrackingDispatchQueue
from app.infrastructure.redis_tracking_due_index import RedisTrackingDueIndex
from app.infrastructure.scraper_gateway import ScraplingGateway


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _resolve_lease_owner() -> str:
    configured = os.getenv("SCHEDULER_LEASE_OWNER", "").strip()
    if configured:
        return configured
    host = socket.gethostname()
    return f"{host}-{os.getpid()}"


def _resolve_worker_consumer_name() -> str:
    configured = os.getenv("SCHEDULER_WORKER_CONSUMER_NAME", "").strip()
    if configured:
        return configured
    host = socket.gethostname()
    return f"{host}-{os.getpid()}"


def _build_worker_service() -> TrackingSchedulerWorkerService:
    settings = get_settings()
    repository = PostgresTrackingRepository(
        database_url=settings.tracking_database_url,
        min_pool_size=settings.tracking_db_pool_min_size,
        max_pool_size=settings.tracking_db_pool_max_size,
    )
    repository.initialize()

    queue = RedisTrackingDispatchQueue(
        redis_url=settings.scheduler_queue_redis_url,
        stream_key=settings.scheduler_queue_stream_key,
        consumer_group=settings.scheduler_queue_consumer_group,
        max_stream_length=settings.scheduler_queue_max_stream_length,
    )
    notification_queue = RedisNotificationDispatchQueue(
        redis_url=settings.scheduler_queue_redis_url,
        stream_key=settings.notifier_queue_stream_key,
        consumer_group=settings.notifier_queue_consumer_group,
        max_stream_length=settings.notifier_queue_max_stream_length,
    )
    due_index = RedisTrackingDueIndex(
        redis_url=settings.scheduler_queue_redis_url,
        zset_key=settings.scheduler_due_zset_key,
    )
    scrape_service = ScrapeBatchService(
        gateway=ScraplingGateway(),
        max_workers=settings.scheduler_max_inflight,
        use_spider_runtime=True,
    )
    scheduler_service = TrackingSchedulerService(
        repository=repository,
        scrape_runner=scrape_service,
        timeout_seconds=settings.scrape_timeout_default,
        claim_batch_size=settings.scheduler_claim_batch_size,
        max_inflight=settings.scheduler_max_inflight,
        lease_seconds=settings.scheduler_lease_seconds,
        jitter_percent=settings.scheduler_jitter_percent,
        align_checks_to_interval=settings.scheduler_align_checks_to_interval,
        lease_owner=_resolve_lease_owner(),
        fetch_mode="auto",
        alert_cooldown_minutes=settings.scheduler_alert_cooldown_minutes,
        notification_max_attempts=settings.notifier_max_attempts,
        affiliate_redirect_base_url=settings.affiliate_redirect_base_url,
        affiliate_signing_secret=settings.affiliate_signing_secret,
        affiliate_link_rules_json=settings.affiliate_link_rules_json,
        affiliate_link_template=settings.affiliate_link_template,
        notification_queue=notification_queue,
        due_index=due_index,
    )
    return TrackingSchedulerWorkerService.from_scheduler_service(
        repository=repository,
        queue=queue,
        scheduler_service=scheduler_service,
        consumer_name=_resolve_worker_consumer_name(),
        claim_batch_size=settings.scheduler_worker_claim_batch_size,
        block_seconds=settings.scheduler_worker_block_seconds,
        pending_idle_seconds=settings.scheduler_worker_pending_idle_seconds,
    )


def _log_cycle(logger: logging.Logger, cycle: SchedulerWorkerCycleResult) -> None:
    logger.info(
        (
            "scheduler_worker_cycle claimed=%s acked=%s processed=%s stale=%s failures=%s "
            "duration_ms=%.2f"
        ),
        cycle.claimed_count,
        cycle.acked_count,
        cycle.processed_count,
        cycle.stale_count,
        cycle.failure_count,
        cycle.duration_seconds * 1000,
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("scraper_api.scheduler.worker")
    worker_service = _build_worker_service()
    logger.info("scheduler_worker_started")
    while True:
        try:
            result = worker_service.run_once()
            _log_cycle(logger, result)
        except Exception:
            logger.exception("scheduler_worker_cycle_failed")
            time.sleep(1)


if __name__ == "__main__":
    main()
