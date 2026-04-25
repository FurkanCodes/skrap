from __future__ import annotations

import logging
import os
import socket
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.application.scrape_service import ScrapeBatchService
from app.application.tracking_scheduler_service import SchedulerCycleResult, TrackingSchedulerService
from app.core.config import get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository
from app.infrastructure.scraper_gateway import ScraplingGateway


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class SchedulerMetricsSnapshot:
    """Aggregated scheduler metrics over a rolling time window."""

    window_seconds: int
    sample_count: int
    due_lag_p50: float | None
    due_lag_p95: float | None
    due_lag_p99: float | None
    total_claimed: int
    total_processed: int
    total_skipped: int
    total_success: int
    total_failure: int
    site_success_counts: dict[str, int]
    site_failure_counts: dict[str, int]
    failure_class_counts: dict[str, int]


class SchedulerMetricsWindow:
    """Keeps one-hour rolling metrics in-memory for structured logging."""

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window_seconds = max(60, window_seconds)
        self._samples: deque[tuple[datetime, SchedulerCycleResult]] = deque()

    def add_cycle(self, result: SchedulerCycleResult) -> SchedulerMetricsSnapshot:
        now = datetime.now(tz=UTC)
        self._samples.append((now, result))
        self._trim(now)
        return self.snapshot()

    def snapshot(self) -> SchedulerMetricsSnapshot:
        due_lags: list[float] = []
        total_claimed = 0
        total_processed = 0
        total_skipped = 0
        total_success = 0
        total_failure = 0
        site_success_counts: Counter[str] = Counter()
        site_failure_counts: Counter[str] = Counter()
        failure_class_counts: Counter[str] = Counter()

        for _, result in self._samples:
            due_lags.extend(result.due_lag_seconds)
            total_claimed += result.claimed_count
            total_processed += result.processed_count
            total_skipped += result.skipped_count
            total_success += result.success_count
            total_failure += result.failure_count
            site_success_counts.update(result.site_success_counts)
            site_failure_counts.update(result.site_failure_counts)
            failure_class_counts.update(result.failure_class_counts)

        return SchedulerMetricsSnapshot(
            window_seconds=self._window_seconds,
            sample_count=len(self._samples),
            due_lag_p50=_percentile(due_lags, 50),
            due_lag_p95=_percentile(due_lags, 95),
            due_lag_p99=_percentile(due_lags, 99),
            total_claimed=total_claimed,
            total_processed=total_processed,
            total_skipped=total_skipped,
            total_success=total_success,
            total_failure=total_failure,
            site_success_counts=dict(site_success_counts),
            site_failure_counts=dict(site_failure_counts),
            failure_class_counts=dict(failure_class_counts),
        )

    def _trim(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._window_seconds)
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()


def _resolve_lease_owner() -> str:
    configured = os.getenv("SCHEDULER_LEASE_OWNER", "").strip()
    if configured:
        return configured
    host = socket.gethostname()
    return f"{host}-{os.getpid()}"


def _build_scheduler_service() -> tuple[TrackingSchedulerService, int]:
    settings = get_settings()
    repository = PostgresTrackingRepository(
        database_url=settings.tracking_database_url,
        min_pool_size=settings.tracking_db_pool_min_size,
        max_pool_size=settings.tracking_db_pool_max_size,
    )
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
        align_checks_to_interval=settings.scheduler_align_checks_to_interval,
        lease_owner=_resolve_lease_owner(),
        fetch_mode="auto",
        alert_cooldown_minutes=settings.scheduler_alert_cooldown_minutes,
        notification_max_attempts=settings.notifier_max_attempts,
        affiliate_link_template=settings.affiliate_link_template,
    )
    return scheduler_service, settings.scheduler_poll_seconds


def main() -> None:
    _configure_logging()
    logger = logging.getLogger("scraper_api.scheduler")
    scheduler_service, poll_seconds = _build_scheduler_service()
    metrics_window = SchedulerMetricsWindow(window_seconds=3600)

    logger.info(
        "scheduler_started poll_seconds=%s",
        poll_seconds,
    )
    while True:
        cycle_started = time.perf_counter()
        try:
            result = scheduler_service.run_once()
            window_snapshot = metrics_window.add_cycle(result)
            logger.info(
                (
                    "scheduler_cycle fetch_mode=%s claimed=%s processed=%s skipped=%s success=%s failure=%s duration_ms=%.2f "
                    "cycle_due_lag_p50=%s cycle_due_lag_p95=%s cycle_due_lag_p99=%s "
                    "window_seconds=%s window_samples=%s window_due_lag_p95=%s "
                    "window_total_success=%s window_total_failure=%s window_total_skipped=%s "
                    "site_success=%s site_failure=%s failure_classes=%s"
                ),
                result.fetch_mode,
                result.claimed_count,
                result.processed_count,
                result.skipped_count,
                result.success_count,
                result.failure_count,
                result.duration_seconds * 1000,
                _format_float(result.due_lag_seconds_p50),
                _format_float(result.due_lag_seconds_p95),
                _format_float(result.due_lag_seconds_p99),
                window_snapshot.window_seconds,
                window_snapshot.sample_count,
                _format_float(window_snapshot.due_lag_p95),
                window_snapshot.total_success,
                window_snapshot.total_failure,
                window_snapshot.total_skipped,
                window_snapshot.site_success_counts,
                window_snapshot.site_failure_counts,
                window_snapshot.failure_class_counts,
            )
        except Exception:
            logger.exception("scheduler_cycle_failed")

        elapsed = time.perf_counter() - cycle_started
        sleep_seconds = max(0.0, float(poll_seconds) - elapsed)
        time.sleep(sleep_seconds)


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    if percentile <= 0:
        return float(min(values))
    if percentile >= 100:
        return float(max(values))
    ordered = sorted(values)
    index = int(round((percentile / 100.0) * (len(ordered) - 1)))
    index = max(0, min(index, len(ordered) - 1))
    return float(ordered[index])


def _format_float(value: float | None) -> str:
    if value is None:
        return "na"
    return f"{value:.2f}"


if __name__ == "__main__":
    main()
