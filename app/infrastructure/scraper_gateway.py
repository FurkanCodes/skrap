from __future__ import annotations

from dataclasses import dataclass

from price_scraper import SUPPORTED_SITES, PriceResult, fetch_price


@dataclass(frozen=True)
class ScraplingGateway:
    def fetch_price(self, url: str, timeout: int, selected_sites: set[str] | None = None) -> PriceResult:
        return fetch_price(url=url, timeout=timeout, selected_sites=selected_sites)

    def list_sites(self) -> dict[str, str]:
        return dict(SUPPORTED_SITES)

