from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchOutput
from app.scraper import PriceResult
from app.tracking import PriceSnapshot, TrackedItem, TrackingRepository


class TrackingSchedulerError(RuntimeError):
    """Base error for scheduler runtime failures."""


class ScrapeRunner(Protocol):
    """Narrow scrape-service contract used by scheduler worker."""

    def scrape(self, payload: ScrapeBatchInput) -> ScrapeBatchOutput:
        ...


@dataclass(frozen=True)
class SchedulerCycleResult:
    claimed_count: int
    processed_count: int
    success_count: int
    failure_count: int
    duration_seconds: float


@dataclass(frozen=True)
class _ItemRunContext:
    """In-memory context for one claimed item and its run audit row."""

    item: TrackedItem
    run_id: str
    started_at: str


class TrackingSchedulerService:
    """Claims due tracking checks and persists check outcomes.

    Execution model:
    - Claim due rows with DB leases (at-least-once semantics).
    - Create one `tracking_check_runs` row per claimed item.
    - Scrape in bounded batches (chunked by `max_inflight`).
    - Persist item + snapshot atomically and close the run row.
    """

    def __init__(
        self,
        *,
        repository: TrackingRepository,
        scrape_runner: ScrapeRunner,
        timeout_seconds: int,
        claim_batch_size: int,
        max_inflight: int,
        lease_seconds: int,
        jitter_percent: int,
        lease_owner: str,
        fetch_mode: str = "auto",
    ) -> None:
        self._repository = repository
        self._scrape_runner = scrape_runner
        self._timeout_seconds = timeout_seconds
        self._claim_batch_size = max(1, claim_batch_size)
        self._max_inflight = max(1, max_inflight)
        self._lease_seconds = max(1, lease_seconds)
        self._jitter_percent = max(0, jitter_percent)
        self._lease_owner = lease_owner
        self._fetch_mode = fetch_mode

    def run_once(self) -> SchedulerCycleResult:
        started = time.perf_counter()
        now = _utcnow()
        claimed = self._repository.claim_due_items(
            now=_to_iso(now),
            lease_owner=self._lease_owner,
            lease_until=_to_iso(now + timedelta(seconds=self._lease_seconds)),
            limit=self._claim_batch_size,
        )
        if not claimed:
            duration = max(0.0, time.perf_counter() - started)
            return SchedulerCycleResult(
                claimed_count=0,
                processed_count=0,
                success_count=0,
                failure_count=0,
                duration_seconds=duration,
            )

        contexts, pre_scrape_failures = self._start_runs(claimed)

        success_count = 0
        failure_count = pre_scrape_failures
        for chunk in _chunked(contexts, self._max_inflight):
            batch_results = self._scrape_chunk(chunk)
            for context in chunk:
                scrape_result = batch_results.get(
                    context.item.source_url,
                    PriceResult(url=context.item.source_url, success=False, error="no_result"),
                )
                try:
                    if self._finalize_result(context, scrape_result):
                        success_count += 1
                    else:
                        failure_count += 1
                except Exception as exc:
                    failure_count += 1
                    self._mark_context_failure(context, str(exc))

        duration = max(0.0, time.perf_counter() - started)
        return SchedulerCycleResult(
            claimed_count=len(claimed),
            processed_count=success_count + failure_count,
            success_count=success_count,
            failure_count=failure_count,
            duration_seconds=duration,
        )

    def _start_runs(self, items: list[TrackedItem]) -> tuple[list[_ItemRunContext], int]:
        contexts: list[_ItemRunContext] = []
        failures = 0
        for item in items:
            run_id = _new_run_id()
            started_at = _to_iso(_utcnow())
            scheduled_for = item.next_check_at or started_at
            try:
                self._repository.start_check_run(
                    run_id=run_id,
                    item_id=item.item_id,
                    scheduled_for=scheduled_for,
                    started_at=started_at,
                )
                contexts.append(
                    _ItemRunContext(
                        item=item,
                        run_id=run_id,
                        started_at=started_at,
                    )
                )
            except Exception:
                failures += 1
                self._release_item_without_run(item=item, started_at=started_at)
        return contexts, failures

    def _scrape_chunk(self, chunk: list[_ItemRunContext]) -> dict[str, PriceResult]:
        results_by_url: dict[str, PriceResult] = {}
        grouped: dict[str | None, list[_ItemRunContext]] = {}
        for context in chunk:
            grouped.setdefault(context.item.site_key, []).append(context)

        for site_key, group in grouped.items():
            urls = [context.item.source_url for context in group]
            selected_sites = {site_key} if site_key else None
            try:
                output = self._scrape_runner.scrape(
                    ScrapeBatchInput(
                        request_id=_new_run_id(),
                        urls=urls,
                        timeout_seconds=self._timeout_seconds,
                        selected_sites=selected_sites,
                        fetch_mode=self._fetch_mode,
                    )
                )
                for result in output.results:
                    results_by_url[result.url] = result
            except Exception as exc:
                error = f"internal_error: {exc}"
                for url in urls:
                    results_by_url[url] = PriceResult(url=url, success=False, error=error)
        return results_by_url

    def _finalize_result(self, context: _ItemRunContext, scrape_result: PriceResult) -> bool:
        item = context.item
        finished_at = _to_iso(_utcnow())
        next_check_at = compute_next_check_at(
            base_time=finished_at,
            interval_minutes=item.check_interval_minutes,
            jitter_percent=self._jitter_percent,
        )

        if scrape_result.success:
            updated_item = replace(
                item,
                canonical_url=scrape_result.canonical_url or item.canonical_url,
                product_id=scrape_result.product_id or item.product_id,
                site_key=scrape_result.site_key or item.site_key,
                adapter_key=scrape_result.adapter_key or item.adapter_key,
                product_name=scrape_result.product_name or item.product_name,
                brand=scrape_result.brand or item.brand,
                model=scrape_result.model or item.model,
                sku=scrape_result.sku or item.sku,
                gtin=scrape_result.gtin or item.gtin,
                image_url=scrape_result.image_url or item.image_url,
                availability=scrape_result.availability or item.availability,
                currency=scrape_result.currency or item.currency,
                last_amount=scrape_result.amount,
                last_raw_text=scrape_result.raw_text,
                last_source=scrape_result.source,
                last_checked_at=finished_at,
                updated_at=finished_at,
                next_check_at=next_check_at,
                lease_owner=None,
                lease_until=None,
                last_check_started_at=context.started_at,
                last_check_finished_at=finished_at,
            )
            snapshot = PriceSnapshot(
                snapshot_id=_new_snapshot_id(),
                item_id=item.item_id,
                checked_at=finished_at,
                success=True,
                amount=scrape_result.amount,
                currency=scrape_result.currency or item.currency,
                raw_text=scrape_result.raw_text,
                source=scrape_result.source,
                error=None,
            )
            self._repository.save_item_and_snapshot(updated_item, snapshot)
            self._repository.finish_check_run(
                run_id=context.run_id,
                status="succeeded",
                finished_at=finished_at,
                error=None,
            )
            return True

        error = scrape_result.error or "price_not_found"
        updated_item = replace(
            item,
            last_checked_at=finished_at,
            updated_at=finished_at,
            next_check_at=next_check_at,
            lease_owner=None,
            lease_until=None,
            last_check_started_at=context.started_at,
            last_check_finished_at=finished_at,
        )
        snapshot = PriceSnapshot(
            snapshot_id=_new_snapshot_id(),
            item_id=item.item_id,
            checked_at=finished_at,
            success=False,
            amount=scrape_result.amount,
            currency=scrape_result.currency or item.currency,
            raw_text=scrape_result.raw_text,
            source=scrape_result.source,
            error=error,
        )
        self._repository.save_item_and_snapshot(updated_item, snapshot)
        self._repository.finish_check_run(
            run_id=context.run_id,
            status="failed",
            finished_at=finished_at,
            error=error,
        )
        return False

    def _mark_context_failure(self, context: _ItemRunContext, error: str) -> None:
        finished_at = _to_iso(_utcnow())
        self._release_item_without_run(
            item=context.item,
            started_at=context.started_at,
            finished_at=finished_at,
        )
        try:
            self._repository.finish_check_run(
                run_id=context.run_id,
                status="failed",
                finished_at=finished_at,
                error=error,
            )
        except Exception:
            pass

    def _release_item_without_run(
        self,
        *,
        item: TrackedItem,
        started_at: str,
        finished_at: str | None = None,
    ) -> None:
        final_time = finished_at or _to_iso(_utcnow())
        next_check_at = compute_next_check_at(
            base_time=final_time,
            interval_minutes=item.check_interval_minutes,
            jitter_percent=self._jitter_percent,
        )
        try:
            self._repository.save_item(
                replace(
                    item,
                    last_checked_at=final_time,
                    updated_at=final_time,
                    next_check_at=next_check_at,
                    lease_owner=None,
                    lease_until=None,
                    last_check_started_at=started_at,
                    last_check_finished_at=final_time,
                )
            )
        except Exception:
            # Lease expiry still protects against permanent lock if this write fails.
            pass


def compute_next_check_at(
    *,
    base_time: str,
    interval_minutes: int,
    jitter_percent: int,
    minimum_delay_seconds: int = 60,
) -> str:
    """Compute next check time with bounded jitter to flatten bursty schedules."""
    base_dt = datetime.fromisoformat(base_time.replace("Z", "+00:00"))
    interval_seconds = max(60, interval_minutes * 60)
    jitter_ratio = max(0.0, float(jitter_percent) / 100.0)
    max_jitter_seconds = interval_seconds * jitter_ratio
    jitter_seconds = random.uniform(-max_jitter_seconds, max_jitter_seconds)
    delay_seconds = max(float(minimum_delay_seconds), interval_seconds + jitter_seconds)
    next_dt = base_dt + timedelta(seconds=delay_seconds)
    return _to_iso(next_dt)


def _new_snapshot_id() -> str:
    return f"snp_{uuid.uuid4().hex[:20]}"


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:20]}"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _chunked(values: list[_ItemRunContext], size: int) -> list[list[_ItemRunContext]]:
    if size <= 0:
        return [values]
    return [values[index : index + size] for index in range(0, len(values), size)]
