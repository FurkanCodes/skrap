from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.api.dependencies import get_scrape_service
from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchOutput
from app.main import app
from price_scraper import PriceListing, PriceResult


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


def _build_client(fake_service: FakeScrapeService) -> TestClient:
    app.dependency_overrides[get_scrape_service] = lambda: fake_service
    return TestClient(app)


def test_health_endpoint_returns_ok_and_request_id() -> None:
    client = _build_client(FakeScrapeService())
    response = client.get("/health")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("X-Request-ID")


def test_sites_endpoint_returns_supported_sites() -> None:
    client = _build_client(FakeScrapeService())
    response = client.get("/api/v1/sites")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert [item["key"] for item in payload["sites"]] == ["akakce", "generic", "newegg"]


def test_scrapes_endpoint_returns_batch_summary_and_deduped_urls() -> None:
    fake_service = FakeScrapeService()
    client = _build_client(fake_service)
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
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["total_urls"] == 2
    assert body["success_count"] == 1
    assert body["failure_count"] == 1
    assert len(body["results"]) == 2

    assert fake_service.last_input is not None
    assert fake_service.last_input.urls == [
        "https://example.com/product-a",
        "https://example.com/fail-product",
    ]
    assert fake_service.last_input.selected_sites == {"generic"}
    assert fake_service.last_input.timeout_seconds == 20
    assert fake_service.last_input.fetch_mode == "http"


def test_scrapes_endpoint_rejects_unknown_site_key() -> None:
    client = _build_client(FakeScrapeService())
    response = client.post(
        "/api/v1/scrapes",
        json={
            "urls": ["https://example.com/product-a"],
            "sites": ["unknown_site"],
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "Unsupported site key" in response.json()["detail"]


def test_scrapes_endpoint_uses_auto_fetch_mode_by_default() -> None:
    fake_service = FakeScrapeService()
    client = _build_client(fake_service)
    response = client.post(
        "/api/v1/scrapes",
        json={
            "urls": ["https://example.com/product-a"],
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert fake_service.last_input is not None
    assert fake_service.last_input.fetch_mode == "auto"
