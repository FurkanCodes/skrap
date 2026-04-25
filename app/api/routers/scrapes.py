from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.api.dependencies import (
    get_app_settings,
    get_current_principal,
    get_scrape_job_service,
    get_scrape_result_cache,
    get_scrape_service,
    require_rate_limit,
    validate_server_fetch_urls,
)
from app.api.schemas.scrapes import (
    PriceResultResponse,
    ScrapeBatchRequest,
    ScrapeBatchResponse,
    ScrapeJobCreateResponse,
)
from app.application.scrape_job_service import ScrapeJobService
from app.application.scrape_result_cache import ScrapeResultCache
from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchService
from app.core.auth import AuthPrincipal
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


def _build_scrape_input(
    payload: ScrapeBatchRequest,
    request: Request,
    *,
    settings: Settings,
    service: ScrapeBatchService,
) -> ScrapeBatchInput:
    request_urls = [str(url) for url in payload.urls]
    validate_server_fetch_urls(request_urls, settings=settings)
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
    return ScrapeBatchInput(
        request_id=request_id,
        urls=request_urls,
        timeout_seconds=timeout_seconds,
        selected_sites=selected_sites,
        fetch_mode=payload.fetch_mode,
    )


def _to_scrape_response(
    output,
    *,
    cache: ScrapeResultCache,
) -> ScrapeBatchResponse:
    cached_refs = cache.store(output.results)
    results = []
    for item, result_ref in zip(output.results, cached_refs.result_refs, strict=False):
        payload_data = asdict(item)
        payload_data["result_ref"] = result_ref
        results.append(PriceResultResponse.model_validate(payload_data))

    return ScrapeBatchResponse(
        request_id=output.request_id,
        scrape_ref=cached_refs.scrape_ref,
        total_urls=output.total_urls,
        success_count=output.success_count,
        failure_count=output.failure_count,
        results=results,
    )


@router.post("", response_model=ScrapeBatchResponse)
async def post_scrape_batch(
    payload: ScrapeBatchRequest,
    request: Request,
    _principal: AuthPrincipal = Depends(get_current_principal),
    _rate_limit: None = Depends(
        require_rate_limit(bucket="scrapes", limit_attr="rate_limit_scrape_requests")
    ),
    settings: Settings = Depends(get_app_settings),
    service: ScrapeBatchService = Depends(get_scrape_service),
    cache: ScrapeResultCache = Depends(get_scrape_result_cache),
) -> ScrapeBatchResponse:
    batch_input = _build_scrape_input(payload, request, settings=settings, service=service)
    output = await run_in_threadpool(service.scrape, batch_input)
    return await run_in_threadpool(_to_scrape_response, output, cache=cache)


@router.post("/jobs", response_model=ScrapeJobCreateResponse, status_code=202)
async def post_scrape_job(
    payload: ScrapeBatchRequest,
    request: Request,
    _principal: AuthPrincipal = Depends(get_current_principal),
    _rate_limit: None = Depends(
        require_rate_limit(bucket="scrapes", limit_attr="rate_limit_scrape_requests")
    ),
    settings: Settings = Depends(get_app_settings),
    service: ScrapeBatchService = Depends(get_scrape_service),
    cache: ScrapeResultCache = Depends(get_scrape_result_cache),
    jobs: ScrapeJobService = Depends(get_scrape_job_service),
) -> ScrapeJobCreateResponse:
    batch_input = _build_scrape_input(payload, request, settings=settings, service=service)

    def _run_job() -> ScrapeBatchResponse:
        return _to_scrape_response(service.scrape(batch_input), cache=cache)

    try:
        job = jobs.submit(
            request_id=batch_input.request_id,
            kind="scrapes",
            run=_run_job,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ScrapeJobCreateResponse(
        job_id=job.job_id,
        request_id=job.request_id,
        status=job.status,
    )
