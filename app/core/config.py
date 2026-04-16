from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    return int(value)


def _parse_origins(value: str) -> list[str]:
    cleaned = value.strip()
    if not cleaned:
        return ["*"]
    if cleaned == "*":
        return ["*"]
    return [item.strip() for item in cleaned.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    api_prefix: str
    cors_allow_origins: list[str]
    scrape_timeout_default: int
    scrape_timeout_min: int
    scrape_timeout_max: int
    scrape_max_urls: int
    scrape_concurrency: int


@lru_cache
def get_settings() -> Settings:
    settings = Settings(
        api_prefix=os.getenv("API_PREFIX", "/api/v1"),
        cors_allow_origins=_parse_origins(os.getenv("CORS_ALLOW_ORIGINS", "*")),
        scrape_timeout_default=_parse_int_env("SCRAPE_TIMEOUT_DEFAULT", 30),
        scrape_timeout_min=_parse_int_env("SCRAPE_TIMEOUT_MIN", 5),
        scrape_timeout_max=_parse_int_env("SCRAPE_TIMEOUT_MAX", 120),
        scrape_max_urls=_parse_int_env("SCRAPE_MAX_URLS", 50),
        scrape_concurrency=_parse_int_env("SCRAPE_CONCURRENCY", 5),
    )

    if settings.scrape_timeout_min < 1:
        raise RuntimeError("SCRAPE_TIMEOUT_MIN must be >= 1")
    if settings.scrape_timeout_max < settings.scrape_timeout_min:
        raise RuntimeError("SCRAPE_TIMEOUT_MAX must be >= SCRAPE_TIMEOUT_MIN")
    if not settings.scrape_timeout_min <= settings.scrape_timeout_default <= settings.scrape_timeout_max:
        raise RuntimeError("SCRAPE_TIMEOUT_DEFAULT must be within min/max timeout bounds")
    if settings.scrape_max_urls < 1:
        raise RuntimeError("SCRAPE_MAX_URLS must be >= 1")
    if settings.scrape_concurrency < 1:
        raise RuntimeError("SCRAPE_CONCURRENCY must be >= 1")

    return settings

