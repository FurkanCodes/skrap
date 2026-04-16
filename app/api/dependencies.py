from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from app.application.scrape_service import ScrapeBatchService
from app.core.config import Settings, get_settings
from app.infrastructure.scraper_gateway import ScraplingGateway


def get_app_settings() -> Settings:
    return get_settings()


@lru_cache
def _get_scrape_service_cached(scrape_concurrency: int) -> ScrapeBatchService:
    gateway = ScraplingGateway()
    return ScrapeBatchService(gateway=gateway, max_workers=scrape_concurrency)


def get_scrape_service(settings: Settings = Depends(get_app_settings)) -> ScrapeBatchService:
    return _get_scrape_service_cached(settings.scrape_concurrency)
