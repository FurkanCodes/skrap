from __future__ import annotations

"""Two-phase discovery -> scrape orchestration service."""

from dataclasses import dataclass
import logging
import time

from app.scraper import PriceResult
from app.scraper.discovery import DiscoveryStats, discover_urls

from .scrape_service import ScrapeBatchInput, ScrapeBatchOutput, ScrapeBatchService


class DiscoveryServiceError(RuntimeError):
    """Base error for discovery orchestration failures."""


class DiscoveryPhaseError(DiscoveryServiceError):
    """Raised when spider discovery cannot produce a usable run result."""


@dataclass(frozen=True)
class DiscoveryBatchInput:
    request_id: str
    start_urls: list[str]
    timeout_seconds: int
    selected_sites: set[str] | None
    fetch_mode: str
    max_pages: int
    max_depth: int


@dataclass(frozen=True)
class DiscoveryBatchOutput:
    request_id: str
    seed_count: int
    discovered_url_count: int
    discovered_urls: list[str]
    discovery_stats: DiscoveryStats
    total_urls: int
    success_count: int
    failure_count: int
    results: list[PriceResult]


class DiscoveryBatchService:
    """Runs URL discovery first, then reuses scrape pipeline for normalization."""

    def __init__(self, scrape_service: ScrapeBatchService) -> None:
        self._scrape_service = scrape_service
        self._logger = logging.getLogger("scraper_api.discovery")

    def list_sites(self) -> dict[str, str]:
        return self._scrape_service.list_sites()

    def discover_and_scrape(self, payload: DiscoveryBatchInput) -> DiscoveryBatchOutput:
        discovery_started = time.perf_counter()
        try:
            discovery = discover_urls(
                start_urls=payload.start_urls,
                max_pages=payload.max_pages,
                max_depth=payload.max_depth,
                timeout_seconds=payload.timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - defensive guard around spider runtime
            discovery_ms = (time.perf_counter() - discovery_started) * 1000
            self._logger.exception(
                "discovery_phase_failed request_id=%s seed_count=%s duration_ms=%.2f error=%s",
                payload.request_id,
                len(payload.start_urls),
                discovery_ms,
                exc,
            )
            raise DiscoveryPhaseError(str(exc)) from exc

        discovery_ms = (time.perf_counter() - discovery_started) * 1000
        self._logger.info(
            "discovery_phase_complete request_id=%s seed_count=%s discovered_url_count=%s duration_ms=%.2f",
            payload.request_id,
            len(payload.start_urls),
            len(discovery.discovered_urls),
            discovery_ms,
        )

        if not discovery.discovered_urls:
            return DiscoveryBatchOutput(
                request_id=payload.request_id,
                seed_count=len(payload.start_urls),
                discovered_url_count=0,
                discovered_urls=[],
                discovery_stats=discovery.stats,
                total_urls=0,
                success_count=0,
                failure_count=0,
                results=[],
            )

        scrape_payload = ScrapeBatchInput(
            request_id=payload.request_id,
            urls=discovery.discovered_urls,
            timeout_seconds=payload.timeout_seconds,
            selected_sites=payload.selected_sites,
            fetch_mode=payload.fetch_mode,
        )
        scrape_started = time.perf_counter()
        try:
            scraped: ScrapeBatchOutput = self._scrape_service.scrape(scrape_payload)
        except Exception as exc:  # pragma: no cover - defensive guard
            scrape_ms = (time.perf_counter() - scrape_started) * 1000
            self._logger.exception(
                "scrape_phase_failed request_id=%s discovered_url_count=%s duration_ms=%.2f error=%s",
                payload.request_id,
                len(discovery.discovered_urls),
                scrape_ms,
                exc,
            )
            fallback_results = [
                PriceResult(url=url, success=False, error=f"scrape_failed: {exc}")
                for url in discovery.discovered_urls
            ]
            return DiscoveryBatchOutput(
                request_id=payload.request_id,
                seed_count=len(payload.start_urls),
                discovered_url_count=len(discovery.discovered_urls),
                discovered_urls=discovery.discovered_urls,
                discovery_stats=discovery.stats,
                total_urls=len(fallback_results),
                success_count=0,
                failure_count=len(fallback_results),
                results=fallback_results,
            )

        scrape_ms = (time.perf_counter() - scrape_started) * 1000
        self._logger.info(
            "scrape_phase_complete request_id=%s discovered_url_count=%s total_urls=%s success_count=%s failure_count=%s duration_ms=%.2f",
            payload.request_id,
            len(discovery.discovered_urls),
            scraped.total_urls,
            scraped.success_count,
            scraped.failure_count,
            scrape_ms,
        )

        return DiscoveryBatchOutput(
            request_id=payload.request_id,
            seed_count=len(payload.start_urls),
            discovered_url_count=len(discovery.discovered_urls),
            discovered_urls=discovery.discovered_urls,
            discovery_stats=discovery.stats,
            total_urls=scraped.total_urls,
            success_count=scraped.success_count,
            failure_count=scraped.failure_count,
            results=scraped.results,
        )
