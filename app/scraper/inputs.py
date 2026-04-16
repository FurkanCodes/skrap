from __future__ import annotations

"""Input parsing/validation helpers for scraper-facing strings."""

import re

from .constants import SUPPORTED_FETCH_MODES
from .registry import list_supported_sites


def parse_urls_blob(blob: str) -> list[str]:
    """Extract and deduplicate HTTP(S) URLs from free-form text input."""
    urls: list[str] = []
    for match in re.finditer(r"https?://\S+", blob):
        token = match.group(0).strip()
        for candidate in re.split(r"[;,](?=https?://)", token):
            # Preserve commas inside URL paths but trim common trailing punctuation.
            cleaned = candidate.rstrip('"\\\')]}>,;:,')
            if cleaned.startswith(("http://", "https://")):
                urls.append(cleaned)
    return list(dict.fromkeys(urls))


def parse_sites_blob(blob: str) -> set[str]:
    """Parse comma/space separated site keys with `all/*` support."""
    tokens = [token.strip().lower() for token in re.split(r"[\s,;]+", blob) if token.strip()]
    if not tokens:
        return set()

    supported_sites = set(list_supported_sites())

    selected: set[str] = set()
    for token in tokens:
        if token in {"all", "*"}:
            return set()
        if token not in supported_sites:
            supported = ", ".join(sorted(supported_sites))
            raise ValueError(f"Unsupported site key '{token}'. Supported keys: {supported}")
        selected.add(token)
    return selected


def parse_fetch_mode(value: str) -> str:
    """Validate fetch mode key against supported configured modes."""
    mode = value.strip().lower()
    if mode not in SUPPORTED_FETCH_MODES:
        supported = ", ".join(sorted(SUPPORTED_FETCH_MODES))
        raise ValueError(f"Unsupported fetch mode '{value}'. Supported modes: {supported}")
    return mode
