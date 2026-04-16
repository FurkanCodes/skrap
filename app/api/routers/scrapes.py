from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.api.dependencies import get_app_settings, get_scrape_service
from app.api.schemas.scrapes import PriceResultResponse, ScrapeBatchRequest, ScrapeBatchResponse
from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchService
from app.core.config import Settings

router = APIRouter(prefix="/scrapes", tags=["scrapes"])


def _normalize_selected_sites(raw_sites: list[str] | None, supported_sites: set[str]) -> set[str] | None:
    if raw_sites is None:
        return None

    normalized: set[str] = set()
    for value in raw_sites:
        site_key = value.strip().lower()
        if not site_key:
            continue
        if site_key in {"all", "*"}:
            return None
        if site_key not in supported_sites:
            supported = ", ".join(sorted(supported_sites))
            raise HTTPException(status_code=422, detail=f"Unsupported site key '{site_key}'. Supported keys: {supported}")
        normalized.add(site_key)

    return normalized or None


@router.post("", response_model=ScrapeBatchResponse)
async def post_scrape_batch(
    payload: ScrapeBatchRequest,
    request: Request,
    settings: Settings = Depends(get_app_settings),
    service: ScrapeBatchService = Depends(get_scrape_service),
) -> ScrapeBatchResponse:
    request_urls = [str(url) for url in payload.urls]
    if len(request_urls) > settings.scrape_max_urls:
        raise HTTPException(status_code=422, detail=f"At most {settings.scrape_max_urls} URLs are allowed per request")

    timeout_seconds = payload.timeout_seconds or settings.scrape_timeout_default
    if timeout_seconds < settings.scrape_timeout_min or timeout_seconds > settings.scrape_timeout_max:
        raise HTTPException(
            status_code=422,
            detail=(
                f"timeout_seconds must be between {settings.scrape_timeout_min} "
                f"and {settings.scrape_timeout_max}"
            ),
        )

    selected_sites = _normalize_selected_sites(payload.sites, set(service.list_sites()))
    request_id = str(getattr(request.state, "request_id", ""))
    batch_input = ScrapeBatchInput(
        request_id=request_id,
        urls=request_urls,
        timeout_seconds=timeout_seconds,
        selected_sites=selected_sites,
    )

    output = await run_in_threadpool(service.scrape, batch_input)
    results = [PriceResultResponse.model_validate(asdict(item)) for item in output.results]

    return ScrapeBatchResponse(
        request_id=output.request_id,
        total_urls=output.total_urls,
        success_count=output.success_count,
        failure_count=output.failure_count,
        results=results,
    )

