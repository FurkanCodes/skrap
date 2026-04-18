from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.api.dependencies import get_scrape_result_cache, get_tracking_service
from app.application.tracking_service import (
    TrackingBootstrapError,
    TrackingBootstrapInput,
    TrackingDetailOutput,
    TrackingServiceError,
    TrackingUpdateInput,
)
from app.main import app
from app.scraper import PriceResult
from app.tracking.domain import PriceSnapshot, TrackedItem


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


@dataclass
class FakeTrackingService:
    items: dict[str, TrackedItem]
    snapshots: dict[str, list[PriceSnapshot]]
    bootstrap_calls: int = 0
    last_bootstrap_payload: TrackingBootstrapInput | None = None

    def list_sites(self) -> dict[str, str]:
        return {
            "akakce": "Akakce adapter",
            "generic": "Generic extractor (fallback for any site)",
            "newegg": "Newegg adapter",
        }

    def bootstrap(self, payload: TrackingBootstrapInput) -> TrackedItem:
        self.bootstrap_calls += 1
        self.last_bootstrap_payload = payload
        if "fail" in payload.url:
            raise TrackingBootstrapError("bootstrap failed: price_not_found")

        now = _now_iso()
        item = TrackedItem(
            item_id="trk_test_1",
            source_url=payload.url,
            canonical_url=payload.url,
            product_id="prd_test",
            site_key="generic",
            adapter_key="generic",
            product_name="Test Product",
            brand="TestBrand",
            model="TB-01",
            sku="SKU-01",
            gtin="GTIN-01",
            image_url="https://example.com/image.jpg",
            availability="in_stock",
            currency="USD",
            last_amount="99.99",
            last_raw_text="$99.99",
            last_source="test:fake",
            status="active",
            check_interval_minutes=payload.check_interval_minutes,
            target_amount=payload.target_amount,
            created_at=now,
            updated_at=now,
            last_checked_at=now,
        )
        self.items[item.item_id] = item
        self.snapshots[item.item_id] = [
            PriceSnapshot(
                snapshot_id="snp_test_1",
                item_id=item.item_id,
                checked_at=now,
                success=True,
                amount=item.last_amount,
                currency=item.currency,
                raw_text=item.last_raw_text,
                source=item.last_source,
                error=None,
            )
        ]
        return item

    def bootstrap_idempotent(
        self,
        *,
        payload: TrackingBootstrapInput,
        idempotency_key: str,
    ) -> TrackedItem:
        if idempotency_key == "conflict":
            raise TrackingServiceError(
                "Idempotency-Key reuse with different payload is not allowed"
            )
        return self.bootstrap(payload)

    def list_items(self, *, user_id: str | None = None) -> list[TrackedItem]:  # noqa: ARG002
        return list(self.items.values())

    def get_detail(
        self,
        item_id: str,
        snapshot_limit: int | None = None,
        *,
        user_id: str | None = None,  # noqa: ARG002
    ) -> TrackingDetailOutput | None:
        item = self.items.get(item_id)
        if item is None:
            return None

        snapshots = self.snapshots.get(item_id, [])
        limit = snapshot_limit or len(snapshots)
        return TrackingDetailOutput(item=item, recent_snapshots=snapshots[:limit])

    def update_item(self, payload: TrackingUpdateInput) -> TrackedItem | None:
        item = self.items.get(payload.item_id)
        if item is None:
            return None

        updated = TrackedItem(
            item_id=item.item_id,
            source_url=item.source_url,
            canonical_url=item.canonical_url,
            product_id=item.product_id,
            site_key=item.site_key,
            adapter_key=item.adapter_key,
            product_name=item.product_name,
            brand=item.brand,
            model=item.model,
            sku=item.sku,
            gtin=item.gtin,
            image_url=item.image_url,
            availability=item.availability,
            currency=item.currency,
            last_amount=item.last_amount,
            last_raw_text=item.last_raw_text,
            last_source=item.last_source,
            status=payload.status or item.status,
            check_interval_minutes=payload.check_interval_minutes or item.check_interval_minutes,
            target_amount=(
                payload.target_amount
                if payload.target_amount_provided
                else item.target_amount
            ),
            created_at=item.created_at,
            updated_at=_now_iso(),
            last_checked_at=item.last_checked_at,
        )
        self.items[item.item_id] = updated
        return updated

    def delete_item(self, item_id: str, *, user_id: str | None = None) -> bool:  # noqa: ARG002
        existed = item_id in self.items
        self.items.pop(item_id, None)
        self.snapshots.pop(item_id, None)
        return existed


