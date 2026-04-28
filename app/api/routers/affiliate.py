from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_affiliate_link_service
from app.api.schemas.affiliate import AffiliateResolveResponse
from app.application.affiliate_link_service import AffiliateLinkError, AffiliateLinkService

router = APIRouter(prefix="/affiliate", tags=["affiliate"])


@router.get("/resolve/{token}", response_model=AffiliateResolveResponse)
def resolve_affiliate_link(
    token: str,
    service: AffiliateLinkService = Depends(get_affiliate_link_service),
) -> AffiliateResolveResponse:
    try:
        result = service.resolve(token)
    except AffiliateLinkError as exc:
        raise HTTPException(status_code=404, detail="Link unavailable") from exc
    return AffiliateResolveResponse(
        target_url=result.target_url,
        is_affiliate=result.is_affiliate,
    )
