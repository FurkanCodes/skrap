from __future__ import annotations

from dataclasses import dataclass

from app.scraper import PriceResult, fetch_price, list_supported_sites


@dataclass(frozen=True)
class ScraplingGateway:
    def fetch_price(
        self,
        url: str,
        timeout: int,
        selected_sites: set[str] | None = None,
        fetch_mode: str = "auto",
    ) -> PriceResult:
        return fetch_price(
            url=url,
            timeout=timeout,
            selected_sites=selected_sites,
            fetch_mode=fetch_mode,
        )

    def list_sites(self) -> dict[str, str]:
        return list_supported_sites()
