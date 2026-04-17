from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.api.dependencies import get_discovery_service, get_scrape_service
from app.application.discovery_service import (
    DiscoveryBatchInput,
    DiscoveryBatchOutput,
    DiscoveryPhaseError,
)
from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchOutput
from app.main import app
from app.scraper import PriceListing, PriceResult
from app.scraper.discovery import DiscoveryStats


@dataclass
class FakeScrapeService:
    last_input: ScrapeBatchInput | None = None

    def list_sites(self) -> dict[str, str]:
        return {
            "akakce": "Akakce adapter",
            "generic": "Generic extractor (fallback for any site)",
            "newegg": "Newegg adapter",
        }

    def scrape(self, payload: ScrapeBatchInput) -> ScrapeBatchOutput:
        deduped_urls: list[str] = []
        seen: set[str] = set()
        for url in payload.urls:
            if url in seen:
                continue
            seen.add(url)
            deduped_urls.append(url)

        self.last_input = ScrapeBatchInput(
            request_id=payload.request_id,
            urls=deduped_urls,
            timeout_seconds=payload.timeout_seconds,
            selected_sites=payload.selected_sites,
            fetch_mode=payload.fetch_mode,
        )
        results: list[PriceResult] = []
        for url in deduped_urls:
            if "fail" in url:
                results.append(
                    PriceResult(
                        url=url,
                        success=False,
                        product_id="prd_test_fail",
                        site_key="generic",
                        adapter_key="generic",
                        error="price_not_found",
                    )
                )
                continue

            listing = PriceListing(
                item_id=f"itm_{abs(hash(url))}",
                amount="99.99",
                currency="USD",
                raw_text="$99.99",
                source="test:fake",
                seller="Fake Store",
                listing_url=url,
            )
            results.append(
                PriceResult(
                    url=url,
                    success=True,
                    product_id=f"prd_{abs(hash(url))}",
                    site_key="generic",
                    adapter_key="generic",
                    amount=listing.amount,
                    currency=listing.currency,
                    raw_text=listing.raw_text,
                    source=listing.source,
                    listing_count=1,
                    listings=[listing],
                )
            )

        success_count = sum(1 for item in results if item.success)
        return ScrapeBatchOutput(
            request_id=payload.request_id,
            total_urls=len(deduped_urls),
            success_count=success_count,
            failure_count=len(deduped_urls) - success_count,
            results=results,
        )


@dataclass
class FakeDiscoveryService:
    last_input: DiscoveryBatchInput | None = None

    def list_sites(self) -> dict[str, str]:
        return {
            "akakce": "Akakce adapter",
            "generic": "Generic extractor (fallback for any site)",
            "newegg": "Newegg adapter",
        }

    def discover_and_scrape(self, payload: DiscoveryBatchInput) -> DiscoveryBatchOutput:
        self.last_input = payload

        listing = PriceListing(
            item_id="itm_discover",
            amount="149.90",
            currency="USD",
            raw_text="$149.90",
            source="test:discover",
            seller="Discover Store",
            listing_url="https://example.com/product-1",
        )
        result = PriceResult(
            url="https://example.com/product-1",
            success=True,
            product_id="prd_discover",
            site_key="generic",
            adapter_key="generic",
            amount=listing.amount,
            currency=listing.currency,
            raw_text=listing.raw_text,
            source=listing.source,
            listing_count=1,
            listings=[listing],
        )
        return DiscoveryBatchOutput(
            request_id=payload.request_id,
            seed_count=len(payload.start_urls),
            discovered_url_count=1,
            discovered_urls=["https://example.com/product-1"],
            discovery_stats=DiscoveryStats(
                requests_count=6,
                failed_requests_count=0,
                blocked_requests_count=1,
                offsite_requests_count=2,
                elapsed_seconds=1.25,
                sessions_requests_count={"http": 5, "stealth": 1},
            ),
            total_urls=1,
            success_count=1,
            failure_count=0,
            results=[result],
        )


def _build_client(
    fake_scrape_service: FakeScrapeService | None = None,
    fake_discovery_service: object | None = None,
) -> TestClient:
    if fake_scrape_service is not None:
        app.dependency_overrides[get_scrape_service] = lambda: fake_scrape_service
    if fake_discovery_service is not None:
        app.dependency_overrides[get_discovery_service] = lambda: fake_discovery_service
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_health_endpoint_returns_ok_and_request_id() -> None:
    client = _build_client(fake_scrape_service=FakeScrapeService(), fake_discovery_service=FakeDiscoveryService())
    response = client.get("/health")
    _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("X-Request-ID")


def test_sites_endpoint_returns_supported_sites() -> None:
    client = _build_client(fake_scrape_service=FakeScrapeService())
    response = client.get("/api/v1/sites")
    _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert [item["key"] for item in payload["sites"]] == ["akakce", "generic", "newegg"]


