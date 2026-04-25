from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

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


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def _parse_origins(value: str) -> list[str]:
    cleaned = value.strip()
    if not cleaned:
        return ["*"]
    if cleaned == "*":
        return ["*"]
    return [item.strip() for item in cleaned.split(",") if item.strip()]


def _parse_csv(value: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = value.strip()
    if not cleaned:
        return default
    parsed = tuple(item.strip() for item in cleaned.split(",") if item.strip())
    return parsed or default


def _parse_int_csv(value: str, *, default: tuple[int, ...]) -> tuple[int, ...]:
    cleaned = value.strip()
    if not cleaned:
        return default
    parsed = tuple(int(item.strip()) for item in cleaned.split(",") if item.strip())
    return parsed or default


@dataclass(frozen=True)
class Settings:
    environment: str
    api_prefix: str
    cors_allow_origins: list[str]
    scrape_timeout_default: int
    scrape_timeout_min: int
    scrape_timeout_max: int
    scrape_max_urls: int
    scrape_concurrency: int
    scrape_result_cache_ttl_seconds: int
    scrape_result_cache_max_batches: int
    scrape_proxy_urls: tuple[str, ...]
    scrape_proxy_list_urls: tuple[str, ...]
    server_fetch_allowed_ports: tuple[int, ...]
    server_fetch_allow_private_networks: bool
    server_fetch_resolve_dns: bool
    rate_limit_enabled: bool
    rate_limit_redis_url: str
    rate_limit_window_seconds: int
    rate_limit_default_requests: int
    rate_limit_scrape_requests: int
    rate_limit_discover_requests: int
    rate_limit_fail_open: bool
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
    scheduler_align_checks_to_interval: bool
    scheduler_alert_cooldown_minutes: int
    scheduler_queue_redis_url: str
    scheduler_queue_stream_key: str
    scheduler_queue_consumer_group: str
    scheduler_queue_max_stream_length: int
    scheduler_worker_claim_batch_size: int
    scheduler_worker_block_seconds: int
    scheduler_worker_pending_idle_seconds: int
    notifier_poll_seconds: int
    notifier_claim_batch_size: int
    notifier_lease_seconds: int
    notifier_max_attempts: int
    notifier_base_backoff_seconds: int
    notifier_backoff_max_seconds: int
    notifier_retry_jitter_percent: int
    notifier_max_inflight: int
    telegram_sender_min_interval_ms: int
    telegram_bot_token: str
    telegram_bot_username: str
    telegram_webhook_secret: str
    telegram_connect_ttl_minutes: int
    affiliate_link_template: str
    auth_required: bool
    better_auth_jwks_url: str
    better_auth_jwt_issuer: str
    better_auth_jwt_audience: str
    better_auth_jwt_algorithms: tuple[str, ...]
    better_auth_jwks_cache_ttl_seconds: int
    better_auth_jwks_timeout_seconds: float


@lru_cache
def get_settings() -> Settings:
    environment = _parse_str_env("ENVIRONMENT", "development").lower()
    tracking_database_url = (
        _parse_str_env("TRACKING_DATABASE_URL", "")
        or _parse_str_env("SUPABASE_DB_URL", "")
        or _parse_str_env("DATABASE_URL", "")
        or "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    )
    scheduler_queue_redis_url = (
        _parse_str_env("SCHEDULER_QUEUE_REDIS_URL", "")
        or _parse_str_env("REDIS_URL", "")
        or "redis://127.0.0.1:6379/0"
    )
    settings = Settings(
        environment=environment,
        api_prefix=os.getenv("API_PREFIX", "/api/v1"),
        cors_allow_origins=_parse_origins(os.getenv("CORS_ALLOW_ORIGINS", "*")),
        scrape_timeout_default=_parse_int_env("SCRAPE_TIMEOUT_DEFAULT", 30),
        scrape_timeout_min=_parse_int_env("SCRAPE_TIMEOUT_MIN", 5),
        scrape_timeout_max=_parse_int_env("SCRAPE_TIMEOUT_MAX", 120),
        scrape_max_urls=_parse_int_env("SCRAPE_MAX_URLS", 50),
        scrape_concurrency=_parse_int_env("SCRAPE_CONCURRENCY", 5),
        scrape_result_cache_ttl_seconds=_parse_int_env("SCRAPE_RESULT_CACHE_TTL_SECONDS", 900),
        scrape_result_cache_max_batches=_parse_int_env("SCRAPE_RESULT_CACHE_MAX_BATCHES", 500),
        scrape_proxy_urls=_parse_csv(
            _parse_str_env("SCRAPE_PROXY_URLS", "")
            or _parse_str_env("SCRAPER_PROXY_URLS", "")
            or _parse_str_env("SCRAPE_PROXY_URL", ""),
            default=(),
        ),
        scrape_proxy_list_urls=_parse_csv(
            _parse_str_env("SCRAPE_PROXY_LIST_URLS", "")
            or _parse_str_env("SCRAPE_PROXY_LIST_URL", ""),
            default=(),
        ),
        server_fetch_allowed_ports=_parse_int_csv(
            _parse_str_env("SERVER_FETCH_ALLOWED_PORTS", ""),
            default=(80, 443),
        ),
        server_fetch_allow_private_networks=_parse_bool_env(
            "SERVER_FETCH_ALLOW_PRIVATE_NETWORKS",
            False,
        ),
        server_fetch_resolve_dns=_parse_bool_env(
            "SERVER_FETCH_RESOLVE_DNS",
            environment == "production",
        ),
        rate_limit_enabled=_parse_bool_env(
            "RATE_LIMIT_ENABLED",
            environment == "production",
        ),
        rate_limit_redis_url=(
            _parse_str_env("RATE_LIMIT_REDIS_URL", "")
            or scheduler_queue_redis_url
        ),
        rate_limit_window_seconds=_parse_int_env("RATE_LIMIT_WINDOW_SECONDS", 60),
        rate_limit_default_requests=_parse_int_env("RATE_LIMIT_DEFAULT_REQUESTS", 120),
        rate_limit_scrape_requests=_parse_int_env("RATE_LIMIT_SCRAPE_REQUESTS", 20),
        rate_limit_discover_requests=_parse_int_env("RATE_LIMIT_DISCOVER_REQUESTS", 10),
        rate_limit_fail_open=_parse_bool_env(
            "RATE_LIMIT_FAIL_OPEN",
            environment != "production",
        ),
        tracking_database_url=tracking_database_url,
        tracking_db_pool_min_size=_parse_int_env("TRACKING_DB_POOL_MIN_SIZE", 1),
        tracking_db_pool_max_size=_parse_int_env("TRACKING_DB_POOL_MAX_SIZE", 10),
        tracking_interval_default_minutes=_parse_int_env(
            "TRACKING_INTERVAL_DEFAULT_MINUTES", 60
        ),
        tracking_interval_min_minutes=_parse_int_env("TRACKING_INTERVAL_MIN_MINUTES", 1),
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
        scheduler_align_checks_to_interval=_parse_bool_env(
            "SCHEDULER_ALIGN_CHECKS_TO_INTERVAL",
            False,
        ),
        scheduler_alert_cooldown_minutes=_parse_int_env(
            "SCHEDULER_ALERT_COOLDOWN_MINUTES", 60
        ),
        scheduler_queue_redis_url=scheduler_queue_redis_url,
        scheduler_queue_stream_key=_parse_str_env(
            "SCHEDULER_QUEUE_STREAM_KEY",
            "tracking:scheduler:jobs",
        ),
        scheduler_queue_consumer_group=_parse_str_env(
            "SCHEDULER_QUEUE_CONSUMER_GROUP",
            "tracking-scheduler-workers",
        ),
        scheduler_queue_max_stream_length=_parse_int_env(
            "SCHEDULER_QUEUE_MAX_STREAM_LENGTH", 1_000_000
        ),
        scheduler_worker_claim_batch_size=_parse_int_env(
            "SCHEDULER_WORKER_CLAIM_BATCH_SIZE", 64
        ),
        scheduler_worker_block_seconds=_parse_int_env(
            "SCHEDULER_WORKER_BLOCK_SECONDS", 5
        ),
        scheduler_worker_pending_idle_seconds=_parse_int_env(
            "SCHEDULER_WORKER_PENDING_IDLE_SECONDS", 60
        ),
        notifier_poll_seconds=_parse_int_env("NOTIFIER_POLL_SECONDS", 5),
        notifier_claim_batch_size=_parse_int_env("NOTIFIER_CLAIM_BATCH_SIZE", 100),
        notifier_lease_seconds=_parse_int_env("NOTIFIER_LEASE_SECONDS", 120),
        notifier_max_attempts=_parse_int_env("NOTIFIER_MAX_ATTEMPTS", 5),
        notifier_base_backoff_seconds=_parse_int_env("NOTIFIER_BASE_BACKOFF_SECONDS", 30),
        notifier_backoff_max_seconds=_parse_int_env("NOTIFIER_BACKOFF_MAX_SECONDS", 3600),
        notifier_retry_jitter_percent=_parse_int_env("NOTIFIER_RETRY_JITTER_PERCENT", 10),
        notifier_max_inflight=_parse_int_env("NOTIFIER_MAX_INFLIGHT", 16),
        telegram_sender_min_interval_ms=_parse_int_env(
            "TELEGRAM_SENDER_MIN_INTERVAL_MS", 50
        ),
        telegram_bot_token=_parse_str_env("TELEGRAM_BOT_TOKEN", ""),
        telegram_bot_username=_parse_str_env("TELEGRAM_BOT_USERNAME", ""),
        telegram_webhook_secret=_parse_str_env("TELEGRAM_WEBHOOK_SECRET", ""),
        telegram_connect_ttl_minutes=_parse_int_env("TELEGRAM_CONNECT_TTL_MINUTES", 15),
        affiliate_link_template=_parse_str_env("AFFILIATE_LINK_TEMPLATE", ""),
        auth_required=_parse_str_env("AUTH_REQUIRED", "false").lower() == "true",
        better_auth_jwks_url=_parse_str_env("BETTER_AUTH_JWKS_URL", ""),
        better_auth_jwt_issuer=_parse_str_env("BETTER_AUTH_JWT_ISSUER", ""),
        better_auth_jwt_audience=_parse_str_env("BETTER_AUTH_JWT_AUDIENCE", ""),
        better_auth_jwt_algorithms=_parse_csv(
            _parse_str_env("BETTER_AUTH_JWT_ALGORITHMS", ""),
            default=("EdDSA",),
        ),
        better_auth_jwks_cache_ttl_seconds=_parse_int_env(
            "BETTER_AUTH_JWKS_CACHE_TTL_SECONDS", 300
        ),
        better_auth_jwks_timeout_seconds=float(
            _parse_str_env("BETTER_AUTH_JWKS_TIMEOUT_SECONDS", "2")
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
    if settings.scrape_result_cache_ttl_seconds < 60:
        raise RuntimeError("SCRAPE_RESULT_CACHE_TTL_SECONDS must be >= 60")
    if settings.scrape_result_cache_max_batches < 1:
        raise RuntimeError("SCRAPE_RESULT_CACHE_MAX_BATCHES must be >= 1")
    for proxy_url in settings.scrape_proxy_urls:
        parsed_proxy = urlparse(proxy_url)
        if parsed_proxy.scheme not in {"http", "https", "socks4", "socks5"} or not parsed_proxy.netloc:
            raise RuntimeError(
                "SCRAPE_PROXY_URLS must contain valid http, https, socks4, or socks5 proxy URLs"
            )
    for list_url in settings.scrape_proxy_list_urls:
        parsed_list_url = urlparse(list_url)
        if parsed_list_url.scheme not in {"http", "https"} or not parsed_list_url.netloc:
            raise RuntimeError("SCRAPE_PROXY_LIST_URLS must contain valid http or https URLs")
    if not settings.server_fetch_allowed_ports:
        raise RuntimeError("SERVER_FETCH_ALLOWED_PORTS must not be empty")
    if any(port < 1 or port > 65535 for port in settings.server_fetch_allowed_ports):
        raise RuntimeError("SERVER_FETCH_ALLOWED_PORTS must contain valid TCP ports")
    if settings.rate_limit_enabled and not settings.rate_limit_redis_url:
        raise RuntimeError("RATE_LIMIT_REDIS_URL is required when rate limiting is enabled")
    if settings.rate_limit_window_seconds < 1:
        raise RuntimeError("RATE_LIMIT_WINDOW_SECONDS must be >= 1")
    if settings.rate_limit_default_requests < 1:
        raise RuntimeError("RATE_LIMIT_DEFAULT_REQUESTS must be >= 1")
    if settings.rate_limit_scrape_requests < 1:
        raise RuntimeError("RATE_LIMIT_SCRAPE_REQUESTS must be >= 1")
    if settings.rate_limit_discover_requests < 1:
        raise RuntimeError("RATE_LIMIT_DISCOVER_REQUESTS must be >= 1")
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
    if settings.scheduler_alert_cooldown_minutes < 0:
        raise RuntimeError("SCHEDULER_ALERT_COOLDOWN_MINUTES must be >= 0")
    if not settings.scheduler_queue_redis_url:
        raise RuntimeError("SCHEDULER_QUEUE_REDIS_URL must not be empty")
    if not settings.scheduler_queue_stream_key:
        raise RuntimeError("SCHEDULER_QUEUE_STREAM_KEY must not be empty")
    if not settings.scheduler_queue_consumer_group:
        raise RuntimeError("SCHEDULER_QUEUE_CONSUMER_GROUP must not be empty")
    if settings.scheduler_queue_max_stream_length < 1:
        raise RuntimeError("SCHEDULER_QUEUE_MAX_STREAM_LENGTH must be >= 1")
    if settings.scheduler_worker_claim_batch_size < 1:
        raise RuntimeError("SCHEDULER_WORKER_CLAIM_BATCH_SIZE must be >= 1")
    if settings.scheduler_worker_block_seconds < 1:
        raise RuntimeError("SCHEDULER_WORKER_BLOCK_SECONDS must be >= 1")
    if settings.scheduler_worker_pending_idle_seconds < 1:
        raise RuntimeError("SCHEDULER_WORKER_PENDING_IDLE_SECONDS must be >= 1")
    if settings.notifier_poll_seconds < 1:
        raise RuntimeError("NOTIFIER_POLL_SECONDS must be >= 1")
    if settings.notifier_claim_batch_size < 1:
        raise RuntimeError("NOTIFIER_CLAIM_BATCH_SIZE must be >= 1")
    if settings.notifier_lease_seconds < 1:
        raise RuntimeError("NOTIFIER_LEASE_SECONDS must be >= 1")
    if settings.notifier_max_attempts < 1:
        raise RuntimeError("NOTIFIER_MAX_ATTEMPTS must be >= 1")
    if settings.notifier_base_backoff_seconds < 1:
        raise RuntimeError("NOTIFIER_BASE_BACKOFF_SECONDS must be >= 1")
    if settings.notifier_backoff_max_seconds < settings.notifier_base_backoff_seconds:
        raise RuntimeError(
            "NOTIFIER_BACKOFF_MAX_SECONDS must be >= NOTIFIER_BASE_BACKOFF_SECONDS"
        )
    if settings.notifier_retry_jitter_percent < 0 or settings.notifier_retry_jitter_percent > 50:
        raise RuntimeError("NOTIFIER_RETRY_JITTER_PERCENT must be between 0 and 50")
    if settings.notifier_max_inflight < 1:
        raise RuntimeError("NOTIFIER_MAX_INFLIGHT must be >= 1")
    if settings.telegram_sender_min_interval_ms < 0:
        raise RuntimeError("TELEGRAM_SENDER_MIN_INTERVAL_MS must be >= 0")
    if settings.telegram_connect_ttl_minutes < 1 or settings.telegram_connect_ttl_minutes > 120:
        raise RuntimeError("TELEGRAM_CONNECT_TTL_MINUTES must be between 1 and 120")
    if settings.auth_required:
        if not settings.better_auth_jwks_url:
            raise RuntimeError("BETTER_AUTH_JWKS_URL is required when AUTH_REQUIRED=true")
        if not settings.better_auth_jwt_issuer:
            raise RuntimeError("BETTER_AUTH_JWT_ISSUER is required when AUTH_REQUIRED=true")
        if not settings.better_auth_jwt_audience:
            raise RuntimeError("BETTER_AUTH_JWT_AUDIENCE is required when AUTH_REQUIRED=true")
        if settings.environment == "production":
            _require_https_url("BETTER_AUTH_JWKS_URL", settings.better_auth_jwks_url)
            _require_https_url("BETTER_AUTH_JWT_ISSUER", settings.better_auth_jwt_issuer)
    if settings.environment == "production" and "*" in settings.cors_allow_origins:
        raise RuntimeError(
            "CORS_ALLOW_ORIGINS must be explicit in production when credentials are enabled"
        )
    if not settings.better_auth_jwt_algorithms:
        raise RuntimeError("BETTER_AUTH_JWT_ALGORITHMS must not be empty")
    if settings.better_auth_jwks_cache_ttl_seconds < 30:
        raise RuntimeError("BETTER_AUTH_JWKS_CACHE_TTL_SECONDS must be >= 30")
    if settings.better_auth_jwks_timeout_seconds <= 0:
        raise RuntimeError("BETTER_AUTH_JWKS_TIMEOUT_SECONDS must be > 0")

    return settings


def _require_https_url(name: str, value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise RuntimeError(f"{name} must use https in production")
