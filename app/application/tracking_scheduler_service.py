from __future__ import annotations

import random
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from typing import Protocol
from urllib.parse import quote_plus

from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchOutput
from app.scraper import PriceResult
from app.tracking import AlertEvent, NotificationEvent, PriceSnapshot, TrackedItem, TrackingRepository


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
    skipped_count: int
    success_count: int
    failure_count: int
    duration_seconds: float
    fetch_mode: str = "auto"
    due_lag_seconds: tuple[float, ...] = ()
    due_lag_seconds_p50: float | None = None
    due_lag_seconds_p95: float | None = None
    due_lag_seconds_p99: float | None = None
    site_success_counts: dict[str, int] = field(default_factory=dict)
    site_failure_counts: dict[str, int] = field(default_factory=dict)
    failure_class_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _ItemRunContext:
    """In-memory context for one claimed item and its run audit row."""

    item: TrackedItem
    run_id: str
    scheduled_for: str
    job_key: str
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
        alert_cooldown_minutes: int = 60,
        notification_max_attempts: int = 5,
        affiliate_link_template: str = "",
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
        self._alert_cooldown_minutes = max(0, alert_cooldown_minutes)
        self._notification_max_attempts = max(1, notification_max_attempts)
        self._affiliate_link_template = affiliate_link_template.strip()

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
                skipped_count=0,
                success_count=0,
                failure_count=0,
                duration_seconds=duration,
                fetch_mode=self._fetch_mode,
            )

        due_lag_seconds = tuple(_compute_due_lag_seconds(now, item.next_check_at) for item in claimed)
        contexts, pre_scrape_failures, skipped_count = self._start_runs(claimed)
        site_success_counts: defaultdict[str, int] = defaultdict(int)
        site_failure_counts: defaultdict[str, int] = defaultdict(int)
        failure_class_counts: defaultdict[str, int] = defaultdict(int)
        if pre_scrape_failures > 0:
            failure_class_counts["start_run_error"] += pre_scrape_failures
        if skipped_count > 0:
            failure_class_counts["duplicate_job"] += skipped_count

        success_count = 0
        failure_count = pre_scrape_failures
        for chunk in _chunked(contexts, self._max_inflight):
            batch_results = self._scrape_chunk(chunk)
            for context in chunk:
                site_key = _normalize_site_key(context.item.site_key)
                scrape_result = batch_results.get(
                    context.item.source_url,
                    PriceResult(url=context.item.source_url, success=False, error="no_result"),
                )
                try:
                    if self._finalize_result(context, scrape_result):
                        success_count += 1
                        site_success_counts[site_key] += 1
                    else:
                        failure_count += 1
                        site_failure_counts[site_key] += 1
                        failure_class_counts[_classify_error(scrape_result.error)] += 1
                except Exception as exc:
                    failure_count += 1
                    site_failure_counts[site_key] += 1
                    failure_class_counts["finalize_error"] += 1
                    self._mark_context_failure(context, str(exc))

        duration = max(0.0, time.perf_counter() - started)
        return SchedulerCycleResult(
            claimed_count=len(claimed),
            processed_count=success_count + failure_count,
            skipped_count=skipped_count,
            success_count=success_count,
            failure_count=failure_count,
            duration_seconds=duration,
            fetch_mode=self._fetch_mode,
            due_lag_seconds=due_lag_seconds,
            due_lag_seconds_p50=_percentile(due_lag_seconds, 50),
            due_lag_seconds_p95=_percentile(due_lag_seconds, 95),
            due_lag_seconds_p99=_percentile(due_lag_seconds, 99),
            site_success_counts=dict(site_success_counts),
            site_failure_counts=dict(site_failure_counts),
            failure_class_counts=dict(failure_class_counts),
        )

    def _start_runs(self, items: list[TrackedItem]) -> tuple[list[_ItemRunContext], int, int]:
        contexts: list[_ItemRunContext] = []
        failures = 0
        skipped = 0
        for item in items:
            run_id = _new_run_id()
            started_at = _to_iso(_utcnow())
            scheduled_for = item.next_check_at or started_at
            job_key = _build_job_key(item.item_id, scheduled_for)
            try:
                run = self._repository.start_check_run(
                    run_id=run_id,
                    item_id=item.item_id,
                    scheduled_for=scheduled_for,
                    job_key=job_key,
                    started_at=started_at,
                )
                if run.run_id != run_id:
                    if run.status == "running" and run.finished_at is None:
                        # A previous worker claimed this schedule slot and never
                        # closed it. Release the stale run so the current worker
                        # can take over instead of looping forever on duplicate_job.
                        self._repository.abandon_check_run(
                            run_id=run.run_id,
                            finished_at=started_at,
                            error="stale_run_reclaimed",
                        )
                        retry_run = self._repository.start_check_run(
                            run_id=run_id,
                            item_id=item.item_id,
                            scheduled_for=scheduled_for,
                            job_key=job_key,
                            started_at=started_at,
                        )
                        if retry_run.run_id == run_id:
                            contexts.append(
                                _ItemRunContext(
                                    item=item,
                                    run_id=run_id,
                                    scheduled_for=scheduled_for,
                                    job_key=job_key,
                                    started_at=started_at,
                                )
                            )
                            continue
                    skipped += 1
                    self._release_duplicate_job(item=item, started_at=started_at)
                    continue
                contexts.append(
                    _ItemRunContext(
                        item=item,
                        run_id=run_id,
                        scheduled_for=scheduled_for,
                        job_key=job_key,
                        started_at=started_at,
                    )
                )
            except Exception:
                failures += 1
                self._release_item_without_run(item=item, started_at=started_at)
        return contexts, failures, skipped

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
                scheduled_for=context.scheduled_for,
            )
            self._repository.save_item_and_snapshot(updated_item, snapshot)
            product_url = _resolve_product_url(item, scrape_result)
            listing_url = _resolve_listing_url(scrape_result)
            if item.tracking_mode == "monitor":
                self._emit_monitor_notification(
                    item_before=item,
                    snapshot=snapshot,
                    current_amount=scrape_result.amount,
                    product_url=product_url,
                    listing_url=listing_url,
                )
            else:
                self._emit_alert_if_crossed(
                    item_before=item,
                    snapshot=snapshot,
                    current_amount=scrape_result.amount,
                    product_url=product_url,
                    listing_url=listing_url,
                )
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
            scheduled_for=context.scheduled_for,
        )
        self._repository.save_item_and_snapshot(updated_item, snapshot)
        self._repository.finish_check_run(
            run_id=context.run_id,
            status="failed",
            finished_at=finished_at,
            error=error,
        )
        return False

    def _emit_alert_if_crossed(
        self,
        *,
        item_before: TrackedItem,
        snapshot: PriceSnapshot,
        current_amount: str | None,
        product_url: str | None,
        listing_url: str | None,
    ) -> None:
        """Emit one idempotent alert event when price crosses below target."""
        if not item_before.alerts_enabled:
            return

        target = _parse_decimal(item_before.target_amount)
        previous = _parse_decimal(item_before.last_amount)
        current = _parse_decimal(current_amount)
        if target is None or previous is None or current is None:
            return

        crossed = previous > target and current <= target
        if not crossed:
            return

        rule_type = "price_below"
        if self._is_in_cooldown(
            item_id=item_before.item_id,
            rule_type=rule_type,
            now=snapshot.checked_at,
        ):
            return

        event = AlertEvent(
            event_id=_new_alert_event_id(),
            idempotency_key=_build_alert_idempotency_key(
                item_id=item_before.item_id,
                rule_type=rule_type,
                snapshot_id=snapshot.snapshot_id,
            ),
            item_id=item_before.item_id,
            snapshot_id=snapshot.snapshot_id,
            rule_type=rule_type,
            previous_amount=_decimal_to_str(previous),
            current_amount=_decimal_to_str(current),
            target_amount=_decimal_to_str(target),
            currency=snapshot.currency or item_before.currency,
            triggered_at=snapshot.checked_at,
        )
        created = self._repository.create_alert_event(event)
        if not created:
            return
        self._enqueue_notification_event(
            event,
            product_url=product_url,
            listing_url=listing_url,
        )

    def _emit_monitor_notification(
        self,
        *,
        item_before: TrackedItem,
        snapshot: PriceSnapshot,
        current_amount: str | None,
        product_url: str | None,
        listing_url: str | None,
    ) -> None:
        """Queue one monitor notification for each successful scheduler check."""
        now = snapshot.checked_at
        active_chat_id = self._repository.get_active_telegram_chat()
        channel = "telegram" if active_chat_id else "log"
        affiliate_url = _build_affiliate_url(
            product_url=product_url,
            template=self._affiliate_link_template,
        )
        payload = {
            "notification_type": "monitor",
            "item_id": item_before.item_id,
            "snapshot_id": snapshot.snapshot_id,
            "rule_type": "monitor",
            "current_amount": current_amount,
            "currency": snapshot.currency or item_before.currency,
            "triggered_at": snapshot.checked_at,
        }
        if product_url:
            payload["product_url"] = product_url
        if listing_url:
            payload["listing_url"] = listing_url
        if affiliate_url:
            payload["affiliate_url"] = affiliate_url
        if active_chat_id is not None:
            payload["chat_id"] = active_chat_id

        notification = NotificationEvent(
            notification_id=_new_notification_event_id(),
            idempotency_key=_build_notification_idempotency_key(
                event_key=_build_monitor_notification_event_key(
                    item_id=item_before.item_id,
                    scheduled_for=snapshot.scheduled_for,
                    checked_at=snapshot.checked_at,
                ),
                channel=channel,
            ),
            alert_event_id=None,
            item_id=item_before.item_id,
            channel=channel,
            payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            status="queued",
            attempt_count=0,
            max_attempts=self._notification_max_attempts,
            next_attempt_at=now,
            last_attempt_at=None,
            sent_at=None,
            provider_name=None,
            provider_message_id=None,
            last_error=None,
            lease_owner=None,
            lease_until=None,
            created_at=now,
            updated_at=now,
        )
        self._repository.enqueue_notification_event(notification)

    def _is_in_cooldown(self, *, item_id: str, rule_type: str, now: str) -> bool:
        if self._alert_cooldown_minutes <= 0:
            return False
        last_event = self._repository.get_last_alert_event(
            item_id=item_id,
            rule_type=rule_type,
        )
        if last_event is None:
            return False

        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        last_dt = datetime.fromisoformat(last_event.triggered_at.replace("Z", "+00:00"))
        elapsed = now_dt - last_dt
        return elapsed < timedelta(minutes=self._alert_cooldown_minutes)

    def _enqueue_notification_event(
        self,
        event: AlertEvent,
        *,
        product_url: str | None,
        listing_url: str | None,
    ) -> None:
        now = event.triggered_at
        active_chat_id = self._repository.get_active_telegram_chat()
        channel = "telegram" if active_chat_id else "log"
        affiliate_url = _build_affiliate_url(
            product_url=product_url,
            template=self._affiliate_link_template,
        )
        payload = {
            "notification_type": "alert",
            "alert_event_id": event.event_id,
            "item_id": event.item_id,
            "snapshot_id": event.snapshot_id,
            "rule_type": event.rule_type,
            "previous_amount": event.previous_amount,
            "current_amount": event.current_amount,
            "target_amount": event.target_amount,
            "currency": event.currency,
            "triggered_at": event.triggered_at,
        }
        if product_url:
            payload["product_url"] = product_url
        if listing_url:
            payload["listing_url"] = listing_url
        if affiliate_url:
            payload["affiliate_url"] = affiliate_url
        if active_chat_id is not None:
            payload["chat_id"] = active_chat_id
        notification = NotificationEvent(
            notification_id=_new_notification_event_id(),
            idempotency_key=_build_notification_idempotency_key(
                event_key=event.event_id,
                channel=channel,
            ),
            alert_event_id=event.event_id,
            item_id=event.item_id,
            channel=channel,
            payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            status="queued",
            attempt_count=0,
            max_attempts=self._notification_max_attempts,
            next_attempt_at=now,
            last_attempt_at=None,
            sent_at=None,
            provider_name=None,
            provider_message_id=None,
            last_error=None,
            lease_owner=None,
            lease_until=None,
            created_at=now,
            updated_at=now,
        )
        self._repository.enqueue_notification_event(notification)

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

    def _release_lease_only(
        self,
        *,
        item: TrackedItem,
        started_at: str,
    ) -> None:
        now = _to_iso(_utcnow())
        try:
            self._repository.save_item(
                replace(
                    item,
                    lease_owner=None,
                    lease_until=None,
                    last_check_started_at=started_at,
                    last_check_finished_at=now,
                    updated_at=now,
                )
            )
        except Exception:
            pass

    def _release_duplicate_job(
        self,
        *,
        item: TrackedItem,
        started_at: str,
    ) -> None:
        now = _to_iso(_utcnow())
        next_check_at = compute_next_check_at(
            base_time=now,
            interval_minutes=item.check_interval_minutes,
            jitter_percent=self._jitter_percent,
        )
        try:
            # Keep `last_checked_at` intact here because no fresh scrape actually
            # ran. We only want to unstick the schedule from a duplicate job key.
            self._repository.save_item(
                replace(
                    item,
                    lease_owner=None,
                    lease_until=None,
                    last_check_started_at=started_at,
                    last_check_finished_at=now,
                    updated_at=now,
                    next_check_at=next_check_at,
                )
            )
        except Exception:
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


