from __future__ import annotations

import logging
import os
import socket
import time

from app.application.tracking_scheduler_dispatcher_service import (
    SchedulerDispatchCycleResult,
    TrackingSchedulerDispatcherService,
)
from app.core.config import get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository
from app.infrastructure.redis_tracking_dispatch_queue import RedisTrackingDispatchQueue


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


def _build_dispatcher_service() -> tuple[TrackingSchedulerDispatcherService, int]:
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
    service = TrackingSchedulerDispatcherService(
        repository=repository,
        queue=queue,
        claim_batch_size=settings.scheduler_claim_batch_size,
        lease_seconds=settings.scheduler_lease_seconds,
        lease_owner=_resolve_lease_owner(),
    )
    return service, settings.scheduler_poll_seconds


def _log_cycle(logger: logging.Logger, cycle: SchedulerDispatchCycleResult) -> None:
    logger.info(
        (
            "scheduler_dispatch_cycle claimed=%s enqueued=%s released=%s failures=%s "
            "duration_ms=%.2f"
        ),
        cycle.claimed_count,
        cycle.enqueued_count,
        cycle.released_count,
        cycle.failure_count,
        cycle.duration_seconds * 1000,
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("scraper_api.scheduler.dispatcher")
    dispatcher_service, poll_seconds = _build_dispatcher_service()
    logger.info("scheduler_dispatcher_started poll_seconds=%s", poll_seconds)
    while True:
        cycle_started = time.perf_counter()
        try:
            result = dispatcher_service.run_once()
            _log_cycle(logger, result)
        except Exception:
            logger.exception("scheduler_dispatch_cycle_failed")

        elapsed = time.perf_counter() - cycle_started
        sleep_seconds = max(0.0, float(poll_seconds) - elapsed)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
