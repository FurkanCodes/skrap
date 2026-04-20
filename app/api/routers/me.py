from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from app.api.dependencies import get_current_principal, get_user_service
from app.api.schemas.me import MeEntitlementResponse, MeResponse, MeUsageResponse, MeUserResponse
from app.application.user_service import UserService
from app.core.auth import AuthPrincipal

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=MeResponse)
async def get_me(
    principal: AuthPrincipal = Depends(get_current_principal),
    service: UserService = Depends(get_user_service),
) -> MeResponse:
    output = await run_in_threadpool(service.get_me, principal)
    return MeResponse(
        user=MeUserResponse.model_validate(asdict(output.user)),
        entitlement=MeEntitlementResponse.model_validate(asdict(output.entitlement)),
        usage=MeUsageResponse.model_validate(asdict(output.usage)),
    )
