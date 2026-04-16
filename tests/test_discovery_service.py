from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.application.discovery_service import (
    DiscoveryBatchInput,
    DiscoveryBatchService,
    DiscoveryPhaseError,
)
from app.application.scrape_service import ScrapeBatchOutput, ScrapeBatchService
from app.scraper import PriceResult
from app.scraper.discovery import DiscoveryRunResult, DiscoveryStats


@dataclass
class _FakeGateway:
    fetch_calls: list[str]

    def fetch_price(
        self,
        url: str,
        timeout: int,
        selected_sites: set[str] | None = None,
        fetch_mode: str = "auto",
    ) -> PriceResult:
        self.fetch_calls.append(url)
        return PriceResult(
            url=url,
            success=True,
            product_id=f"prd_{len(self.fetch_calls)}",
            site_key="generic",
            adapter_key="generic",
            amount="10.00",
            currency="USD",
            raw_text="$10.00",
            source="test:gateway",
            listing_count=0,
            listings=[],
        )

    def list_sites(self) -> dict[str, str]:
        return {"generic": "Generic extractor"}


@dataclass
class _ExplodingScrapeService:
    def list_sites(self) -> dict[str, str]:
        return {"generic": "Generic extractor"}

    def scrape(self, payload) -> ScrapeBatchOutput:
        raise RuntimeError("scrape exploded")


def _payload() -> DiscoveryBatchInput:
    return DiscoveryBatchInput(
        request_id="req_123",
        start_urls=["https://example.com/category/laptops"],
        timeout_seconds=20,
        selected_sites=None,
        fetch_mode="auto",
        max_pages=50,
        max_depth=2,
    )


def _stats() -> DiscoveryStats:
    return DiscoveryStats(
        requests_count=3,
        failed_requests_count=0,
        blocked_requests_count=0,
        offsite_requests_count=1,
        elapsed_seconds=0.42,
        sessions_requests_count={"http": 3},
    )


def test_two_phase_flow_preserves_discovery_order_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway(fetch_calls=[])
    scrape_service = ScrapeBatchService(gateway=gateway, max_workers=1)
    service = DiscoveryBatchService(scrape_service=scrape_service)

    monkeypatch.setattr(
        "app.application.discovery_service.discover_urls",
        lambda **_: DiscoveryRunResult(
            discovered_urls=[
                "https://example.com/product/1",
                "https://example.com/product/1",
                "https://example.com/product/2",
            ],
            stats=_stats(),
        ),
    )

    output = service.discover_and_scrape(_payload())

    assert output.discovered_url_count == 3
    assert output.total_urls == 2
    assert [item.url for item in output.results] == [
        "https://example.com/product/1",
        "https://example.com/product/2",
    ]
    assert gateway.fetch_calls == [
        "https://example.com/product/1",
        "https://example.com/product/2",
    ]


def test_empty_discovery_skips_scrape_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway(fetch_calls=[])
    scrape_service = ScrapeBatchService(gateway=gateway, max_workers=1)
    service = DiscoveryBatchService(scrape_service=scrape_service)

    monkeypatch.setattr(
        "app.application.discovery_service.discover_urls",
        lambda **_: DiscoveryRunResult(discovered_urls=[], stats=_stats()),
    )

    output = service.discover_and_scrape(_payload())

    assert output.discovered_url_count == 0
    assert output.total_urls == 0
    assert output.results == []
    assert gateway.fetch_calls == []


def test_discovery_phase_failures_raise_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway(fetch_calls=[])
    scrape_service = ScrapeBatchService(gateway=gateway, max_workers=1)
    service = DiscoveryBatchService(scrape_service=scrape_service)

    def _raise_error(**kwargs):  # noqa: ARG001
        raise RuntimeError("spider failed")

    monkeypatch.setattr("app.application.discovery_service.discover_urls", _raise_error)

    with pytest.raises(DiscoveryPhaseError, match="spider failed"):
        service.discover_and_scrape(_payload())


def test_scrape_phase_failures_return_partial_failure_results(monkeypatch: pytest.MonkeyPatch) -> None:
    service = DiscoveryBatchService(scrape_service=_ExplodingScrapeService())

    monkeypatch.setattr(
        "app.application.discovery_service.discover_urls",
        lambda **_: DiscoveryRunResult(
            discovered_urls=[
                "https://example.com/product/1",
                "https://example.com/product/2",
            ],
            stats=_stats(),
        ),
    )

    output = service.discover_and_scrape(_payload())

    assert output.total_urls == 2
    assert output.success_count == 0
    assert output.failure_count == 2
    assert [item.error for item in output.results] == [
        "scrape_failed: scrape exploded",
        "scrape_failed: scrape exploded",
    ]
