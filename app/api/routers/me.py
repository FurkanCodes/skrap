from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from time import monotonic
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_current_principal, get_user_service
from app.api.schemas.me import MeEntitlementResponse, MeResponse, MeUsageResponse, MeUserResponse
from app.application.user_service import UserService
from app.core.auth import AuthPrincipal

router = APIRouter(prefix="/me", tags=["me"])
USAGE_STREAM_POLL_SECONDS = 5.0
USAGE_STREAM_HEARTBEAT_SECONDS = 20.0


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


def _encode_usage_event(payload: MeUsageResponse) -> str:
    encoded = json.dumps(payload.model_dump(), separators=(",", ":"))
    return f"event: usage\ndata: {encoded}\n\n"


async def _usage_stream(
    *,
    request: Request,
    principal: AuthPrincipal,
    service: UserService,
) -> AsyncIterator[str]:
    last_event_payload: str | None = None
    last_heartbeat_at = monotonic()

    while True:
        if await request.is_disconnected():
            break

        usage_output = await run_in_threadpool(service.get_usage, principal)
        usage = MeUsageResponse.model_validate(asdict(usage_output))
        encoded_event = _encode_usage_event(usage)

        if encoded_event != last_event_payload:
            last_event_payload = encoded_event
            last_heartbeat_at = monotonic()
            yield encoded_event
        elif monotonic() - last_heartbeat_at >= USAGE_STREAM_HEARTBEAT_SECONDS:
            last_heartbeat_at = monotonic()
            yield ": heartbeat\n\n"

        await asyncio.sleep(USAGE_STREAM_POLL_SECONDS)


@router.get("/usage/stream")
async def stream_me_usage(
    request: Request,
    principal: AuthPrincipal = Depends(get_current_principal),
    service: UserService = Depends(get_user_service),
) -> StreamingResponse:
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _usage_stream(request=request, principal=principal, service=service),
        media_type="text/event-stream",
        headers=headers,
    )
