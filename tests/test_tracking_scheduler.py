from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.application.scrape_service import ScrapeBatchOutput
from app.application.tracking_scheduler_service import (
    TrackingSchedulerService,
    compute_next_check_at,
)
from app.infrastructure.sqlite_tracking_repository import SQLiteTrackingRepository
from app.scraper import PriceListing, PriceResult
from app.tracking import TrackedItem


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _item(
    *,
    item_id: str,
    url: str,
    now: datetime,
    status: str = "active",
    next_check_at: str | None = None,
    last_amount: str | None = "100.00",
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
        target_amount=None,
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


def _build_repo(tmp_path: Path) -> SQLiteTrackingRepository:
    repo = SQLiteTrackingRepository(str(tmp_path / "tracking.sqlite3"))
    repo.initialize()
    return repo


def test_claim_due_items_filters_status_and_due_time(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
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


def test_claim_due_items_reclaims_after_lease_expiry(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
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


def test_scheduler_run_once_persists_success_snapshot_and_next_check(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
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


def test_scheduler_run_once_failure_keeps_last_good_price(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    now = datetime.now(tz=UTC)
    item = _item(
        item_id="trk_failure",
        url="https://example.com/failure",
        now=now,
        next_check_at=_iso(now - timedelta(minutes=1)),
        last_amount="120.00",
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

    assert outcome.claimed_count == 1
    assert outcome.processed_count == 1
    assert outcome.success_count == 0
    assert outcome.failure_count == 1

    updated = repo.get_item(item.item_id)
    assert updated is not None
    assert updated.last_amount == "120.00"
    assert updated.lease_owner is None
    assert updated.lease_until is None
    assert updated.next_check_at is not None

    snapshots = repo.list_snapshots(item.item_id, limit=5)
    assert snapshots
    assert snapshots[0].success is False
    assert snapshots[0].error == "price_not_found"

    runs = repo.list_check_runs(item.item_id, limit=5)
    assert runs
    assert runs[0].status == "failed"
