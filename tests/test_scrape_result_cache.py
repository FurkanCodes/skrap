from __future__ import annotations

import time

from app.application.scrape_result_cache import ScrapeResultCache
from app.scraper import PriceResult


def test_scrape_result_cache_store_and_get() -> None:
    cache = ScrapeResultCache(ttl_seconds=120, max_batches=10)
    results = [
        PriceResult(url="https://example.com/a", success=True, amount="1.00"),
        PriceResult(url="https://example.com/b", success=False, error="price_not_found"),
    ]

    refs = cache.store(results)
    assert refs.scrape_ref
    assert len(refs.result_refs) == 2

    first = cache.get_result(refs.result_refs[0])
    second = cache.get_result(refs.result_refs[1])
    assert first is not None
    assert second is not None
    assert first.url == "https://example.com/a"
    assert second.url == "https://example.com/b"


def test_scrape_result_cache_expires_entries() -> None:
    cache = ScrapeResultCache(ttl_seconds=60, max_batches=10)
    refs = cache.store([PriceResult(url="https://example.com/a", success=True)])

    # Reach into internal state to simulate elapsed time deterministically.
    batch = cache._batches[refs.scrape_ref]  # noqa: SLF001
    batch.created_at = time.time() - 61

    expired = cache.get_result(refs.result_refs[0])
    assert expired is None
