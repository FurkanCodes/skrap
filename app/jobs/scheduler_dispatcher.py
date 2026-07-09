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
from app.infrastructure.redis_tracking_due_index import RedisTrackingDueIndex


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


def _build_dispatcher_service() -> TrackingSchedulerDispatcherService:
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
    due_index = RedisTrackingDueIndex(
        redis_url=settings.scheduler_queue_redis_url,
        zset_key=settings.scheduler_due_zset_key,
    )
    return TrackingSchedulerDispatcherService(
        repository=repository,
        queue=queue,
        claim_batch_size=settings.scheduler_claim_batch_size,
        lease_seconds=settings.scheduler_lease_seconds,
        lease_owner=_resolve_lease_owner(),
        due_index=due_index,
        due_block_seconds=settings.scheduler_due_block_seconds,
        reconcile_poll_seconds=settings.scheduler_reconcile_poll_seconds,
    )


def _log_cycle(logger: logging.Logger, cycle: SchedulerDispatchCycleResult) -> None:
    should_log_info = (
        cycle.claimed_count > 0
        or cycle.enqueued_count > 0
        or cycle.released_count > 0
        or cycle.failure_count > 0
        or cycle.stale_count > 0
    )
    log_fn = logger.info if should_log_info else logger.debug
    log_fn(
        (
            "scheduler_dispatch_cycle source=%s claimed=%s enqueued=%s released=%s "
            "failures=%s stale=%s duration_ms=%.2f"
        ),
        cycle.source,
        cycle.claimed_count,
        cycle.enqueued_count,
        cycle.released_count,
        cycle.failure_count,
        cycle.stale_count,
        cycle.duration_seconds * 1000,
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("scraper_api.scheduler.dispatcher")
    dispatcher_service = _build_dispatcher_service()
    logger.info("scheduler_dispatcher_started mode=redis_due_index")
    while True:
        try:
            result = dispatcher_service.run_once()
            _log_cycle(logger, result)
        except Exception:
            logger.exception("scheduler_dispatch_cycle_failed")
            time.sleep(1)


if __name__ == "__main__":
    main()