@dataclass
class FakeScrapeResultCache:
    result_by_ref: dict[str, PriceResult]

    def get_result(self, result_ref: str) -> PriceResult | None:
        return self.result_by_ref.get(result_ref)


def _build_client(
    fake_tracking_service: FakeTrackingService,
    fake_scrape_cache: FakeScrapeResultCache | None = None,
) -> TestClient:
    app.dependency_overrides[get_tracking_service] = lambda: fake_tracking_service
    if fake_scrape_cache is not None:
        app.dependency_overrides[get_scrape_result_cache] = lambda: fake_scrape_cache
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_tracked_items_bootstrap_creates_item() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    client = _build_client(service)

    response = client.post(
        "/api/v1/tracked-items",
        json={
            "url": "https://example.com/products/test-1",
            "sites": ["generic"],
            "timeout_seconds": 20,
            "fetch_mode": "auto",
            "check_interval_minutes": 30,
            "target_amount": "89.99",
        },
    )
    _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["item_id"] == "trk_test_1"
    assert body["item"]["source_url"] == "https://example.com/products/test-1"
    assert body["item"]["check_interval_minutes"] == 30
    assert body["item"]["target_amount"] == "89.99"
    assert body["item"]["tracking_mode"] == "alert"
    assert body["item"]["alerts_enabled"] is True
    assert "next_check_at" in body["item"]
    assert "lease_until" in body["item"]
    assert "last_check_started_at" in body["item"]
    assert "last_check_finished_at" in body["item"]


def test_tracked_items_bootstrap_from_scrape_result_ref_without_url() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    cached_result = PriceResult(
        url="https://example.com/products/from-cache",
        success=True,
        product_id="prd_cached",
        site_key="generic",
        adapter_key="generic",
        amount="49.99",
        currency="USD",
        raw_text="$49.99",
        source="test:cache",
    )
    cache = FakeScrapeResultCache(result_by_ref={"result-ref-1": cached_result})
    client = _build_client(service, fake_scrape_cache=cache)

    response = client.post(
        "/api/v1/tracked-items",
        json={
            "scrape_result_ref": "result-ref-1",
            "check_interval_minutes": 30,
            "target_amount": "45.00",
        },
    )
    _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["source_url"] == "https://example.com/products/from-cache"
    assert service.bootstrap_calls == 1
    assert service.last_bootstrap_payload is not None
    assert service.last_bootstrap_payload.scrape_result_ref == "result-ref-1"
    assert service.last_bootstrap_payload.pre_fetched_result is not None


def test_tracked_items_bootstrap_rejects_expired_scrape_result_ref() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    cache = FakeScrapeResultCache(result_by_ref={})
    client = _build_client(service, fake_scrape_cache=cache)

    response = client.post(
        "/api/v1/tracked-items",
        json={
            "scrape_result_ref": "missing-result-ref",
        },
    )
    _clear_overrides()

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid or expired scrape_result_ref"


def test_tracked_items_bootstrap_without_target_is_monitor_mode() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    client = _build_client(service)

    response = client.post(
        "/api/v1/tracked-items",
        json={
            "url": "https://example.com/products/test-2",
            "sites": ["generic"],
        },
    )
    _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["target_amount"] is None
    assert body["item"]["tracking_mode"] == "monitor"
    assert body["item"]["alerts_enabled"] is False


def test_tracked_items_bootstrap_requires_url_or_scrape_result_ref() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    client = _build_client(service)

    response = client.post(
        "/api/v1/tracked-items",
        json={
            "sites": ["generic"],
        },
    )
    _clear_overrides()

    assert response.status_code == 422


