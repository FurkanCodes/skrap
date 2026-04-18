from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.application.discovery_service import DiscoveryBatchService
from app.application.scrape_result_cache import ScrapeResultCache
from app.application.scrape_service import ScrapeBatchService
from app.application.telegram_integration_service import TelegramIntegrationService
from app.application.tracking_service import TrackingService
from app.application.user_service import MeOutput, UserService
from app.core.auth import AuthPrincipal, AuthenticationError, BetterAuthJwtVerifier
from app.core.config import Settings, get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository
from app.infrastructure.scraper_gateway import ScraplingGateway
from app.tracking import AppUser, UserEntitlement

_http_bearer = HTTPBearer(auto_error=False)


def get_app_settings() -> Settings:
    return get_settings()


@lru_cache
def _get_scrape_service_cached(scrape_concurrency: int) -> ScrapeBatchService:
    gateway = ScraplingGateway()
    return ScrapeBatchService(
        gateway=gateway,
        max_workers=scrape_concurrency,
        use_spider_runtime=True,
    )


def get_scrape_service(
    settings: Settings = Depends(get_app_settings),
) -> ScrapeBatchService:
    return _get_scrape_service_cached(settings.scrape_concurrency)


@lru_cache
def _get_discovery_service_cached(scrape_concurrency: int) -> DiscoveryBatchService:
    scrape_service = _get_scrape_service_cached(scrape_concurrency)
    return DiscoveryBatchService(scrape_service=scrape_service)


def get_discovery_service(
    settings: Settings = Depends(get_app_settings),
) -> DiscoveryBatchService:
    return _get_discovery_service_cached(settings.scrape_concurrency)


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
    repository.initialize()
    return repository


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
    return TrackingService(
        repository=repository,
        scrape_service=scrape_service,
        default_interval_minutes=default_interval_minutes,
        min_interval_minutes=min_interval_minutes,
        max_interval_minutes=max_interval_minutes,
        default_snapshot_limit=snapshot_default_limit,
    )


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
    return TelegramIntegrationService(
        repository=repository,
        bot_token=bot_token,
        bot_username=bot_username,
        connect_ttl_minutes=connect_ttl_minutes,
    )


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
def _get_jwt_verifier_cached(
    jwks_url: str,
    issuer: str,
    audience: str,
    jwks_cache_ttl_seconds: int,
) -> BetterAuthJwtVerifier:
    return BetterAuthJwtVerifier(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=audience,
        jwks_cache_ttl_seconds=jwks_cache_ttl_seconds,
    )


def get_jwt_verifier(
    settings: Settings = Depends(get_app_settings),
) -> BetterAuthJwtVerifier:
    return _get_jwt_verifier_cached(
        jwks_url=settings.better_auth_jwks_url,
        issuer=settings.better_auth_jwt_issuer,
        audience=settings.better_auth_jwt_audience,
        jwks_cache_ttl_seconds=settings.better_auth_jwks_cache_ttl_seconds,
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
        raise HTTPException(status_code=401, detail=str(exc)) from exc


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
    return UserService(repository=repository)


def get_user_service(
    settings: Settings = Depends(get_app_settings),
) -> UserService:
    if not settings.auth_required:
        return _get_dev_user_service_cached()
    return _get_user_service_cached(
        database_url=settings.tracking_database_url,
        db_pool_min_size=settings.tracking_db_pool_min_size,
        db_pool_max_size=settings.tracking_db_pool_max_size,
    )


@dataclass(frozen=True)
class _DevUserService:
    """No-op user service for local/test mode when auth is disabled."""

    def provision_from_principal(self, principal: AuthPrincipal) -> AppUser:
        now = _utcnow_iso()
        return AppUser(
            user_id=principal.subject,
            auth_provider="better_auth",
            auth_subject=principal.subject,
            email=principal.email,
            display_name=principal.name,
            created_at=now,
            updated_at=now,
        )

    def get_me(self, principal: AuthPrincipal) -> MeOutput:
        now = _utcnow_iso()
        return MeOutput(
            user=self.provision_from_principal(principal),
            entitlement=UserEntitlement(
                user_id=principal.subject,
                plan_key="free",
                max_active_trackers=1_000_000,
                min_check_interval_minutes=5,
                monthly_check_units=1_000_000,
                allowed_channels="telegram,log",
                overage_policy="block",
                created_at=now,
                updated_at=now,
            ),
        )


@lru_cache
def _get_dev_user_service_cached() -> _DevUserService:
    return _DevUserService()


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
