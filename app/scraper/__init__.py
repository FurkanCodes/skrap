from .domain import FetchMode, PriceCandidate, PriceListing, PriceResult
from .extractors.generic import extract_price_from_selector, extract_price_listings_from_selector
from .inputs import parse_fetch_mode, parse_sites_blob, parse_urls_blob
from .registry import detect_site_key, list_supported_sites
from .service import (
    REQUIRED_RESULT_FIELDS,
    SUPPORTED_FETCH_MODES,
    fetch_price,
    is_result_qualified,
    missing_required_result_fields,
    scrape_prices,
)

__all__ = [
    "FetchMode",
    "PriceCandidate",
    "PriceListing",
    "PriceResult",
    "REQUIRED_RESULT_FIELDS",
    "SUPPORTED_FETCH_MODES",
    "detect_site_key",
    "extract_price_from_selector",
    "extract_price_listings_from_selector",
    "fetch_price",
    "is_result_qualified",
    "list_supported_sites",
    "missing_required_result_fields",
    "parse_fetch_mode",
    "parse_sites_blob",
    "parse_urls_blob",
    "scrape_prices",
]
