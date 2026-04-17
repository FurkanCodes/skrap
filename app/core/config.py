from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _load_dotenv_file() -> None:
    """Load repository .env once so local runs work without shell `source`."""
    project_root = Path(__file__).resolve().parents[2]
    dotenv_path = project_root / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)
        return
    load_dotenv(override=False)


_load_dotenv_file()


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
    tracking_database_url: str
    tracking_db_pool_min_size: int
    tracking_db_pool_max_size: int
    tracking_interval_default_minutes: int
    tracking_interval_min_minutes: int
    tracking_interval_max_minutes: int
    tracking_snapshot_default_limit: int
    scheduler_poll_seconds: int
    scheduler_claim_batch_size: int
    scheduler_max_inflight: int
    scheduler_lease_seconds: int
    scheduler_jitter_percent: int


@lru_cache
def get_settings() -> Settings:
    tracking_database_url = (
        _parse_str_env("TRACKING_DATABASE_URL", "")
        or _parse_str_env("SUPABASE_DB_URL", "")
        or _parse_str_env("DATABASE_URL", "")
        or "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    )
    settings = Settings(
        api_prefix=os.getenv("API_PREFIX", "/api/v1"),
        cors_allow_origins=_parse_origins(os.getenv("CORS_ALLOW_ORIGINS", "*")),
        scrape_timeout_default=_parse_int_env("SCRAPE_TIMEOUT_DEFAULT", 30),
        scrape_timeout_min=_parse_int_env("SCRAPE_TIMEOUT_MIN", 5),
        scrape_timeout_max=_parse_int_env("SCRAPE_TIMEOUT_MAX", 120),
        scrape_max_urls=_parse_int_env("SCRAPE_MAX_URLS", 50),
        scrape_concurrency=_parse_int_env("SCRAPE_CONCURRENCY", 5),
        tracking_database_url=tracking_database_url,
        tracking_db_pool_min_size=_parse_int_env("TRACKING_DB_POOL_MIN_SIZE", 1),
        tracking_db_pool_max_size=_parse_int_env("TRACKING_DB_POOL_MAX_SIZE", 10),
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
        scheduler_poll_seconds=_parse_int_env("SCHEDULER_POLL_SECONDS", 10),
        scheduler_claim_batch_size=_parse_int_env("SCHEDULER_CLAIM_BATCH_SIZE", 250),
        scheduler_max_inflight=_parse_int_env("SCHEDULER_MAX_INFLIGHT", 64),
        scheduler_lease_seconds=_parse_int_env("SCHEDULER_LEASE_SECONDS", 180),
        scheduler_jitter_percent=_parse_int_env("SCHEDULER_JITTER_PERCENT", 10),
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
    if settings.tracking_db_pool_min_size < 1:
        raise RuntimeError("TRACKING_DB_POOL_MIN_SIZE must be >= 1")
    if settings.tracking_db_pool_max_size < settings.tracking_db_pool_min_size:
        raise RuntimeError(
            "TRACKING_DB_POOL_MAX_SIZE must be >= TRACKING_DB_POOL_MIN_SIZE"
        )
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
    if settings.scheduler_poll_seconds < 1:
        raise RuntimeError("SCHEDULER_POLL_SECONDS must be >= 1")
    if settings.scheduler_claim_batch_size < 1:
        raise RuntimeError("SCHEDULER_CLAIM_BATCH_SIZE must be >= 1")
    if settings.scheduler_max_inflight < 1:
        raise RuntimeError("SCHEDULER_MAX_INFLIGHT must be >= 1")
    if settings.scheduler_lease_seconds < 1:
        raise RuntimeError("SCHEDULER_LEASE_SECONDS must be >= 1")
    if settings.scheduler_jitter_percent < 0 or settings.scheduler_jitter_percent > 50:
        raise RuntimeError("SCHEDULER_JITTER_PERCENT must be between 0 and 50")

    return settings
