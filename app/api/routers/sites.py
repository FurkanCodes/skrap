from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_scrape_service
from app.api.schemas.scrapes import SiteInfoResponse, SitesResponse
from app.application.scrape_service import ScrapeBatchService

router = APIRouter(prefix="/sites", tags=["sites"])


@router.get("", response_model=SitesResponse)
def get_sites(service: ScrapeBatchService = Depends(get_scrape_service)) -> SitesResponse:
    site_map = service.list_sites()
    ordered_sites = [SiteInfoResponse(key=key, description=site_map[key]) for key in sorted(site_map)]
    return SitesResponse(sites=ordered_sites)

