from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.application.scrape_service import ScrapeBatchOutput
from app.application.tracking_scheduler_service import (
    TrackingSchedulerService,
    compute_next_check_at,
)
from app.scraper import PriceListing, PriceResult
from app.tracking import (
    AlertEvent,
    NotificationEvent,
    PriceSnapshot,
    TrackedItem,
    TrackingBootstrapIdempotencyRecord,
    TrackingCheckRun,
    TrackingCheckRunStatus,
    UsageCounter,
    UserEntitlement,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _usage_period_bounds(now_iso: str) -> tuple[str, str]:
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(UTC)
    period_start = datetime(year=now.year, month=now.month, day=1, tzinfo=UTC)
    if now.month == 12:
        period_end = datetime(year=now.year + 1, month=1, day=1, tzinfo=UTC)
    else:
        period_end = datetime(year=now.year, month=now.month + 1, day=1, tzinfo=UTC)
    return (
        period_start.isoformat().replace("+00:00", "Z"),
        period_end.isoformat().replace("+00:00", "Z"),
    )


def _item(
    *,
    item_id: str,
    url: str,
    now: datetime,
    status: str = "active",
    next_check_at: str | None = None,
    last_amount: str | None = "100.00",
    target_amount: str | None = None,
    notify_on_price_increase: bool = False,
    notify_on_stock_change: bool = False,
) -> TrackedItem:
    stamp = _iso(now)
    return TrackedItem(
        item_id=item_id,
        source_url=url,
        canonical_url=url,
        product_id=f"prd_{item_id}",
        site_key="generic",
        adapter_key="generic",
        product_name="Product",
        brand=None,
        model=None,
        sku=None,
        gtin=None,
        image_url=None,
        availability=None,
        currency="USD",
        last_amount=last_amount,
        last_raw_text=(f"${last_amount}" if last_amount else None),
        last_source="test:init",
        status=status,
        check_interval_minutes=60,
        target_amount=target_amount,
        notify_on_price_increase=notify_on_price_increase,
        notify_on_stock_change=notify_on_stock_change,
        created_at=stamp,
        updated_at=stamp,
        last_checked_at=stamp,
        next_check_at=next_check_at,
        lease_owner=None,
        lease_until=None,
        last_check_started_at=None,
        last_check_finished_at=stamp,
    )


@dataclass
class _FakeScrapeRunner:
    result_by_url: dict[str, PriceResult]

    def scrape(self, payload) -> ScrapeBatchOutput:
        results: list[PriceResult] = []
        for url in payload.urls:
            results.append(
                self.result_by_url.get(
                    url,
                    PriceResult(url=url, success=False, error="not_configured"),
                )
            )
        success_count = sum(1 for result in results if result.success)
        return ScrapeBatchOutput(
            request_id=payload.request_id,
            total_urls=len(payload.urls),
            success_count=success_count,
            failure_count=len(payload.urls) - success_count,
            results=results,
        )


@dataclass
class _InMemoryTrackingRepository:
    items_by_id: dict[str, TrackedItem]
    source_to_item_id: dict[str, str]
    snapshots_by_id: dict[str, PriceSnapshot]
    runs_by_id: dict[str, TrackingCheckRun]
    runs_by_job_key: dict[str, str]
    idempotency: dict[str, TrackingBootstrapIdempotencyRecord]
    alerts_by_id: dict[str, AlertEvent]
    alerts_by_idempotency_key: dict[str, str]
    notifications_by_id: dict[str, NotificationEvent]
    notifications_by_idempotency_key: dict[str, str]
    active_telegram_chat_id: str | None
    entitlements_by_user: dict[str, UserEntitlement]
    usage_counters_by_scope: dict[tuple[str, str, str], UsageCounter]

    def initialize(self) -> None:
        return None

    def list_items(self) -> list[TrackedItem]:
        return sorted(
            self.items_by_id.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def get_item(self, item_id: str) -> TrackedItem | None:
        return self.items_by_id.get(item_id)

    def get_item_by_source_url(self, source_url: str) -> TrackedItem | None:
        item_id = self.source_to_item_id.get(source_url)
        if item_id is None:
            return None
        return self.items_by_id.get(item_id)

    def save_item(self, item: TrackedItem) -> TrackedItem:
        current_item_id = self.source_to_item_id.get(item.source_url)
        if current_item_id is not None and current_item_id != item.item_id:
            self.items_by_id.pop(current_item_id, None)
        self.items_by_id[item.item_id] = item
        self.source_to_item_id[item.source_url] = item.item_id
        return item

    def save_item_and_snapshot(self, item: TrackedItem, snapshot: PriceSnapshot) -> TrackedItem:
        saved = self.save_item(item)
        self._upsert_snapshot(snapshot)
        return saved

    def delete_item(self, item_id: str) -> bool:
        item = self.items_by_id.pop(item_id, None)
        if item is None:
            return False
        self.source_to_item_id.pop(item.source_url, None)

        for snapshot_id in list(self.snapshots_by_id.keys()):
            if self.snapshots_by_id[snapshot_id].item_id == item_id:
                self.snapshots_by_id.pop(snapshot_id, None)

        for run_id in list(self.runs_by_id.keys()):
            run = self.runs_by_id[run_id]
            if run.item_id != item_id:
                continue
            self.runs_by_job_key.pop(run.job_key or "", None)
            self.runs_by_id.pop(run_id, None)

        for event_id in list(self.alerts_by_id.keys()):
            event = self.alerts_by_id[event_id]
            if event.item_id != item_id:
                continue
            self.alerts_by_idempotency_key.pop(event.idempotency_key, None)
            self.alerts_by_id.pop(event_id, None)

        for notification_id in list(self.notifications_by_id.keys()):
            notification = self.notifications_by_id[notification_id]
            if notification.item_id != item_id:
                continue
            self.notifications_by_idempotency_key.pop(notification.idempotency_key, None)
            self.notifications_by_id.pop(notification_id, None)
        return True

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._upsert_snapshot(snapshot)

    def list_snapshots(self, item_id: str, limit: int) -> list[PriceSnapshot]:
        rows = [snapshot for snapshot in self.snapshots_by_id.values() if snapshot.item_id == item_id]
        rows.sort(key=lambda snapshot: snapshot.checked_at, reverse=True)
        return rows[:limit]

    def create_alert_event(self, event: AlertEvent) -> bool:
        existing_event_id = self.alerts_by_idempotency_key.get(event.idempotency_key)
        if existing_event_id is not None:
            return False
        self.alerts_by_id[event.event_id] = event
        self.alerts_by_idempotency_key[event.idempotency_key] = event.event_id
        return True

    def get_last_alert_event(
        self,
        *,
        item_id: str,
        rule_type: str,
    ) -> AlertEvent | None:
        rows = [
            event
            for event in self.alerts_by_id.values()
            if event.item_id == item_id and event.rule_type == rule_type
        ]
        if not rows:
            return None
        rows.sort(key=lambda event: event.triggered_at, reverse=True)
        return rows[0]

    def list_alert_events(self, item_id: str, limit: int) -> list[AlertEvent]:
        rows = [event for event in self.alerts_by_id.values() if event.item_id == item_id]
        rows.sort(key=lambda event: event.triggered_at, reverse=True)
        return rows[:limit]

    def enqueue_notification_event(self, event: NotificationEvent) -> bool:
        existing_id = self.notifications_by_idempotency_key.get(event.idempotency_key)
        if existing_id is not None:
            return False
        self.notifications_by_id[event.notification_id] = event
        self.notifications_by_idempotency_key[event.idempotency_key] = event.notification_id
        return True

    def claim_due_notification_events(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[NotificationEvent]:
        if limit < 1:
            return []
        candidates = [
            event
            for event in self.notifications_by_id.values()
            if event.status in {"queued", "failed", "sending"}
            and event.status not in {"sent", "dead"}
            and event.next_attempt_at <= now
            and (event.lease_until is None or event.lease_until <= now)
        ]
        candidates.sort(key=lambda event: (event.next_attempt_at, event.notification_id))
        claimed = candidates[:limit]
        updated: list[NotificationEvent] = []
        for event in claimed:
            next_event = replace(
                event,
                status="sending",
                lease_owner=lease_owner,
                lease_until=lease_until,
                last_attempt_at=now,
                updated_at=now,
            )
            self.notifications_by_id[event.notification_id] = next_event
            updated.append(next_event)
        return updated

    def finalize_notification_event(
        self,
        *,
        notification_id: str,
        status: str,
        attempt_count: int,
        now: str,
        next_attempt_at: str | None,
        sent_at: str | None,
        provider_name: str | None,
        provider_message_id: str | None,
        error: str | None,
    ) -> None:
        current = self.notifications_by_id.get(notification_id)
        if current is None:
            return
        self.notifications_by_id[notification_id] = replace(
            current,
            status=status,
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at or current.next_attempt_at,
            sent_at=sent_at,
            provider_name=provider_name,
            provider_message_id=provider_message_id,
            last_error=error,
            lease_owner=None,
            lease_until=None,
            updated_at=now,
        )

    def list_notification_events(self, item_id: str, limit: int) -> list[NotificationEvent]:
        rows = [event for event in self.notifications_by_id.values() if event.item_id == item_id]
        rows.sort(key=lambda event: event.created_at, reverse=True)
        return rows[:limit]

    def claim_due_items(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[TrackedItem]:
        if limit < 1:
            return []

        candidates = [
            item
            for item in self.items_by_id.values()
            if item.status == "active"
            and (item.next_check_at is None or item.next_check_at <= now)
            and (item.lease_until is None or item.lease_until <= now)
        ]
        candidates.sort(key=lambda item: (item.next_check_at or item.updated_at or item.created_at, item.item_id))
        claimed = candidates[:limit]

        updated: list[TrackedItem] = []
        for item in claimed:
            next_item = replace(
                item,
                lease_owner=lease_owner,
                lease_until=lease_until,
                last_check_started_at=now,
                updated_at=now,
            )
            self.save_item(next_item)
            updated.append(next_item)
        return updated

    def start_check_run(
        self,
        *,
        run_id: str,
        item_id: str,
        scheduled_for: str,
        job_key: str,
        started_at: str,
    ) -> TrackingCheckRun:
        existing_run_id = self.runs_by_job_key.get(job_key)
        if existing_run_id is not None:
            return self.runs_by_id[existing_run_id]

        max_attempt = max(
            (
                run.attempt
                for run in self.runs_by_id.values()
                if run.item_id == item_id and run.scheduled_for == scheduled_for
            ),
            default=0,
        )
        run = TrackingCheckRun(
            run_id=run_id,
            item_id=item_id,
            scheduled_for=scheduled_for,
            started_at=started_at,
            finished_at=None,
            status="running",
            attempt=max_attempt + 1,
            error=None,
            job_key=job_key,
        )
        self.runs_by_id[run_id] = run
        self.runs_by_job_key[job_key] = run_id
        return run

    def finish_check_run(
        self,
        *,
        run_id: str,
        status: TrackingCheckRunStatus,
        finished_at: str,
        error: str | None,
    ) -> None:
        run = self.runs_by_id.get(run_id)
        if run is None:
            return
        self.runs_by_id[run_id] = replace(
            run,
            status=status,
            finished_at=finished_at,
            error=error,
        )

    def abandon_check_run(
        self,
        *,
        run_id: str,
        finished_at: str,
        error: str,
    ) -> None:
        run = self.runs_by_id.get(run_id)
        if run is None:
            return
        self.runs_by_job_key.pop(run.job_key or "", None)
        self.runs_by_id[run_id] = replace(
            run,
            status="failed",
            finished_at=finished_at,
            error=error,
            job_key=None,
        )

    def list_check_runs(self, item_id: str, limit: int) -> list[TrackingCheckRun]:
        rows = [run for run in self.runs_by_id.values() if run.item_id == item_id]
        rows.sort(key=lambda run: run.started_at, reverse=True)
        return rows[:limit]

    def get_bootstrap_idempotency_record(
        self,
        idempotency_key: str,
    ) -> TrackingBootstrapIdempotencyRecord | None:
        return self.idempotency.get(idempotency_key)

    def save_bootstrap_idempotency_record(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        existing = self.idempotency.get(idempotency_key)
        created_at = existing.created_at if existing is not None else now
        self.idempotency[idempotency_key] = TrackingBootstrapIdempotencyRecord(
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            item_id=item_id,
            created_at=created_at,
            updated_at=now,
        )

    def create_telegram_connect_session(self, session):  # pragma: no cover - not used in these tests
        return session

    def get_telegram_connect_session(self, state: str):  # pragma: no cover - not used in these tests
        return None

    def mark_telegram_connect_session_connected(self, **kwargs):  # pragma: no cover - not used in these tests
        return None

    def mark_telegram_connect_session_expired(self, *, state: str):  # pragma: no cover - not used in these tests
        return None

    def set_active_telegram_chat(self, *, chat_id: str, updated_at: str) -> None:  # noqa: ARG002
        self.active_telegram_chat_id = chat_id

    def get_active_telegram_chat(self) -> str | None:
        return self.active_telegram_chat_id

    def get_user_entitlement(self, *, user_id: str) -> UserEntitlement | None:
        return self.entitlements_by_user.get(user_id)

    def get_current_usage_counter(self, *, user_id: str, now: str) -> UsageCounter:
        period_start, period_end = _usage_period_bounds(now)
        key = (user_id, period_start, period_end)
        current = self.usage_counters_by_scope.get(key)
        if current is not None:
            return current
        created = UsageCounter(
            counter_id=f"usc_{user_id}_{period_start}",
            user_id=user_id,
            period_start=period_start,
            period_end=period_end,
            consumed_check_units=0,
            created_at=now,
            updated_at=now,
        )
        self.usage_counters_by_scope[key] = created
        return created

    def try_consume_check_units(
        self,
        *,
        user_id: str,
        units: int,
        monthly_limit: int,
        now: str,
    ) -> bool:
        if units < 1:
            return True
        usage = self.get_current_usage_counter(user_id=user_id, now=now)
        if usage.consumed_check_units + units > max(0, monthly_limit):
            return False
        key = (user_id, usage.period_start, usage.period_end)
        self.usage_counters_by_scope[key] = replace(
            usage,
            consumed_check_units=usage.consumed_check_units + units,
            updated_at=now,
        )
        return True

    def _upsert_snapshot(self, snapshot: PriceSnapshot) -> None:
        conflict_id = None
        if snapshot.scheduled_for is not None:
            for current_snapshot in self.snapshots_by_id.values():
                if (
                    current_snapshot.item_id == snapshot.item_id
                    and current_snapshot.scheduled_for == snapshot.scheduled_for
                ):
                    conflict_id = current_snapshot.snapshot_id
                    break

        if conflict_id is None:
            self.snapshots_by_id[snapshot.snapshot_id] = snapshot
            return

        existing = self.snapshots_by_id[conflict_id]
        success = existing.success or snapshot.success
        can_replace_values = snapshot.success or (not existing.success)
        self.snapshots_by_id[conflict_id] = PriceSnapshot(
            snapshot_id=existing.snapshot_id,
            item_id=existing.item_id,
            checked_at=snapshot.checked_at,
            scheduled_for=existing.scheduled_for,
            success=success,
            amount=snapshot.amount if can_replace_values else existing.amount,
            currency=snapshot.currency if can_replace_values else existing.currency,
            raw_text=snapshot.raw_text if can_replace_values else existing.raw_text,
            source=snapshot.source if can_replace_values else existing.source,
            error=(
                None
                if snapshot.success
                else (existing.error if existing.success else snapshot.error)
            ),
            availability=snapshot.availability if can_replace_values else existing.availability,
        )


def _build_repo() -> _InMemoryTrackingRepository:
    repo = _InMemoryTrackingRepository(
        items_by_id={},
        source_to_item_id={},
        snapshots_by_id={},
        runs_by_id={},
        runs_by_job_key={},
        idempotency={},
        alerts_by_id={},
        alerts_by_idempotency_key={},
        notifications_by_id={},
        notifications_by_idempotency_key={},
        active_telegram_chat_id=None,
        entitlements_by_user={
            "legacy_owner": UserEntitlement(
                user_id="legacy_owner",
                plan_key="free",
                max_active_trackers=5,
                min_check_interval_minutes=720,
                monthly_check_units=100,
                allowed_channels="telegram",
                overage_policy="block",
                created_at=_iso(datetime.now(tz=UTC)),
                updated_at=_iso(datetime.now(tz=UTC)),
            )
        },
        usage_counters_by_scope={},
    )
    return repo


def test_claim_due_items_filters_status_and_due_time() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)

    due_time = _iso(now - timedelta(minutes=5))
    future_time = _iso(now + timedelta(minutes=30))

    due_item = _item(
        item_id="trk_due",
        url="https://example.com/due",
        now=now,
        status="active",
        next_check_at=due_time,
    )
    immediate_item = _item(
        item_id="trk_immediate",
        url="https://example.com/immediate",
        now=now,
        status="active",
        next_check_at=None,
    )
    future_item = _item(
        item_id="trk_future",
        url="https://example.com/future",
        now=now,
        status="active",
        next_check_at=future_time,
    )
    paused_item = _item(
        item_id="trk_paused",
        url="https://example.com/paused",
        now=now,
        status="paused",
        next_check_at=due_time,
    )

    repo.save_item(due_item)
    repo.save_item(immediate_item)
    repo.save_item(future_item)
    repo.save_item(paused_item)

    claimed = repo.claim_due_items(
        now=_iso(now),
        lease_owner="sched-a",
        lease_until=_iso(now + timedelta(seconds=180)),
        limit=10,
    )

    claimed_ids = {item.item_id for item in claimed}
    assert claimed_ids == {"trk_due", "trk_immediate"}


def test_claim_due_items_reclaims_after_lease_expiry() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_lease",
        url="https://example.com/lease",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
    )
    repo.save_item(item)

    first_claim = repo.claim_due_items(
        now=_iso(now),
        lease_owner="sched-1",
        lease_until=_iso(now + timedelta(seconds=60)),
        limit=10,
    )
    assert [it.item_id for it in first_claim] == ["trk_lease"]

    second_claim = repo.claim_due_items(
        now=_iso(now + timedelta(seconds=30)),
        lease_owner="sched-2",
        lease_until=_iso(now + timedelta(seconds=120)),
        limit=10,
    )
    assert second_claim == []

    third_claim = repo.claim_due_items(
        now=_iso(now + timedelta(seconds=61)),
        lease_owner="sched-2",
        lease_until=_iso(now + timedelta(seconds=240)),
        limit=10,
    )
    assert [it.item_id for it in third_claim] == ["trk_lease"]


def test_compute_next_check_at_applies_bounded_jitter() -> None:
    base = _iso(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    values: list[float] = []
    for _ in range(200):
        next_at = compute_next_check_at(
            base_time=base,
            interval_minutes=60,
            jitter_percent=10,
        )
        base_dt = datetime.fromisoformat(base.replace("Z", "+00:00"))
        next_dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
        values.append((next_dt - base_dt).total_seconds())

    assert min(values) >= 3240  # -10%
    assert max(values) <= 3960  # +10%


def test_compute_next_check_at_with_small_interval_uses_minutes() -> None:
    base = _iso(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    next_at = compute_next_check_at(
        base_time=base,
        interval_minutes=15,
        jitter_percent=0,
    )
    base_dt = datetime.fromisoformat(base.replace("Z", "+00:00"))
    next_dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
    assert (next_dt - base_dt).total_seconds() == 900


def test_compute_next_check_at_with_test_interval_uses_fifteen_seconds() -> None:
    base = _iso(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    next_at = compute_next_check_at(
        base_time=base,
        interval_minutes=10_079,
        jitter_percent=0,
    )
    base_dt = datetime.fromisoformat(base.replace("Z", "+00:00"))
    next_dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
    assert (next_dt - base_dt).total_seconds() == 15


def test_compute_next_check_at_aligns_to_shared_boundary_when_enabled() -> None:
    base = _iso(datetime(2026, 1, 1, 0, 0, 1, 200000, tzinfo=UTC))
    next_at = compute_next_check_at(
        base_time=base,
        interval_minutes=10_079,
        jitter_percent=50,
        align_checks_to_interval=True,
    )
    next_dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
    assert next_dt == datetime(2026, 1, 1, 0, 0, 15, tzinfo=UTC)


def test_compute_next_check_at_aligns_to_next_slot_after_boundary() -> None:
    base = _iso(datetime(2026, 1, 1, 0, 0, 14, 900000, tzinfo=UTC))
    next_at = compute_next_check_at(
        base_time=base,
        interval_minutes=10_079,
        jitter_percent=0,
        align_checks_to_interval=True,
    )
    next_dt = datetime.fromisoformat(next_at.replace("Z", "+00:00"))
    assert next_dt == datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)


def test_scheduler_run_once_persists_success_snapshot_and_next_check() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_success",
        url="https://example.com/success",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
        last_amount="100.00",
    )
    repo.save_item(item)

    listing = PriceListing(
        item_id="itm_success",
        amount="80.00",
        currency="USD",
        raw_text="$80.00",
        source="test:success",
    )
    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        product_id=item.product_id,
        site_key="generic",
        adapter_key="generic",
        amount=listing.amount,
        currency=listing.currency,
        raw_text=listing.raw_text,
        source=listing.source,
        product_name="Updated",
        listing_count=1,
        listings=[listing],
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()

    assert outcome.claimed_count == 1
    assert outcome.processed_count == 1
    assert outcome.success_count == 1
    assert outcome.failure_count == 0
    assert outcome.fetch_mode == "auto"
    assert outcome.due_lag_seconds_p95 is not None
    assert outcome.site_success_counts.get("generic") == 1

    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.last_amount == "80.00"
    assert updated.lease_owner is None
    assert updated.lease_until is None
    assert updated.next_check_at is not None

    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert snapshots
    assert snapshots[0].success is True
    assert snapshots[0].amount == "80.00"

    runs = repo.list_check_runs(item.item_id, limit=5)
    assert runs
    assert runs[0].status == "succeeded"


def test_scheduler_run_once_failure_keeps_last_good_price() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_failure",
        url="https://example.com/failure",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
        last_amount="120.00",
    )
    repo.save_item(item)
    previous_checked_at = item.last_checked_at

    scrape_result = PriceResult(
        url=item.source_url,
        success=False,
        error="price_not_found",
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()

    assert outcome.claimed_count == 1
    assert outcome.processed_count == 1
    assert outcome.success_count == 0
    assert outcome.failure_count == 1
    assert outcome.site_failure_counts.get("generic") == 1
    assert outcome.failure_class_counts.get("price_not_found") == 1

    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.last_amount == "120.00"
    assert updated.availability == "unknown"
    assert updated.last_checked_at == previous_checked_at
    assert updated.last_check_finished_at is not None
    assert updated.last_check_finished_at != previous_checked_at
    assert updated.lease_owner is None
    assert updated.lease_until is None
    assert updated.next_check_at is not None

    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert snapshots
    assert snapshots[0].success is False
    assert snapshots[0].error == "price_not_found"
    assert snapshots[0].availability == "unknown"

    runs = repo.list_check_runs(item.item_id, limit=5)
    assert runs
    assert runs[0].status == "failed"


def test_scheduler_failure_preserves_known_out_of_stock_availability() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = replace(
        _item(
            item_id="trk_failure_preserves_oos",
            url="https://www.amazon.com.tr/dp/B0D6YXHYCX",
            now=now,
            next_check_at=_iso(now - timedelta(minutes=1)),
            last_amount="13702.00",
        ),
        availability="out_of_stock",
        site_key="amazon",
        adapter_key="amazon",
        last_source="amazon:hidden-buybox-display:0",
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=False,
        error="price_not_found",
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()

    assert outcome.success_count == 0
    assert outcome.failure_count == 1

    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.last_amount == "13702.00"
    assert updated.availability == "out_of_stock"

    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert snapshots[0].success is False
    assert snapshots[0].availability == "out_of_stock"
    assert snapshots[0].error == "price_not_found"


def test_scheduler_defers_when_monthly_credits_are_exhausted() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_exhausted",
        url="https://example.com/exhausted",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
        last_amount="100.00",
    )
    repo.save_item(item)
    repo.entitlements_by_user["legacy_owner"] = replace(
        repo.entitlements_by_user["legacy_owner"],
        monthly_check_units=0,
        updated_at=_iso(now),
    )

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="80.00",
        currency="USD",
        raw_text="$80.00",
        source="test:exhausted",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()

    assert outcome.claimed_count == 1
    assert outcome.processed_count == 0
    assert outcome.skipped_count == 1
    assert outcome.success_count == 0
    assert outcome.failure_count == 0
    assert outcome.failure_class_counts.get("credit_exhausted") == 1

    runs = repo.list_check_runs(item.item_id, limit=5)
    assert runs
    assert runs[0].status == "failed"
    assert runs[0].error == "monthly_credit_exhausted"

    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert snapshots == []

    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.last_amount == "100.00"
    assert updated.next_check_at is not None
    _, period_end = _usage_period_bounds(_iso(now))
    assert updated.next_check_at == period_end


def test_scheduler_due_lag_metrics_are_reported() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    lagging_item = _item(
        item_id="trk_lag",
        url="https://example.com/lag",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=10)),
        last_amount="50.00",
    )
    repo.save_item(lagging_item)

    scrape_result = PriceResult(
        url=lagging_item.source_url,
        success=True,
        amount="49.00",
        currency="USD",
        raw_text="$49.00",
        source="test:lag",
        site_key="generic",
        adapter_key="generic",
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({lagging_item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.due_lag_seconds_p50 is not None
    assert outcome.due_lag_seconds_p95 is not None
    assert outcome.due_lag_seconds_p99 is not None
    assert outcome.due_lag_seconds_p95 >= 590


def test_scheduler_skips_duplicate_job_key_and_avoids_duplicate_snapshot() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    scheduled_for = _iso(now - timedelta(minutes=2))
    item = _item(
        item_id="trk_dupe",
        url="https://example.com/dupe",
        now=now,
        next_check_at=scheduled_for,
        last_amount="70.00",
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="69.00",
        currency="USD",
        raw_text="$69.00",
        source="test:dupe",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    first = scheduler.run_once()
    assert first.success_count == 1
    first_item = repo.get_item(item.item_id)
    assert first_item is not None
    assert first_item.next_check_at is not None

    # Force the same schedule key again to emulate a duplicate queue message.
    repo.save_item(
        replace(
            first_item,
            next_check_at=scheduled_for,
            lease_owner=None,
            lease_until=None,
        )
    )
    second = scheduler.run_once()
    assert second.claimed_count == 1
    assert second.processed_count == 0
    assert second.skipped_count == 1
    assert second.failure_class_counts.get("duplicate_job") == 1

    snapshots = repo.list_snapshots(item.item_id, limit=10)
    assert len(snapshots) == 1


def test_scheduler_emits_alert_event_on_price_below_crossing() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_alert",
        url="https://example.com/alert",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
        last_amount="120.00",
        target_amount="100.00",
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="95.00",
        currency="USD",
        raw_text="$95.00",
        source="test:alert",
        site_key="generic",
        adapter_key="generic",
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
        alert_cooldown_minutes=60,
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1

    events = repo.list_alert_events(item.item_id, limit=10)
    assert len(events) == 1
    assert events[0].rule_type == "price_below"
    assert events[0].previous_amount == "120.00"
    assert events[0].current_amount == "95.00"
    assert events[0].target_amount == "100.00"
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    assert notifications[0].status == "queued"
    assert notifications[0].alert_event_id == events[0].event_id
    payload = json.loads(notifications[0].payload)
    assert "outbound_url" not in payload
    assert payload["product_url"] == item.source_url


def test_scheduler_includes_affiliate_template_link_in_notification_payload() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_affiliate",
        url="https://example.com/product/123?ref=abc",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
        last_amount="200.00",
        target_amount="100.00",
    )
    repo.save_item(item)
    repo.set_active_telegram_chat(chat_id="12345", updated_at=_iso(now))

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:affiliate",
        site_key="generic",
        adapter_key="generic",
        canonical_url="https://example.com/product/123",
        listings=[
            PriceListing(
                item_id="itm_aff",
                amount="90.00",
                currency="USD",
                raw_text="$90.00",
                source="test:affiliate",
                listing_url="https://merchant.example.com/buy/123",
            )
        ],
        listing_count=1,
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
        alert_cooldown_minutes=60,
        affiliate_redirect_base_url="https://app.example.com",
        affiliate_signing_secret="secret",
        affiliate_link_template="https://go.example.com/out?u={url}",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1

    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    assert notifications[0].channel == "telegram"
    payload = json.loads(notifications[0].payload)
    assert payload["product_url"] == "https://example.com/product/123"
    assert payload["listing_url"] == "https://merchant.example.com/buy/123"
    assert payload["outbound_url"].startswith("https://app.example.com/r/")
    assert payload["outbound_is_affiliate"] is True

def test_scheduler_monitor_mode_does_not_notify_when_price_unchanged() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_monitor_notify",
        url="https://example.com/monitor",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,  # monitor mode
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="95.00",
        currency="USD",
        raw_text="$95.00",
        source="test:monitor",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    first = scheduler.run_once()
    assert first.success_count == 1
    assert repo.list_alert_events(item.item_id, limit=10) == []

    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert notifications == []


def test_scheduler_monitor_mode_notifies_when_price_increases_by_default() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_monitor_increase_default",
        url="https://example.com/monitor-increase-default",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,
        notify_on_price_increase=False,
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="99.00",
        currency="USD",
        raw_text="$99.00",
        source="test:monitor-increase-default",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "price_increase"
    assert payload["previous_amount"] == "95.00"
    assert payload["current_amount"] == "99.00"


def test_scheduler_alert_mode_does_not_notify_price_increase_without_setting() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_alert_no_increase_notify",
        url="https://example.com/alert-increase-disabled",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount="80.00",
        notify_on_price_increase=False,
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="99.00",
        currency="USD",
        raw_text="$99.00",
        source="test:alert-increase-disabled",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    assert repo.list_notification_events(item.item_id, limit=10) == []


def test_scheduler_monitor_mode_notifies_when_price_increases_with_setting_enabled() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_monitor_increase_enabled",
        url="https://example.com/monitor-increase-enabled",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,
        notify_on_price_increase=True,
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="101.00",
        currency="USD",
        raw_text="$101.00",
        source="test:monitor-increase-enabled",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1

    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "price_increase"
    assert payload["previous_amount"] == "95.00"
    assert payload["current_amount"] == "101.00"


def test_scheduler_monitor_mode_notifies_when_price_drops() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_monitor_drop",
        url="https://example.com/monitor-drop",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,  # monitor mode
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:monitor-drop",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    assert repo.list_alert_events(item.item_id, limit=10) == []

    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    assert notifications[0].alert_event_id is None
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "price_drop"
    assert payload["previous_amount"] == "95.00"
    assert payload["current_amount"] == "90.00"


def test_scheduler_monitor_mode_uses_first_listing_for_price_drop_notification() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_monitor_drop_primary_listing",
        url="https://example.com/monitor-drop-primary-listing",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,
    )
    repo.save_item(item)

    first_listing = PriceListing(
        item_id="itm_first",
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:first-listing",
        listing_url="https://merchant.example.com/first",
    )
    later_listing = PriceListing(
        item_id="itm_later",
        amount="110.00",
        currency="USD",
        raw_text="$110.00",
        source="test:later-listing",
        listing_url="https://merchant.example.com/later",
    )
    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount=later_listing.amount,
        currency=later_listing.currency,
        raw_text=later_listing.raw_text,
        source=later_listing.source,
        site_key="generic",
        adapter_key="generic",
        listings=[first_listing, later_listing],
        listing_count=2,
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1

    saved_item = repo.get_item(item.item_id)
    assert saved_item is not None
    assert saved_item.last_amount == "90.00"
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "price_drop"
    assert payload["previous_amount"] == "95.00"
    assert payload["current_amount"] == "90.00"
    assert payload["listing_url"] == "https://merchant.example.com/first"


def test_scheduler_tracks_stored_listing_identity_instead_of_current_first_listing() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = replace(
        _item(
            item_id="trk_monitor_selected_listing",
            url="https://example.com/monitor-selected-listing",
            now=now,
            next_check_at=due_time,
            last_amount="120.00",
            target_amount=None,
        ),
        tracked_listing_id="itm_selected",
        tracked_listing_url="https://merchant.example.com/selected",
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:current-first",
        site_key="generic",
        adapter_key="generic",
        listings=[
            PriceListing(
                item_id="itm_other",
                amount="90.00",
                currency="USD",
                raw_text="$90.00",
                source="test:current-first",
                listing_url="https://merchant.example.com/current-first",
            ),
            PriceListing(
                item_id="itm_selected",
                amount="110.00",
                currency="USD",
                raw_text="$110.00",
                source="test:selected",
                listing_url="https://merchant.example.com/selected",
            ),
        ],
        listing_count=2,
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1

    saved_item = repo.get_item(item.item_id)
    assert saved_item is not None
    assert saved_item.last_amount == "110.00"
    assert saved_item.tracked_listing_id == "itm_selected"
    assert saved_item.tracked_listing_url == "https://merchant.example.com/selected"
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "price_drop"
    assert payload["current_amount"] == "110.00"
    assert payload["listing_url"] == "https://merchant.example.com/selected"


def test_scheduler_tracks_akakce_listing_when_fragment_prefix_changes() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    source_url = (
        "https://www.akakce.com/ssd/en-ucuz-samsung-9100-pro-mz-vap2t0bw-"
        "pci-express-5-0-2-tb-m-2-fiyati,983826204.html"
    )
    item = replace(
        _item(
            item_id="trk_akakce_selected_listing",
            url=source_url,
            now=now,
            next_check_at=due_time,
            last_amount="21000.00",
            target_amount=None,
        ),
        site_key="akakce",
        adapter_key="akakce",
        tracked_listing_id="itm_old_volatile",
        tracked_listing_url=(
            "#/8860528/?s=1&v=1538&p=983826204&c=15598&g=1472419210"
            "&f=%2fr%2f%3fpr%3d983826204%26vd%3d1538%26pg%3d1472419210"
        ),
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="20929.09",
        currency="TRY",
        raw_text="20.929,09 TL",
        source="akakce:dom",
        site_key="akakce",
        adapter_key="akakce",
        listings=[
            PriceListing(
                item_id="itm_other",
                amount="20929.09",
                currency="TRY",
                raw_text="20.929,09 TL",
                source="akakce:dom",
                seller="nitrotechnology",
                listing_url=(
                    "#/753723/?s=3&v=11070&p=983826204&c=15663&g=1420412326"
                ),
            ),
            PriceListing(
                item_id="itm_new_stable",
                amount="20750.00",
                currency="TRY",
                raw_text="20.750,00 TL",
                source="akakce:dom",
                seller="Ucuzbudur.com",
                listing_url=(
                    "#/753723/?s=1&v=1538&p=983826204&c=15598&g=1472419210"
                    "&f=%2fr%2f%3fpr%3d983826204%26vd%3d1538%26pg%3d1472419210"
                ),
            ),
        ],
        listing_count=2,
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()

    assert outcome.success_count == 1
    saved_item = repo.get_item(item.item_id)
    assert saved_item is not None
    assert saved_item.last_amount == "20750.00"
    snapshots = repo.list_snapshots(item.item_id, limit=1)
    assert snapshots[0].success is True
    assert snapshots[0].error is None


def test_scheduler_marks_selected_listing_unavailable_without_fallback() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = replace(
        _item(
            item_id="trk_monitor_selected_listing_missing",
            url="https://example.com/monitor-selected-listing-missing",
            now=now,
            next_check_at=due_time,
            last_amount="120.00",
            target_amount=None,
        ),
        tracked_listing_id="itm_selected",
        tracked_listing_url="https://merchant.example.com/selected",
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:other-listing",
        site_key="generic",
        adapter_key="generic",
        listings=[
            PriceListing(
                item_id="itm_other",
                amount="90.00",
                currency="USD",
                raw_text="$90.00",
                source="test:other-listing",
                listing_url="https://merchant.example.com/other",
            )
        ],
        listing_count=1,
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.failure_count == 1

    saved_item = repo.get_item(item.item_id)
    assert saved_item is not None
    assert saved_item.last_amount == "120.00"
    assert saved_item.tracked_listing_id == "itm_selected"
    assert saved_item.tracked_listing_url == "https://merchant.example.com/selected"

    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "selected_offer_unavailable"
    assert payload["error"] == "selected_listing_not_found"
    assert payload["listing_url"] == "https://merchant.example.com/selected"
    snapshots = repo.list_snapshots(item.item_id, limit=1)
    assert snapshots[0].success is False
    assert snapshots[0].error == "selected_listing_not_found"


def test_scheduler_marks_missing_primary_listing_price_as_unknown_failure() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_monitor_primary_listing_missing_price",
        url="https://example.com/monitor-missing-primary-price",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:stale-top-level",
        site_key="generic",
        adapter_key="generic",
        listings=[
            PriceListing(
                item_id="itm_primary_missing",
                amount="",
                currency="USD",
                raw_text="",
                source="test:primary-missing",
                listing_url="https://merchant.example.com/primary",
            )
        ],
        listing_count=1,
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 0
    assert outcome.failure_count == 1

    saved_item = repo.get_item(item.item_id)
    assert saved_item is not None
    assert saved_item.last_amount == "95.00"
    assert saved_item.availability == "unknown"
    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert snapshots[0].success is False
    assert snapshots[0].availability == "unknown"
    assert snapshots[0].error == "price_not_found"
    assert repo.list_notification_events(item.item_id, limit=10) == []


def test_scheduler_does_not_update_tracker_from_low_confidence_price() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_low_confidence_price",
        url="https://example.com/low-confidence-price",
        now=now,
        next_check_at=due_time,
        last_amount="100.00",
        target_amount="50.00",
    )
    repo.save_item(item)

    listing = PriceListing(
        item_id="itm_weak",
        amount="10.00",
        currency="USD",
        raw_text="$10.00",
        source="find:regex:first",
        confidence="low",
        signals=("find_regex",),
    )
    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount=listing.amount,
        currency=listing.currency,
        raw_text=listing.raw_text,
        source=listing.source,
        product_name="Weak Product",
        site_key="generic",
        adapter_key="generic",
        listings=[listing],
        listing_count=1,
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.failure_count == 1

    saved_item = repo.get_item(item.item_id)
    assert saved_item is not None
    assert saved_item.last_amount == "100.00"
    assert repo.list_alert_events(item.item_id, limit=10) == []
    snapshots = repo.list_snapshots(item.item_id, limit=1)
    assert snapshots[0].success is False
    assert snapshots[0].amount is None
    assert snapshots[0].error == "low_confidence_price"


def test_scheduler_emits_attention_notification_after_three_consecutive_failures() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_attention_failures",
        url="https://example.com/attention-failures",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,
    )
    repo.save_item(item)
    for index in range(2):
        repo.add_snapshot(
            PriceSnapshot(
                snapshot_id=f"snp_attention_previous_{index}",
                item_id=item.item_id,
                checked_at=_iso(now - timedelta(minutes=3 - index)),
                success=False,
                amount=None,
                currency="USD",
                raw_text=None,
                source=None,
                error="price_not_found",
            )
        )

    scrape_result = PriceResult(
        url=item.source_url,
        success=False,
        error="blocked_by_protection",
        product_name="Attention Item",
        canonical_url=item.source_url,
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.failure_count == 1

    saved_item = repo.get_item(item.item_id)
    assert saved_item is not None
    assert saved_item.last_amount == "95.00"
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "tracking_attention"
    assert payload["consecutive_failure_count"] == 3
    assert payload["error"] == "blocked_by_protection"
    assert payload["product_url"] == item.source_url


def test_scheduler_does_not_repeat_attention_notification_after_threshold() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_attention_already_failed",
        url="https://example.com/attention-already-failed",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,
    )
    repo.save_item(item)
    for index in range(3):
        repo.add_snapshot(
            PriceSnapshot(
                snapshot_id=f"snp_attention_existing_{index}",
                item_id=item.item_id,
                checked_at=_iso(now - timedelta(minutes=4 - index)),
                success=False,
                amount=None,
                currency="USD",
                raw_text=None,
                source=None,
                error="price_not_found",
            )
        )

    scrape_result = PriceResult(
        url=item.source_url,
        success=False,
        error="blocked_by_protection",
        product_name="Attention Item",
        canonical_url=item.source_url,
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.failure_count == 1
    assert repo.list_notification_events(item.item_id, limit=10) == []


def test_scheduler_out_of_stock_success_keeps_last_price_and_skips_price_notifications() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_out_of_stock",
        url="https://example.com/out-of-stock",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,
        notify_on_price_increase=True,
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount=None,
        currency=None,
        raw_text=None,
        source=None,
        availability="out_of_stock",
        product_name="Stockless",
        image_url="https://example.com/image.jpg",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.last_amount == "95.00"
    assert updated.availability == "out_of_stock"
    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert len(snapshots) == 1
    assert snapshots[0].success is True
    assert snapshots[0].amount is None
    assert snapshots[0].availability == "out_of_stock"
    assert repo.list_notification_events(item.item_id, limit=10) == []


def test_scheduler_emits_stock_out_notification_when_enabled() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_stock_out_notify",
        url="https://example.com/stock-out",
        now=now,
        next_check_at=due_time,
        last_amount="105.00",
        target_amount=None,
        notify_on_stock_change=True,
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount=None,
        currency=None,
        raw_text=None,
        source=None,
        availability="out_of_stock",
        product_name="Stock Out Item",
        image_url="https://example.com/image.jpg",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "stock_out"
    assert payload["current_availability"] == "out_of_stock"
    assert "current_amount" not in payload


def test_scheduler_treats_preorder_and_discontinued_as_stock_out_states() -> None:
    for availability in ("preorder", "discontinued"):
        repo = _build_repo()
        now = datetime.now(tz=UTC)
        due_time = _iso(now - timedelta(minutes=1))
        item = _item(
            item_id=f"trk_{availability}",
            url=f"https://example.com/{availability}",
            now=now,
            next_check_at=due_time,
            last_amount="105.00",
            target_amount=None,
            notify_on_stock_change=True,
        )
        repo.save_item(item)

        scrape_result = PriceResult(
            url=item.source_url,
            success=True,
            amount=None,
            currency=None,
            raw_text=None,
            source=None,
            availability=availability,
            product_name="Unavailable Item",
            image_url="https://example.com/image.jpg",
            site_key="generic",
            adapter_key="generic",
        )
        scheduler = TrackingSchedulerService(
            repository=repo,
            scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
            timeout_seconds=30,
            claim_batch_size=50,
            max_inflight=4,
            lease_seconds=180,
            jitter_percent=0,
            lease_owner="scheduler-test",
        )

        outcome = scheduler.run_once()
        assert outcome.success_count == 1
        updated = repo.get_item(item.item_id)
        assert updated is not None
        assert updated.last_amount == "105.00"
        assert updated.availability == availability
        notifications = repo.list_notification_events(item.item_id, limit=10)
        assert len(notifications) == 1
        payload = json.loads(notifications[0].payload)
        assert payload["notification_type"] == "stock_out"
        assert payload["current_availability"] == availability


def test_scheduler_emits_restocked_notification_without_price_after_out_of_stock() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_restock_notify",
        url="https://example.com/restock",
        now=now,
        next_check_at=due_time,
        last_amount="105.00",
        target_amount=None,
        notify_on_stock_change=True,
    )
    repo.save_item(replace(item, availability="out_of_stock"))

    scrape_result = PriceResult(
        url=item.source_url,
        success=False,
        amount=None,
        currency=None,
        raw_text=None,
        source=None,
        availability="in_stock",
        product_name="Restocked Item",
        image_url="https://example.com/image.jpg",
        error="price_not_found",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.availability == "in_stock"
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "stock_restocked"
    assert payload["current_availability"] == "in_stock"
    assert "current_amount" not in payload


def test_scheduler_emits_restocked_notification_with_price_after_out_of_stock() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_restock_notify_with_price",
        url="https://example.com/restock-with-price",
        now=now,
        next_check_at=due_time,
        last_amount="105.00",
        target_amount=None,
        notify_on_stock_change=True,
    )
    repo.save_item(replace(item, availability="out_of_stock"))

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="99.00",
        currency="USD",
        raw_text="$99.00",
        source="test:restock",
        availability="in_stock",
        product_name="Restocked Item",
        image_url="https://example.com/image.jpg",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.availability == "in_stock"
    assert updated.last_amount == "99.00"
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "stock_restocked"
    assert payload["current_availability"] == "in_stock"
    assert payload["current_amount"] == "99.00"


def test_scheduler_treats_confirmed_price_as_in_stock_when_stock_text_is_missing() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_price_implies_in_stock",
        url="https://example.com/price-implies-stock",
        now=now,
        next_check_at=due_time,
        last_amount="105.00",
        target_amount=None,
        notify_on_stock_change=True,
    )
    repo.save_item(replace(item, availability="out_of_stock"))

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="99.00",
        currency="USD",
        raw_text="$99.00",
        source="test:price-without-stock-text",
        availability=None,
        product_name="Restocked Item",
        image_url="https://example.com/image.jpg",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.availability == "in_stock"
    assert updated.last_amount == "99.00"
    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert len(notifications) == 1
    payload = json.loads(notifications[0].payload)
    assert payload["notification_type"] == "stock_restocked"
    assert payload["current_availability"] == "in_stock"
    assert payload["current_amount"] == "99.00"


def test_scheduler_does_not_restock_from_ambiguous_amazon_hidden_buybox_price() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = replace(
        _item(
            item_id="trk_amazon_hidden_price_oos",
            url="https://www.amazon.com.tr/dp/B0D6YXHYCX",
            now=now,
            next_check_at=due_time,
            last_amount="13702.00",
            target_amount=None,
            notify_on_stock_change=True,
        ),
        availability="out_of_stock",
        site_key="amazon",
        adapter_key="amazon",
        currency="TRY",
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="13702.00",
        currency="TRY",
        raw_text="13.702,00 TL",
        source="amazon:hidden-buybox-display:0",
        availability=None,
        product_name="Novation Launchkey",
        image_url="https://m.media-amazon.com/images/I/example.jpg",
        site_key="amazon",
        adapter_key="amazon",
        signals=("embedded_state", "amazon_buybox", "currency:TRY"),
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1

    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.availability == "out_of_stock"
    assert updated.last_amount == "13702.00"

    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert snapshots[0].success is True
    assert snapshots[0].amount is None
    assert snapshots[0].availability == "out_of_stock"
    assert repo.list_notification_events(item.item_id, limit=10) == []


def test_scheduler_does_not_notify_when_item_is_paused_during_inflight_run() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    due_time = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_pause_race",
        url="https://example.com/pause-race",
        now=now,
        next_check_at=due_time,
        last_amount="95.00",
        target_amount=None,  # monitor mode
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="94.00",
        currency="USD",
        raw_text="$94.00",
        source="test:pause-race",
        site_key="generic",
        adapter_key="generic",
    )

    @dataclass
    class _PauseDuringScrapeRunner:
        repository: _InMemoryTrackingRepository

        def scrape(self, payload) -> ScrapeBatchOutput:
            current = self.repository.get_item(item.item_id)
            if current is not None:
                # Emulate user pause action while the scheduler run is in-flight.
                self.repository.save_item(replace(current, status="paused"))
            return _FakeScrapeRunner({item.source_url: scrape_result}).scrape(payload)

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_PauseDuringScrapeRunner(repository=repo),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1

    updated_item = repo.get_item(item.item_id)
    assert updated_item is not None
    assert updated_item.status == "paused"

    notifications = repo.list_notification_events(item.item_id, limit=10)
    assert notifications == []


def test_scheduler_reclaims_stale_running_duplicate_job() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    scheduled_for = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_stale_dupe",
        url="https://example.com/stale-dupe",
        now=now,
        next_check_at=scheduled_for,
        last_amount="100.00",
    )
    repo.save_item(item)

    stale_run = TrackingCheckRun(
        run_id="run_stale",
        item_id=item.item_id,
        scheduled_for=scheduled_for,
        started_at=_iso(now - timedelta(hours=1)),
        finished_at=None,
        status="running",
        attempt=1,
        error=None,
        job_key=f"{item.item_id}:{scheduled_for}",
    )
    repo.runs_by_id[stale_run.run_id] = stale_run
    repo.runs_by_job_key[stale_run.job_key or ""] = stale_run.run_id

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:stale-dupe",
        site_key="generic",
        adapter_key="generic",
    )
    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
    )

    outcome = scheduler.run_once()

    assert outcome.claimed_count == 1
    assert outcome.processed_count == 1
    assert outcome.skipped_count == 0
    assert outcome.success_count == 1

    reclaimed = repo.runs_by_id["run_stale"]
    assert reclaimed.status == "failed"
    assert reclaimed.error == "stale_run_reclaimed"
    assert reclaimed.job_key is None


def test_scheduler_does_not_emit_alert_without_crossing() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_no_cross",
        url="https://example.com/no-cross",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
        last_amount="95.00",
        target_amount="100.00",
    )
    repo.save_item(item)

    scrape_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:no-cross",
        site_key="generic",
        adapter_key="generic",
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: scrape_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
        alert_cooldown_minutes=60,
    )

    outcome = scheduler.run_once()
    assert outcome.success_count == 1
    assert repo.list_alert_events(item.item_id, limit=10) == []


