from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.api.dependencies import get_app_settings, get_jwt_verifier
from app.api.schemas.scrapes import ComponentHealthResponse, HealthResponse, ReadinessResponse
from app.core.auth import BetterAuthJwtVerifier
from app.core.config import Settings
from app.core.readiness import (
    ComponentReadiness,
    check_postgres_ready,
    check_redis_ready,
)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse)
async def get_ready(
    settings: Settings = Depends(get_app_settings),
    verifier: BetterAuthJwtVerifier = Depends(get_jwt_verifier),
) -> JSONResponse:
    checks = {
        "postgres": await run_in_threadpool(check_postgres_ready, settings),
        "redis": await run_in_threadpool(check_redis_ready, settings),
        "jwks": await run_in_threadpool(_check_jwks_ready, settings, verifier),
    }
    response_status = "ok" if all(check.status != "error" for check in checks.values()) else "error"
    payload = ReadinessResponse(
        status=response_status,
        checks={
            name: ComponentHealthResponse.model_validate(asdict(check))
            for name, check in checks.items()
        },
    )
    status_code = (
        status.HTTP_200_OK
        if response_status == "ok"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _check_jwks_ready(
    settings: Settings,
    verifier: BetterAuthJwtVerifier,
) -> ComponentReadiness:
    if not settings.auth_required:
        return ComponentReadiness(status="skipped")
    try:
        verifier.warm()
    except Exception:
        return ComponentReadiness(status="error", error="jwks_unavailable")
    return ComponentReadiness(status="ok")
