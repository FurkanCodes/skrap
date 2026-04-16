from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Protocol

from price_scraper import PriceResult


class ScraperGateway(Protocol):
    def fetch_price(self, url: str, timeout: int, selected_sites: set[str] | None = None) -> PriceResult:
        ...

    def list_sites(self) -> dict[str, str]:
        ...


@dataclass(frozen=True)
class ScrapeBatchInput:
    request_id: str
    urls: list[str]
    timeout_seconds: int
    selected_sites: set[str] | None


@dataclass(frozen=True)
class ScrapeBatchOutput:
    request_id: str
    total_urls: int
    success_count: int
    failure_count: int
    results: list[PriceResult]


def _dedupe_preserving_order(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


class ScrapeBatchService:
    def __init__(self, gateway: ScraperGateway, max_workers: int) -> None:
        self._gateway = gateway
        self._max_workers = max_workers

    def list_sites(self) -> dict[str, str]:
        return self._gateway.list_sites()

    def scrape(self, payload: ScrapeBatchInput) -> ScrapeBatchOutput:
        urls = _dedupe_preserving_order(payload.urls)
        if not urls:
            return ScrapeBatchOutput(
                request_id=payload.request_id,
                total_urls=0,
                success_count=0,
                failure_count=0,
                results=[],
            )

        if self._max_workers == 1 or len(urls) == 1:
            results = [self._run_single(url, payload.timeout_seconds, payload.selected_sites) for url in urls]
            return self._build_output(payload.request_id, urls, results)

        results_by_index: list[PriceResult | None] = [None] * len(urls)
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_map: dict[Future[PriceResult], tuple[int, str]] = {}
            for index, url in enumerate(urls):
                future = executor.submit(self._run_single, url, payload.timeout_seconds, payload.selected_sites)
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

        ordered_results = [result for result in results_by_index if result is not None]
        return self._build_output(payload.request_id, urls, ordered_results)

    def _run_single(self, url: str, timeout_seconds: int, selected_sites: set[str] | None) -> PriceResult:
        return self._gateway.fetch_price(url, timeout=timeout_seconds, selected_sites=selected_sites)

    @staticmethod
    def _build_output(request_id: str, urls: list[str], results: list[PriceResult]) -> ScrapeBatchOutput:
        success_count = sum(1 for item in results if item.success)
        return ScrapeBatchOutput(
            request_id=request_id,
            total_urls=len(urls),
            success_count=success_count,
            failure_count=len(results) - success_count,
            results=results,
        )

