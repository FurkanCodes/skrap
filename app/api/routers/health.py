from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas.scrapes import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    return HealthResponse(status="ok")