def test_tracked_items_bootstrap_rejects_unknown_site_key() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    client = _build_client(service)

    response = client.post(
        "/api/v1/tracked-items",
        json={
            "url": "https://example.com/products/test-1",
            "sites": ["unknown_site"],
        },
    )
    _clear_overrides()

    assert response.status_code == 422
    assert "Unsupported site key" in response.json()["detail"]


def test_tracked_items_bootstrap_maps_bootstrap_errors() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    client = _build_client(service)

    response = client.post(
        "/api/v1/tracked-items",
        json={"url": "https://example.com/fail-product"},
    )
    _clear_overrides()

    assert response.status_code == 422
    assert response.json()["detail"] == "bootstrap failed: price_not_found"


def test_tracked_items_list_detail_patch_delete_flow() -> None:
    now = _now_iso()
    existing = TrackedItem(
        item_id="trk_existing",
        source_url="https://example.com/p/1",
        canonical_url="https://example.com/p/1",
        product_id="prd_existing",
        site_key="generic",
        adapter_key="generic",
        product_name="Existing Product",
        brand=None,
        model=None,
        sku=None,
        gtin=None,
        image_url=None,
        availability=None,
        currency="USD",
        last_amount="20.00",
        last_raw_text="$20.00",
        last_source="test:init",
        status="active",
        check_interval_minutes=60,
        target_amount=None,
        created_at=now,
        updated_at=now,
        last_checked_at=now,
    )
    snapshot = PriceSnapshot(
        snapshot_id="snp_existing",
        item_id=existing.item_id,
        checked_at=now,
        success=True,
        amount="20.00",
        currency="USD",
        raw_text="$20.00",
        source="test:init",
        error=None,
    )

    service = FakeTrackingService(
        items={existing.item_id: existing},
        snapshots={existing.item_id: [snapshot]},
    )
    client = _build_client(service)

    list_response = client.get("/api/v1/tracked-items")
    assert list_response.status_code == 200
    assert list_response.json()["items"][0]["item_id"] == "trk_existing"
    assert list_response.json()["items"][0]["tracking_mode"] == "monitor"
    assert list_response.json()["items"][0]["alerts_enabled"] is False
    assert "next_check_at" in list_response.json()["items"][0]

    detail_response = client.get(f"/api/v1/tracked-items/{existing.item_id}")
    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    assert detail_body["item"]["item_id"] == "trk_existing"
    assert detail_body["item"]["tracking_mode"] == "monitor"
    assert detail_body["recent_snapshots"][0]["snapshot_id"] == "snp_existing"

    patch_response = client.patch(
        f"/api/v1/tracked-items/{existing.item_id}",
        json={"status": "paused", "target_amount": "18.00"},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["item"]["status"] == "paused"
    assert patch_response.json()["item"]["target_amount"] == "18.00"
    assert patch_response.json()["item"]["tracking_mode"] == "alert"
    assert patch_response.json()["item"]["alerts_enabled"] is True

    delete_response = client.delete(f"/api/v1/tracked-items/{existing.item_id}")
    assert delete_response.status_code == 204

    missing_response = client.get(f"/api/v1/tracked-items/{existing.item_id}")
    _clear_overrides()

    assert missing_response.status_code == 404
    assert missing_response.json()["detail"] == "tracked item not found"


def test_tracked_items_bootstrap_accepts_idempotency_key_header() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    client = _build_client(service)

    response = client.post(
        "/api/v1/tracked-items",
        headers={"Idempotency-Key": "boot-1"},
        json={
            "url": "https://example.com/products/test-idempo",
            "sites": ["generic"],
        },
    )
    _clear_overrides()

    assert response.status_code == 200
    assert service.bootstrap_calls == 1


def test_tracked_items_bootstrap_idempotency_conflict_returns_409() -> None:
    service = FakeTrackingService(items={}, snapshots={})
    client = _build_client(service)

    response = client.post(
        "/api/v1/tracked-items",
        headers={"Idempotency-Key": "conflict"},
        json={
            "url": "https://example.com/products/test-idempo-conflict",
            "sites": ["generic"],
        },
    )
    _clear_overrides()

    assert response.status_code == 409
    assert "Idempotency-Key reuse with different payload" in response.json()["detail"]
