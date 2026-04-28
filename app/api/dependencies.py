from __future__ import annotations

from functools import lru_cache
import hashlib
import logging
import time
from typing import Callable, TypeVar

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.application.discovery_service import DiscoveryBatchService
from app.application.affiliate_link_service import AffiliateLinkService
from app.application.scrape_job_service import ScrapeJobService
from app.application.scrape_result_cache import ScrapeResultCache
from app.application.scrape_service import ScrapeBatchService
from app.application.telegram_integration_service import TelegramIntegrationService
from app.application.tracking_service import TrackingService
from app.application.user_service import UserService
from app.core.auth import AuthPrincipal, AuthenticationError, BetterAuthJwtVerifier
from app.core.config import Settings, get_settings
from app.core.perf import TimedProxy, log_span, perf_enabled
from app.core.url_security import (
    ServerFetchUrlPolicy,
    UnsafeServerFetchUrlError,
    validate_server_fetch_url,
)
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository
from app.infrastructure.redis_rate_limiter import (
    RateLimitUnavailable,
    RedisFixedWindowRateLimiter,
)
from app.infrastructure.scraper_gateway import ScraplingGateway

_http_bearer = HTTPBearer(auto_error=False)
_Instance = TypeVar("_Instance")
_logger = logging.getLogger("scraper_api.auth")
_rate_limit_logger = logging.getLogger("scraper_api.rate_limit")


def get_app_settings() -> Settings:
    return get_settings()


@lru_cache
def _get_rate_limiter_cached(redis_url: str) -> RedisFixedWindowRateLimiter:
    return RedisFixedWindowRateLimiter(redis_url=redis_url)


def get_rate_limiter(
    settings: Settings = Depends(get_app_settings),
) -> RedisFixedWindowRateLimiter | None:
    if not settings.rate_limit_enabled:
        return None
    return _get_rate_limiter_cached(settings.rate_limit_redis_url)


def require_rate_limit(
    *,
    bucket: str,
    limit_attr: str = "rate_limit_default_requests",
) -> Callable[..., None]:
    def _dependency(
        request: Request,
        principal: AuthPrincipal = Depends(get_current_principal),
        settings: Settings = Depends(get_app_settings),
        limiter: RedisFixedWindowRateLimiter | None = Depends(get_rate_limiter),
    ) -> None:
        if limiter is None:
            return
        limit = int(getattr(settings, limit_attr))
        subject = _rate_limit_subject(principal=principal, request=request)
        try:
            result = limiter.check(
                bucket=bucket,
                subject=subject,
                limit=limit,
                window_seconds=settings.rate_limit_window_seconds,
            )
        except RateLimitUnavailable:
            _rate_limit_logger.warning("rate_limit_unavailable bucket=%s", bucket, exc_info=True)
            if settings.rate_limit_fail_open:
                return
            raise HTTPException(status_code=503, detail="Rate limit store unavailable") from None

        if result.allowed:
            return

        _rate_limit_logger.info("rate_limited bucket=%s subject=%s", bucket, subject)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={
                "Retry-After": str(result.retry_after_seconds),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
            },
        )

    return _dependency


def _rate_limit_subject(*, principal: AuthPrincipal, request: Request) -> str:
    raw_subject = f"user:{principal.provider}:{principal.subject}"
    if not principal.subject:
        client_host = request.client.host if request.client is not None else "unknown"
        raw_subject = f"ip:{client_host}"
    return hashlib.sha256(raw_subject.encode("utf-8")).hexdigest()[:32]


def validate_server_fetch_urls(urls: list[str], *, settings: Settings) -> None:
    policy = ServerFetchUrlPolicy(
        allowed_ports=settings.server_fetch_allowed_ports,
        allow_private_networks=settings.server_fetch_allow_private_networks,
        resolve_dns=settings.server_fetch_resolve_dns,
    )
    for url in urls:
        try:
            validate_server_fetch_url(url, policy=policy)
        except UnsafeServerFetchUrlError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"URL is not allowed for server-side fetching: {exc}",
            ) from exc


