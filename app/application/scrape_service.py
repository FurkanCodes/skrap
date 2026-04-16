from __future__ import annotations

"""Application service layer for batch scrape requests.

This layer is intentionally thin: it owns batching/concurrency and delegates
single-URL work to the scraper gateway implementation.
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol

from app.scraper import PriceResult
from app.scraper.scrape_spider import scrape_prices_with_spider


class ScraperGateway(Protocol):
    """Boundary between application layer and scraper implementation."""

    def fetch_price(
        self,
        url: str,
        timeout: int,
        selected_sites: set[str] | None = None,
        fetch_mode: str = "auto",
    ) -> PriceResult:
        ...

    def list_sites(self) -> dict[str, str]:
        ...


@dataclass(frozen=True)
class ScrapeBatchInput:
    """Validated input payload for batch scraping operations."""

    request_id: str
    urls: list[str]
    timeout_seconds: int
    selected_sites: set[str] | None
    fetch_mode: str = "auto"


@dataclass(frozen=True)
class ScrapeBatchOutput:
    """Application-level output returned to API layer."""

    request_id: str
    total_urls: int
    success_count: int
    failure_count: int
    results: list[PriceResult]


def _dedupe_preserving_order(urls: list[str]) -> list[str]:
    """Remove duplicate URLs while preserving caller-provided order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


class ScrapeBatchService:
    """Coordinates batch execution and deterministic result ordering."""

    def __init__(
        self,
        gateway: ScraperGateway,
        max_workers: int,
        use_spider_runtime: bool = False,
    ) -> None:
        self._gateway = gateway
        self._max_workers = max_workers
        self._use_spider_runtime = use_spider_runtime
        self._logger = logging.getLogger("scraper_api.scrape")

    def list_sites(self) -> dict[str, str]:
        return self._gateway.list_sites()

    def scrape(self, payload: ScrapeBatchInput) -> ScrapeBatchOutput:
        """Execute batch scraping with optional threadpool parallelism."""
        urls = _dedupe_preserving_order(payload.urls)
        if not urls:
            return ScrapeBatchOutput(
                request_id=payload.request_id,
                total_urls=0,
                success_count=0,
                failure_count=0,
                results=[],
            )

        if self._use_spider_runtime:
            try:
                spider_results = scrape_prices_with_spider(
                    urls=urls,
                    timeout_seconds=payload.timeout_seconds,
                    selected_sites=payload.selected_sites,
                    fetch_mode=payload.fetch_mode,
                    concurrent_requests=self._max_workers,
                )
                by_url = {item.url: item for item in spider_results}
                ordered_results = [
                    by_url.get(url)
                    or PriceResult(
                        url=url,
                        success=False,
                        error="internal_error: missing_spider_result",
                    )
                    for url in urls
                ]
                return self._build_output(payload.request_id, urls, ordered_results)
            except Exception as exc:  # pragma: no cover - defensive fallback
                self._logger.exception("spider_runtime_failed fallback=threadpool error=%s", exc)

        if self._max_workers == 1 or len(urls) == 1:
            # Fast path: avoid threadpool overhead when parallelism has no value.
            results = [
                self._run_single(url, payload.timeout_seconds, payload.selected_sites, payload.fetch_mode)
                for url in urls
            ]
            return self._build_output(payload.request_id, urls, results)

        results_by_index: list[PriceResult | None] = [None] * len(urls)
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_map: dict[Future[PriceResult], tuple[int, str]] = {}
            for index, url in enumerate(urls):
                future = executor.submit(
                    self._run_single,
                    url,
                    payload.timeout_seconds,
                    payload.selected_sites,
                    payload.fetch_mode,
                )
                future_map[future] = (index, url)

            for future in as_completed(future_map):
                index, url = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    result = PriceResult(
                        url=url,
                        success=False,
                        error=f"internal_error: {exc}",
                    )
                results_by_index[index] = result

        # Preserve input ordering even when futures complete out-of-order.
        ordered_results = [result for result in results_by_index if result is not None]
        return self._build_output(payload.request_id, urls, ordered_results)

    def _run_single(
        self,
        url: str,
        timeout_seconds: int,
        selected_sites: set[str] | None,
        fetch_mode: str,
    ) -> PriceResult:
        return self._gateway.fetch_price(
            url,
            timeout=timeout_seconds,
            selected_sites=selected_sites,
            fetch_mode=fetch_mode,
        )

    @staticmethod
    def _build_output(request_id: str, urls: list[str], results: list[PriceResult]) -> ScrapeBatchOutput:
        """Build response summary fields from individual URL results."""
        success_count = sum(1 for item in results if item.success)
        return ScrapeBatchOutput(
            request_id=request_id,
            total_urls=len(urls),
            success_count=success_count,
            failure_count=len(results) - success_count,
            results=results,
        )
