from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.scraper.scrape_spider import PriceScrapeSpider, scrape_prices_with_spider


@dataclass
class _FakeStats:
    blocked_requests_count: int = 0


@dataclass
class _FakeCrawlResult:
    items: list[Any]
    stats: _FakeStats


def test_scrape_prices_with_spider_returns_failure_for_blocked_missing_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_start(self: PriceScrapeSpider) -> _FakeCrawlResult:  # noqa: ARG001
        return _FakeCrawlResult(items=[], stats=_FakeStats(blocked_requests_count=3))

    monkeypatch.setattr(PriceScrapeSpider, "start", _fake_start)

    results = scrape_prices_with_spider(
        urls=["https://www.teknosa.com/ninja-cremi-7si-1-arada-dondurma-makinesi-p-120451101"],
        timeout_seconds=10,
        selected_sites=None,
        fetch_mode="auto",
        concurrent_requests=1,
    )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].error == "blocked_by_protection"
    assert results[0].adapter_key == "generic"