def _new_alert_event_id() -> str:
    return f"aev_{uuid.uuid4().hex[:20]}"


def _new_notification_event_id() -> str:
    return f"nev_{uuid.uuid4().hex[:20]}"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _chunked(values: list[_ItemRunContext], size: int) -> list[list[_ItemRunContext]]:
    if size <= 0:
        return [values]
    return [values[index : index + size] for index in range(0, len(values), size)]


def _normalize_site_key(site_key: str | None) -> str:
    normalized = (site_key or "").strip().lower()
    return normalized or "unknown"


def _compute_due_lag_seconds(now: datetime, next_check_at: str | None) -> float:
    if not next_check_at:
        return 0.0
    scheduled = datetime.fromisoformat(next_check_at.replace("Z", "+00:00"))
    return max(0.0, (now - scheduled).total_seconds())


def _percentile(values: tuple[float, ...], percentile: int) -> float | None:
    if not values:
        return None
    if percentile <= 0:
        return min(values)
    if percentile >= 100:
        return max(values)
    ordered = sorted(values)
    index = int(round((percentile / 100.0) * (len(ordered) - 1)))
    index = max(0, min(index, len(ordered) - 1))
    return float(ordered[index])


def _classify_error(error: str | None) -> str:
    if not error:
        return "unknown"
    normalized = error.strip().lower()
    if "protection" in normalized or "blocked" in normalized or "cloudflare" in normalized:
        return "blocked"
    if "timeout" in normalized:
        return "timeout"
    if "price_not_found" in normalized or "no_result" in normalized:
        return "price_not_found"
    if "network" in normalized or "connection" in normalized:
        return "network"
    if normalized.startswith("internal_error"):
        return "internal_error"
    return "other"


