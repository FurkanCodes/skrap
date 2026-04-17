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


def _parse_str_env(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    return value


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
    tracking_db_path: str
    tracking_interval_default_minutes: int
    tracking_interval_min_minutes: int
    tracking_interval_max_minutes: int
    tracking_snapshot_default_limit: int


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
        tracking_db_path=_parse_str_env("TRACKING_DB_PATH", "./data/tracking.sqlite3"),
        tracking_interval_default_minutes=_parse_int_env(
            "TRACKING_INTERVAL_DEFAULT_MINUTES", 60
        ),
        tracking_interval_min_minutes=_parse_int_env("TRACKING_INTERVAL_MIN_MINUTES", 5),
        tracking_interval_max_minutes=_parse_int_env(
            "TRACKING_INTERVAL_MAX_MINUTES", 10080
        ),
        tracking_snapshot_default_limit=_parse_int_env(
            "TRACKING_SNAPSHOT_DEFAULT_LIMIT", 20
        ),
    )

    if settings.scrape_timeout_min < 1:
        raise RuntimeError("SCRAPE_TIMEOUT_MIN must be >= 1")
    if settings.scrape_timeout_max < settings.scrape_timeout_min:
        raise RuntimeError("SCRAPE_TIMEOUT_MAX must be >= SCRAPE_TIMEOUT_MIN")
    if (
        not settings.scrape_timeout_min
        <= settings.scrape_timeout_default
        <= settings.scrape_timeout_max
    ):
        raise RuntimeError(
            "SCRAPE_TIMEOUT_DEFAULT must be within min/max timeout bounds"
        )
    if settings.scrape_max_urls < 1:
        raise RuntimeError("SCRAPE_MAX_URLS must be >= 1")
    if settings.scrape_concurrency < 1:
        raise RuntimeError("SCRAPE_CONCURRENCY must be >= 1")
    if settings.tracking_interval_min_minutes < 1:
        raise RuntimeError("TRACKING_INTERVAL_MIN_MINUTES must be >= 1")
    if settings.tracking_interval_max_minutes < settings.tracking_interval_min_minutes:
        raise RuntimeError(
            "TRACKING_INTERVAL_MAX_MINUTES must be >= TRACKING_INTERVAL_MIN_MINUTES"
        )
    if (
        not settings.tracking_interval_min_minutes
        <= settings.tracking_interval_default_minutes
        <= settings.tracking_interval_max_minutes
    ):
        raise RuntimeError(
            "TRACKING_INTERVAL_DEFAULT_MINUTES must be within min/max interval bounds"
        )
    if settings.tracking_snapshot_default_limit < 1:
        raise RuntimeError("TRACKING_SNAPSHOT_DEFAULT_LIMIT must be >= 1")

    return settings