@lru_cache
def _get_scrape_service_cached(scrape_concurrency: int) -> ScrapeBatchService:
    gateway = ScraplingGateway()
    service = ScrapeBatchService(
        gateway=gateway,
        max_workers=scrape_concurrency,
        use_spider_runtime=True,
    )
    return _maybe_profile(service, component="scrape_service")


def get_scrape_service(
    settings: Settings = Depends(get_app_settings),
) -> ScrapeBatchService:
    return _get_scrape_service_cached(settings.scrape_concurrency)


@lru_cache
def _get_discovery_service_cached(scrape_concurrency: int) -> DiscoveryBatchService:
    scrape_service = _get_scrape_service_cached(scrape_concurrency)
    service = DiscoveryBatchService(scrape_service=scrape_service)
    return _maybe_profile(service, component="discovery_service")


def get_discovery_service(
    settings: Settings = Depends(get_app_settings),
) -> DiscoveryBatchService:
    return _get_discovery_service_cached(settings.scrape_concurrency)


@lru_cache
def _get_scrape_job_service_cached(
    scrape_concurrency: int,
    ttl_seconds: int,
    max_jobs: int,
) -> ScrapeJobService:
    return ScrapeJobService(
        max_workers=scrape_concurrency,
        ttl_seconds=ttl_seconds,
        max_jobs=max_jobs,
    )


def get_scrape_job_service(
    settings: Settings = Depends(get_app_settings),
) -> ScrapeJobService:
    return _get_scrape_job_service_cached(
        scrape_concurrency=settings.scrape_concurrency,
        ttl_seconds=settings.scrape_result_cache_ttl_seconds,
        max_jobs=settings.scrape_result_cache_max_batches,
    )


@lru_cache
def _get_scrape_result_cache_cached(
    ttl_seconds: int,
    max_batches: int,
) -> ScrapeResultCache:
    return ScrapeResultCache(ttl_seconds=ttl_seconds, max_batches=max_batches)


def get_scrape_result_cache(
    settings: Settings = Depends(get_app_settings),
) -> ScrapeResultCache:
    return _get_scrape_result_cache_cached(
        ttl_seconds=settings.scrape_result_cache_ttl_seconds,
        max_batches=settings.scrape_result_cache_max_batches,
    )


@lru_cache
def _get_tracking_repository_cached(
    database_url: str,
    db_pool_min_size: int,
    db_pool_max_size: int,
) -> PostgresTrackingRepository:
    repository = PostgresTrackingRepository(
        database_url=database_url,
        min_pool_size=db_pool_min_size,
        max_pool_size=db_pool_max_size,
    )
    started = time.perf_counter()
    repository.initialize()
    log_span(
        component="tracking_repository",
        operation="initialize",
        duration_ms=(time.perf_counter() - started) * 1000,
        always=True,
    )
    return _maybe_profile(repository, component="tracking_repository")


@lru_cache
def _get_tracking_service_cached(
    scrape_concurrency: int,
    database_url: str,
    db_pool_min_size: int,
    db_pool_max_size: int,
    default_interval_minutes: int,
    min_interval_minutes: int,
    max_interval_minutes: int,
    snapshot_default_limit: int,
) -> TrackingService:
    repository = _get_tracking_repository_cached(
        database_url=database_url,
        db_pool_min_size=db_pool_min_size,
        db_pool_max_size=db_pool_max_size,
    )

    scrape_service = _get_scrape_service_cached(scrape_concurrency)
    service = TrackingService(
        repository=repository,
        scrape_service=scrape_service,
        default_interval_minutes=default_interval_minutes,
        min_interval_minutes=min_interval_minutes,
        max_interval_minutes=max_interval_minutes,
        default_snapshot_limit=snapshot_default_limit,
    )
    return _maybe_profile(service, component="tracking_service")


@lru_cache
def _get_telegram_integration_service_cached(
    database_url: str,
    db_pool_min_size: int,
    db_pool_max_size: int,
    bot_token: str,
    bot_username: str,
    connect_ttl_minutes: int,
) -> TelegramIntegrationService:
    repository = _get_tracking_repository_cached(
        database_url=database_url,
        db_pool_min_size=db_pool_min_size,
        db_pool_max_size=db_pool_max_size,
    )
    service = TelegramIntegrationService(
        repository=repository,
        bot_token=bot_token,
        bot_username=bot_username,
        connect_ttl_minutes=connect_ttl_minutes,
    )
    return _maybe_profile(service, component="telegram_service")