def test_scrapes_endpoint_returns_batch_summary_and_deduped_urls() -> None:
    fake_service = FakeScrapeService()
    client = _build_client(fake_scrape_service=fake_service)
    response = client.post(
        "/api/v1/scrapes",
        json={
            "urls": [
                "https://example.com/product-a",
                "https://example.com/product-a",
                "https://example.com/fail-product",
            ],
            "sites": ["generic"],
            "timeout_seconds": 20,
            "fetch_mode": "http",
        },
    )
    _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["total_urls"] == 2
    assert body["scrape_ref"]
    assert body["success_count"] == 1
    assert body["failure_count"] == 1
    assert len(body["results"]) == 2
    assert body["results"][0]["result_ref"]
    assert body["results"][1]["result_ref"]

    assert fake_service.last_input is not None
    assert fake_service.last_input.urls == [
        "https://example.com/product-a",
        "https://example.com/fail-product",
    ]
    assert fake_service.last_input.selected_sites == {"generic"}
    assert fake_service.last_input.timeout_seconds == 20
    assert fake_service.last_input.fetch_mode == "http"


def test_scrapes_endpoint_rejects_unknown_site_key() -> None:
    client = _build_client(fake_scrape_service=FakeScrapeService())
    response = client.post(
        "/api/v1/scrapes",
        json={
            "urls": ["https://example.com/product-a"],
            "sites": ["unknown_site"],
        },
    )
    _clear_overrides()

    assert response.status_code == 422
    assert "Unsupported site key" in response.json()["detail"]


def test_scrapes_endpoint_uses_auto_fetch_mode_by_default() -> None:
    fake_service = FakeScrapeService()
    client = _build_client(fake_scrape_service=fake_service)
    response = client.post(
        "/api/v1/scrapes",
        json={
            "urls": ["https://example.com/product-a"],
        },
    )
    _clear_overrides()

    assert response.status_code == 200
    assert fake_service.last_input is not None
    assert fake_service.last_input.fetch_mode == "auto"


def test_discover_endpoint_returns_summary_stats_and_results() -> None:
    fake_service = FakeDiscoveryService()
    client = _build_client(fake_discovery_service=fake_service)
    response = client.post(
        "/api/v1/discover",
        json={
            "start_urls": ["https://example.com/category/laptops"],
            "sites": ["generic"],
            "timeout_seconds": 20,
            "fetch_mode": "auto",
            "max_pages": 40,
            "max_depth": 2,
        },
    )
    _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["seed_count"] == 1
    assert body["discovered_url_count"] == 1
    assert body["total_urls"] == 1
    assert body["success_count"] == 1
    assert body["failure_count"] == 0
    assert body["discovery_stats"]["requests_count"] == 6
    assert body["discovery_stats"]["sessions_requests_count"] == {"http": 5, "stealth": 1}
    assert len(body["results"]) == 1

    assert fake_service.last_input is not None
    assert fake_service.last_input.selected_sites == {"generic"}
    assert fake_service.last_input.max_pages == 40
    assert fake_service.last_input.max_depth == 2


def test_discover_endpoint_rejects_unknown_site_key() -> None:
    client = _build_client(fake_discovery_service=FakeDiscoveryService())
    response = client.post(
        "/api/v1/discover",
        json={
            "start_urls": ["https://example.com/category/laptops"],
            "sites": ["unknown_site"],
        },
    )
    _clear_overrides()

    assert response.status_code == 422
    assert "Unsupported site key" in response.json()["detail"]


def test_discover_endpoint_rejects_max_pages_out_of_bounds() -> None:
    client = _build_client(fake_discovery_service=FakeDiscoveryService())
    response = client.post(
        "/api/v1/discover",
        json={
            "start_urls": ["https://example.com/category/laptops"],
            "max_pages": 999,
        },
    )
    _clear_overrides()

    assert response.status_code == 422


def test_discover_endpoint_rejects_max_depth_out_of_bounds() -> None:
    client = _build_client(fake_discovery_service=FakeDiscoveryService())
    response = client.post(
        "/api/v1/discover",
        json={
            "start_urls": ["https://example.com/category/laptops"],
            "max_depth": 99,
        },
    )
    _clear_overrides()

    assert response.status_code == 422


def test_discover_endpoint_rejects_timeout_out_of_bounds() -> None:
    client = _build_client(fake_discovery_service=FakeDiscoveryService())
    response = client.post(
        "/api/v1/discover",
        json={
            "start_urls": ["https://example.com/category/laptops"],
            "timeout_seconds": 1,
        },
    )
    _clear_overrides()

    assert response.status_code == 422
    assert "timeout_seconds must be between" in response.json()["detail"]


def test_discover_endpoint_rejects_too_many_seed_urls() -> None:
    client = _build_client(fake_discovery_service=FakeDiscoveryService())
    urls = [f"https://example.com/seed/{index}" for index in range(60)]
    response = client.post(
        "/api/v1/discover",
        json={"start_urls": urls},
    )
    _clear_overrides()

    assert response.status_code == 422
    assert "At most" in response.json()["detail"]


def test_discover_endpoint_maps_discovery_phase_errors_to_502() -> None:
    class _FailingDiscoveryService:
        def list_sites(self) -> dict[str, str]:
            return {"generic": "Generic extractor (fallback for any site)"}

        def discover_and_scrape(self, payload: DiscoveryBatchInput) -> DiscoveryBatchOutput:  # noqa: ARG002
            raise DiscoveryPhaseError("spider_crashed")

    client = _build_client(fake_discovery_service=_FailingDiscoveryService())
    response = client.post(
        "/api/v1/discover",
        json={"start_urls": ["https://example.com/category/laptops"]},
    )
    _clear_overrides()

    assert response.status_code == 502
    assert response.json()["detail"] == "discovery_failed: spider_crashed"
