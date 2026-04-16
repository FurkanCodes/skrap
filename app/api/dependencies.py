from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from app.application.discovery_service import DiscoveryBatchService
from app.application.scrape_service import ScrapeBatchService
from app.core.config import Settings, get_settings
from app.infrastructure.scraper_gateway import ScraplingGateway


def get_app_settings() -> Settings:
    return get_settings()


@lru_cache
def _get_scrape_service_cached(scrape_concurrency: int) -> ScrapeBatchService:
    gateway = ScraplingGateway()
    return ScrapeBatchService(
        gateway=gateway,
        max_workers=scrape_concurrency,
        use_spider_runtime=True,
    )


def get_scrape_service(settings: Settings = Depends(get_app_settings)) -> ScrapeBatchService:
    return _get_scrape_service_cached(settings.scrape_concurrency)


@lru_cache
def _get_discovery_service_cached(scrape_concurrency: int) -> DiscoveryBatchService:
    scrape_service = _get_scrape_service_cached(scrape_concurrency)
    return DiscoveryBatchService(scrape_service=scrape_service)


def get_discovery_service(settings: Settings = Depends(get_app_settings)) -> DiscoveryBatchService:
    return _get_discovery_service_cached(settings.scrape_concurrency)