def get_tracking_service(
    settings: Settings = Depends(get_app_settings),
) -> TrackingService:
    return _get_tracking_service_cached(
        scrape_concurrency=settings.scrape_concurrency,
        database_url=settings.tracking_database_url,
        db_pool_min_size=settings.tracking_db_pool_min_size,
        db_pool_max_size=settings.tracking_db_pool_max_size,
        default_interval_minutes=settings.tracking_interval_default_minutes,
        min_interval_minutes=settings.tracking_interval_min_minutes,
        max_interval_minutes=settings.tracking_interval_max_minutes,
        snapshot_default_limit=settings.tracking_snapshot_default_limit,
    )


def get_telegram_integration_service(
    settings: Settings = Depends(get_app_settings),
) -> TelegramIntegrationService:
    return _get_telegram_integration_service_cached(
        database_url=settings.tracking_database_url,
        db_pool_min_size=settings.tracking_db_pool_min_size,
        db_pool_max_size=settings.tracking_db_pool_max_size,
        bot_token=settings.telegram_bot_token,
        bot_username=settings.telegram_bot_username,
        connect_ttl_minutes=settings.telegram_connect_ttl_minutes,
    )


@lru_cache
def _get_affiliate_link_service_cached(
    database_url: str,
    db_pool_min_size: int,
    db_pool_max_size: int,
    redirect_base_url: str,
    signing_secret: str,
    rules_json: str,
    legacy_template: str,
) -> AffiliateLinkService:
    repository = _get_tracking_repository_cached(
        database_url=database_url,
        db_pool_min_size=db_pool_min_size,
        db_pool_max_size=db_pool_max_size,
    )
    service = AffiliateLinkService(
        repository=repository,
        redirect_base_url=redirect_base_url,
        signing_secret=signing_secret,
        rules_json=rules_json,
        legacy_template=legacy_template,
    )
    return _maybe_profile(service, component="affiliate_link_service")


def get_affiliate_link_service(
    settings: Settings = Depends(get_app_settings),
) -> AffiliateLinkService:
    return _get_affiliate_link_service_cached(
        database_url=settings.tracking_database_url,
        db_pool_min_size=settings.tracking_db_pool_min_size,
        db_pool_max_size=settings.tracking_db_pool_max_size,
        redirect_base_url=settings.affiliate_redirect_base_url,
        signing_secret=settings.affiliate_signing_secret,
        rules_json=settings.affiliate_link_rules_json,
        legacy_template=settings.affiliate_link_template,
    )


@lru_cache
def _get_jwt_verifier_cached(
    jwks_url: str,
    issuer: str,
    audience: str,
    algorithms: tuple[str, ...],
    jwks_cache_ttl_seconds: int,
    jwks_timeout_seconds: float,
) -> BetterAuthJwtVerifier:
    verifier = BetterAuthJwtVerifier(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=audience,
        algorithms=algorithms,
        jwks_cache_ttl_seconds=jwks_cache_ttl_seconds,
        jwks_timeout_seconds=jwks_timeout_seconds,
    )
    return _maybe_profile(verifier, component="jwt_verifier")


def get_jwt_verifier(
    settings: Settings = Depends(get_app_settings),
) -> BetterAuthJwtVerifier:
    return _get_jwt_verifier_cached(
        jwks_url=settings.better_auth_jwks_url,
        issuer=settings.better_auth_jwt_issuer,
        audience=settings.better_auth_jwt_audience,
        algorithms=settings.better_auth_jwt_algorithms,
        jwks_cache_ttl_seconds=settings.better_auth_jwks_cache_ttl_seconds,
        jwks_timeout_seconds=settings.better_auth_jwks_timeout_seconds,
    )


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_http_bearer),
    settings: Settings = Depends(get_app_settings),
    verifier: BetterAuthJwtVerifier = Depends(get_jwt_verifier),
) -> AuthPrincipal:
    if not settings.auth_required:
        return AuthPrincipal(
            subject="dev-local-user",
            email="dev@local",
            name="Local Dev",
            issuer="dev",
            audience="dev",
        )
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization scheme")
    try:
        return verifier.verify(credentials.credentials)
    except AuthenticationError as exc:
        _logger.info("auth_failed reason=%s", exc.reason)
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc


@lru_cache
def _get_user_service_cached(
    database_url: str,
    db_pool_min_size: int,
    db_pool_max_size: int,
) -> UserService:
    repository = _get_tracking_repository_cached(
        database_url=database_url,
        db_pool_min_size=db_pool_min_size,
        db_pool_max_size=db_pool_max_size,
    )
    service = UserService(repository=repository)
    return _maybe_profile(service, component="user_service")


def get_user_service(
    settings: Settings = Depends(get_app_settings),
) -> UserService:
    return _get_user_service_cached(
        database_url=settings.tracking_database_url,
        db_pool_min_size=settings.tracking_db_pool_min_size,
        db_pool_max_size=settings.tracking_db_pool_max_size,
    )


def warm_application_dependencies(settings: Settings) -> None:
    """Best-effort cold-path warmup for auth and DB-backed services."""
    if settings.auth_required:
        _soft_warm(
            "jwt_verifier",
            lambda: _get_jwt_verifier_cached(
                jwks_url=settings.better_auth_jwks_url,
                issuer=settings.better_auth_jwt_issuer,
                audience=settings.better_auth_jwt_audience,
                algorithms=settings.better_auth_jwt_algorithms,
                jwks_cache_ttl_seconds=settings.better_auth_jwks_cache_ttl_seconds,
                jwks_timeout_seconds=settings.better_auth_jwks_timeout_seconds,
            ).warm(),
        )

    _soft_warm(
        "tracking_repository",
        lambda: _get_tracking_repository_cached(
            database_url=settings.tracking_database_url,
            db_pool_min_size=settings.tracking_db_pool_min_size,
            db_pool_max_size=settings.tracking_db_pool_max_size,
        ),
    )
    _soft_warm(
        "tracking_service",
        lambda: _get_tracking_service_cached(
            scrape_concurrency=settings.scrape_concurrency,
            database_url=settings.tracking_database_url,
            db_pool_min_size=settings.tracking_db_pool_min_size,
            db_pool_max_size=settings.tracking_db_pool_max_size,
            default_interval_minutes=settings.tracking_interval_default_minutes,
            min_interval_minutes=settings.tracking_interval_min_minutes,
            max_interval_minutes=settings.tracking_interval_max_minutes,
            snapshot_default_limit=settings.tracking_snapshot_default_limit,
        ),
    )
    _soft_warm(
        "telegram_service",
        lambda: _get_telegram_integration_service_cached(
            database_url=settings.tracking_database_url,
            db_pool_min_size=settings.tracking_db_pool_min_size,
            db_pool_max_size=settings.tracking_db_pool_max_size,
            bot_token=settings.telegram_bot_token,
            bot_username=settings.telegram_bot_username,
            connect_ttl_minutes=settings.telegram_connect_ttl_minutes,
        ),
    )
    _soft_warm(
        "user_service",
        lambda: _get_user_service_cached(
            database_url=settings.tracking_database_url,
            db_pool_min_size=settings.tracking_db_pool_min_size,
            db_pool_max_size=settings.tracking_db_pool_max_size,
        ),
    )


def _soft_warm(name: str, fn) -> None:  # noqa: ANN001
    started = time.perf_counter()
    try:
        fn()
    except Exception:
        _logger.warning("warmup_failed dependency=%s", name, exc_info=True)
        return
    _logger.info(
        "warmup_complete dependency=%s duration_ms=%.2f",
        name,
        (time.perf_counter() - started) * 1000,
    )


def _maybe_profile(instance: _Instance, *, component: str) -> _Instance:
    if not perf_enabled():
        return instance
    return TimedProxy(instance, component=component)  # type: ignore[return-value]
