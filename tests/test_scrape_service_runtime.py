from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchService
from app.scraper import PriceResult


@dataclass
class _FakeGateway:
    calls: list[str]

    def list_sites(self) -> dict[str, str]:
        return {"generic": "Generic extractor"}

    def fetch_price(
        self,
        url: str,
        timeout: int,
        selected_sites: set[str] | None = None,
        fetch_mode: str = "auto",
    ) -> PriceResult:
        self.calls.append(url)
        return PriceResult(url=url, success=True, site_key="generic", adapter_key="generic")


def test_scrape_service_uses_spider_runtime_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway(calls=[])
    service = ScrapeBatchService(gateway=gateway, max_workers=4, use_spider_runtime=True)
    payload = ScrapeBatchInput(
        request_id="req_1",
        urls=["https://example.com/a", "https://example.com/a", "https://example.com/b"],
        timeout_seconds=15,
        selected_sites={"generic"},
        fetch_mode="auto",
    )
    received: dict[str, object] = {}

    def _fake_spider_runtime(**kwargs) -> list[PriceResult]:
        received.update(kwargs)
        return [
            PriceResult(url="https://example.com/a", success=True, site_key="generic", adapter_key="generic"),
            PriceResult(url="https://example.com/b", success=False, site_key="generic", adapter_key="generic", error="price_not_found"),
        ]

    monkeypatch.setattr("app.application.scrape_service.scrape_prices_with_spider", _fake_spider_runtime)

    output = service.scrape(payload)

    assert received["urls"] == ["https://example.com/a", "https://example.com/b"]
    assert received["timeout_seconds"] == 15
    assert received["selected_sites"] == {"generic"}
    assert received["fetch_mode"] == "auto"
    assert output.total_urls == 2
    assert output.success_count == 1
    assert output.failure_count == 1
    assert [result.url for result in output.results] == ["https://example.com/a", "https://example.com/b"]
    assert gateway.calls == []


def test_scrape_service_falls_back_when_spider_runtime_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _FakeGateway(calls=[])
    service = ScrapeBatchService(gateway=gateway, max_workers=1, use_spider_runtime=True)
    payload = ScrapeBatchInput(
        request_id="req_2",
        urls=["https://example.com/a"],
        timeout_seconds=15,
        selected_sites=None,
        fetch_mode="http",
    )

    def _raise(**kwargs):  # noqa: ARG001
        raise RuntimeError("spider crashed")

    monkeypatch.setattr("app.application.scrape_service.scrape_prices_with_spider", _raise)

    output = service.scrape(payload)

    assert output.total_urls == 1
    assert output.success_count == 1
    assert output.failure_count == 0
    assert gateway.calls == ["https://example.com/a"]
