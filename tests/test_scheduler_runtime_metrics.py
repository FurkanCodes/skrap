from __future__ import annotations

from app.application.tracking_scheduler_service import SchedulerCycleResult
from app.jobs.scheduler import SchedulerMetricsWindow


def test_scheduler_metrics_window_aggregates_counts_and_due_lag() -> None:
    window = SchedulerMetricsWindow(window_seconds=3600)

    first = SchedulerCycleResult(
        claimed_count=2,
        processed_count=2,
        skipped_count=1,
        success_count=1,
        failure_count=1,
        duration_seconds=0.2,
        fetch_mode="auto",
        due_lag_seconds=(10.0, 20.0),
        site_success_counts={"generic": 1},
        site_failure_counts={"akakce": 1},
        failure_class_counts={"price_not_found": 1},
    )
    second = SchedulerCycleResult(
        claimed_count=1,
        processed_count=1,
        skipped_count=0,
        success_count=1,
        failure_count=0,
        duration_seconds=0.1,
        fetch_mode="auto",
        due_lag_seconds=(30.0,),
        site_success_counts={"generic": 1},
        site_failure_counts={},
        failure_class_counts={},
    )

    window.add_cycle(first)
    snapshot = window.add_cycle(second)

    assert snapshot.sample_count == 2
    assert snapshot.total_claimed == 3
    assert snapshot.total_processed == 3
    assert snapshot.total_skipped == 1
    assert snapshot.total_success == 2
    assert snapshot.total_failure == 1
    assert snapshot.site_success_counts.get("generic") == 2
    assert snapshot.site_failure_counts.get("akakce") == 1
    assert snapshot.failure_class_counts.get("price_not_found") == 1
    assert snapshot.due_lag_p95 is not None
    assert snapshot.due_lag_p95 >= 20.0
