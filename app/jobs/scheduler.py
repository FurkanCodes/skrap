from __future__ import annotations

import logging
import os
import socket
import time

from app.application.scrape_service import ScrapeBatchService
from app.application.tracking_scheduler_service import TrackingSchedulerService
from app.core.config import get_settings
from app.infrastructure.scraper_gateway import ScraplingGateway
from app.infrastructure.sqlite_tracking_repository import SQLiteTrackingRepository


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _resolve_lease_owner() -> str:
    configured = os.getenv("SCHEDULER_LEASE_OWNER", "").strip()
    if configured:
        return configured
    host = socket.gethostname()
    return f"{host}-{os.getpid()}"


def _build_scheduler_service() -> tuple[TrackingSchedulerService, int]:
    settings = get_settings()
    repository = SQLiteTrackingRepository(db_path=settings.tracking_db_path)
    repository.initialize()

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
        lease_owner=_resolve_lease_owner(),
        fetch_mode="auto",
    )
    return scheduler_service, settings.scheduler_poll_seconds


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("scraper_api.scheduler")
    scheduler_service, poll_seconds = _build_scheduler_service()

    logger.info(
        "scheduler_started poll_seconds=%s",
        poll_seconds,
    )
    while True:
        cycle_started = time.perf_counter()
        try:
            result = scheduler_service.run_once()
            logger.info(
                "scheduler_cycle claimed=%s processed=%s success=%s failure=%s duration_ms=%.2f",
                result.claimed_count,
                result.processed_count,
                result.success_count,
                result.failure_count,
                result.duration_seconds * 1000,
            )
        except Exception:
            logger.exception("scheduler_cycle_failed")

        elapsed = time.perf_counter() - cycle_started
        sleep_seconds = max(0.0, float(poll_seconds) - elapsed)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
