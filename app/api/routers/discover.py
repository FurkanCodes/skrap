from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.api.dependencies import (
    get_app_settings,
    get_current_principal,
    get_discovery_service,
    get_scrape_job_service,
    require_rate_limit,
    validate_server_fetch_urls,
)
from app.api.schemas.scrapes import (
    DiscoverBatchRequest,
    DiscoverBatchResponse,
    DiscoveryStatsResponse,
    PriceResultResponse,
    ScrapeJobCreateResponse,
)
from app.application.scrape_job_service import ScrapeJobService
from app.application.discovery_service import (
    DiscoveryBatchInput,
    DiscoveryBatchService,
    DiscoveryPhaseError,
)
from app.core.auth import AuthPrincipal
from app.core.config import Settings

router = APIRouter(prefix="/discover", tags=["discover"])


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


def _build_discover_input(
    payload: DiscoverBatchRequest,
    request: Request,
    *,
    settings: Settings,
    service: DiscoveryBatchService,
) -> DiscoveryBatchInput:
    start_urls = [str(url) for url in payload.start_urls]
    validate_server_fetch_urls(start_urls, settings=settings)
    if len(start_urls) > settings.scrape_max_urls:
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
    return DiscoveryBatchInput(
        request_id=request_id,
        start_urls=start_urls,
        timeout_seconds=timeout_seconds,
        selected_sites=selected_sites,
        fetch_mode=payload.fetch_mode,
        max_pages=payload.max_pages,
        max_depth=payload.max_depth,
    )


def _to_discover_response(output) -> DiscoverBatchResponse:
    results = [PriceResultResponse.model_validate(asdict(item)) for item in output.results]
    return DiscoverBatchResponse(
        request_id=output.request_id,
        seed_count=output.seed_count,
        discovered_url_count=output.discovered_url_count,
        discovered_urls=output.discovered_urls,
        discovery_stats=DiscoveryStatsResponse.model_validate(asdict(output.discovery_stats)),
        total_urls=output.total_urls,
        success_count=output.success_count,
        failure_count=output.failure_count,
        results=results,
    )


@router.post("", response_model=DiscoverBatchResponse)
async def post_discover_batch(
    payload: DiscoverBatchRequest,
    request: Request,
    _principal: AuthPrincipal = Depends(get_current_principal),
    _rate_limit: None = Depends(
        require_rate_limit(bucket="discover", limit_attr="rate_limit_discover_requests")
    ),
    settings: Settings = Depends(get_app_settings),
    service: DiscoveryBatchService = Depends(get_discovery_service),
) -> DiscoverBatchResponse:
    discover_input = _build_discover_input(payload, request, settings=settings, service=service)
    try:
        output = await run_in_threadpool(service.discover_and_scrape, discover_input)
    except DiscoveryPhaseError as exc:
        raise HTTPException(status_code=502, detail=f"discovery_failed: {exc}") from exc
    return await run_in_threadpool(_to_discover_response, output)


@router.post("/jobs", response_model=ScrapeJobCreateResponse, status_code=202)
async def post_discover_job(
    payload: DiscoverBatchRequest,
    request: Request,
    _principal: AuthPrincipal = Depends(get_current_principal),
    _rate_limit: None = Depends(
        require_rate_limit(bucket="discover", limit_attr="rate_limit_discover_requests")
    ),
    settings: Settings = Depends(get_app_settings),
    service: DiscoveryBatchService = Depends(get_discovery_service),
    jobs: ScrapeJobService = Depends(get_scrape_job_service),
) -> ScrapeJobCreateResponse:
    discover_input = _build_discover_input(payload, request, settings=settings, service=service)

    def _run_job() -> DiscoverBatchResponse:
        try:
            return _to_discover_response(service.discover_and_scrape(discover_input))
        except DiscoveryPhaseError as exc:
            raise RuntimeError(f"discovery_failed: {exc}") from exc

    try:
        job = jobs.submit(
            request_id=discover_input.request_id,
            kind="discover",
            run=_run_job,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ScrapeJobCreateResponse(
        job_id=job.job_id,
        request_id=job.request_id,
        status=job.status,
    )
