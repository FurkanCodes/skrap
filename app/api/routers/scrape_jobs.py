from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_current_principal, get_scrape_job_service
from app.api.schemas.scrapes import ScrapeJobStatusResponse
from app.application.scrape_job_service import ScrapeJobService
from app.core.auth import AuthPrincipal

router = APIRouter(prefix="/scrape-jobs", tags=["scrape-jobs"])


@router.get("/{job_id}", response_model=ScrapeJobStatusResponse)
async def get_scrape_job(
    job_id: str,
    _principal: AuthPrincipal = Depends(get_current_principal),
    jobs: ScrapeJobService = Depends(get_scrape_job_service),
) -> ScrapeJobStatusResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="scrape job not found")
    return ScrapeJobStatusResponse(
        job_id=job.job_id,
        request_id=job.request_id,
        kind=job.kind,
        status=job.status,
        progress=job.progress,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