def _build_job_key(item_id: str, scheduled_for: str) -> str:
    return f"{item_id}:{scheduled_for}"


def _build_alert_idempotency_key(*, item_id: str, rule_type: str, snapshot_id: str) -> str:
    return f"{item_id}:{rule_type}:{snapshot_id}"


def _build_notification_idempotency_key(*, event_key: str, channel: str) -> str:
    return f"{event_key}:{channel}"

def _build_monitor_notification_event_key(
    *,
    item_id: str,
    scheduled_for: str | None,
    checked_at: str,
) -> str:
    marker = scheduled_for or checked_at
    return f"monitor:{item_id}:{marker}"


def _resolve_product_url(item: TrackedItem, scrape_result: PriceResult) -> str | None:
    candidate = (
        (scrape_result.canonical_url or "").strip()
        or (scrape_result.url or "").strip()
        or (item.canonical_url or "").strip()
        or (item.source_url or "").strip()
    )
    return candidate or None


def _resolve_listing_url(scrape_result: PriceResult) -> str | None:
    if not scrape_result.listings:
        return None
    listing_url = (scrape_result.listings[0].listing_url or "").strip()
    return listing_url or None


def _build_affiliate_url(*, product_url: str | None, template: str) -> str | None:
    url = (product_url or "").strip()
    if not url:
        return None

    normalized_template = template.strip()
    if not normalized_template:
        # No template configured: fall back to direct product URL.
        return url

    encoded = quote_plus(url)
    if "{url}" in normalized_template:
        return normalized_template.replace("{url}", encoded)

    separator = "&" if "?" in normalized_template else "?"
    return f"{normalized_template}{separator}url={encoded}"


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _decimal_to_str(value: Decimal) -> str:
    # Keep a stable non-scientific string representation for storage and APIs.
    return format(value, "f")