def test_scheduler_suppresses_alerts_during_cooldown() -> None:
    repo = _build_repo()
    now = datetime.now(tz=UTC)
    scheduled_for = _iso(now - timedelta(minutes=1))
    item = _item(
        item_id="trk_cooldown",
        url="https://example.com/cooldown",
        now=now,
        next_check_at=scheduled_for,
        last_amount="120.00",
        target_amount="100.00",
    )
    repo.save_item(item)

    first_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="95.00",
        currency="USD",
        raw_text="$95.00",
        source="test:cooldown-1",
        site_key="generic",
        adapter_key="generic",
    )
    second_result = PriceResult(
        url=item.source_url,
        success=True,
        amount="94.00",
        currency="USD",
        raw_text="$94.00",
        source="test:cooldown-2",
        site_key="generic",
        adapter_key="generic",
    )

    scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: first_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
        alert_cooldown_minutes=60,
    )

    first = scheduler.run_once()
    assert first.success_count == 1
    assert len(repo.list_alert_events(item.item_id, limit=10)) == 1

    updated = repo.get_item(item.item_id)
    assert updated is not None
    second_due_time = _iso(now - timedelta(minutes=2))
    repo.save_item(
        replace(
            updated,
            last_amount="150.00",
            next_check_at=second_due_time,
            lease_owner=None,
            lease_until=None,
        )
    )
    second_scheduler = TrackingSchedulerService(
        repository=repo,
        scrape_runner=_FakeScrapeRunner({item.source_url: second_result}),
        timeout_seconds=30,
        claim_batch_size=50,
        max_inflight=4,
        lease_seconds=180,
        jitter_percent=0,
        lease_owner="scheduler-test",
        alert_cooldown_minutes=60,
    )

    second = second_scheduler.run_once()
    assert second.success_count == 1
    assert len(repo.list_alert_events(item.item_id, limit=10)) == 1
