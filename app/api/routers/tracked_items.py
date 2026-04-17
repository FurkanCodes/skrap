from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.api.dependencies import get_app_settings, get_scrape_result_cache, get_tracking_service
from app.api.schemas.tracking import (
    PriceSnapshotResponse,
    TrackedItemBootstrapRequest,
    TrackedItemBootstrapResponse,
    TrackedItemDetailResponse,
    TrackedItemListResponse,
    TrackedItemResponse,
    TrackedItemUpdateRequest,
    TrackedItemUpdateResponse,
)
from app.application.tracking_service import (
    TrackingBootstrapError,
    TrackingBootstrapInput,
    TrackingService,
    TrackingServiceError,
    TrackingUpdateInput,
)
from app.application.scrape_result_cache import ScrapeResultCache
from app.core.config import Settings
from app.tracking import TrackedItem, derive_tracking_mode

router = APIRouter(prefix="/tracked-items", tags=["tracked-items"])


def _normalize_selected_sites(raw_sites: list[str] | None, supported_sites: set[str]) -> set[str] | None:
    """Normalize site filters with `all/*` wildcard semantics."""
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
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported site key '{site_key}'. Supported keys: {supported}",
            )
        normalized.add(site_key)

    return normalized or None


def _to_item_response(item: TrackedItem) -> TrackedItemResponse:
    """Map tracking domain object to API response with computed mode flags."""
    payload = asdict(item)
    mode = derive_tracking_mode(item.target_amount)
    payload["tracking_mode"] = mode
    payload["alerts_enabled"] = mode == "alert"
    return TrackedItemResponse.model_validate(payload)


@router.post("", response_model=TrackedItemBootstrapResponse)
async def post_tracked_item_bootstrap(
    payload: TrackedItemBootstrapRequest,
    request: Request,
    settings: Settings = Depends(get_app_settings),
    service: TrackingService = Depends(get_tracking_service),
    scrape_result_cache: ScrapeResultCache = Depends(get_scrape_result_cache),
) -> TrackedItemBootstrapResponse:
    """Create or refresh one tracked item from a product URL.

    We run an immediate scrape so saved tracking rows always start with
    a concrete baseline price snapshot.
    """
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
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    scrape_result_ref = (payload.scrape_result_ref or "").strip() or None
    pre_fetched_result = None
    source_url = str(payload.url) if payload.url is not None else ""
    if scrape_result_ref is not None:
        pre_fetched_result = await run_in_threadpool(
            scrape_result_cache.get_result,
            scrape_result_ref,
        )
        if pre_fetched_result is None:
            raise HTTPException(
                status_code=422,
                detail="Invalid or expired scrape_result_ref",
            )
        source_url = pre_fetched_result.url

    bootstrap_input = TrackingBootstrapInput(
        request_id=request_id,
        url=source_url,
        timeout_seconds=timeout_seconds,
        selected_sites=selected_sites,
        fetch_mode=payload.fetch_mode,
        check_interval_minutes=payload.check_interval_minutes,
        target_amount=payload.target_amount,
        scrape_result_ref=scrape_result_ref,
        pre_fetched_result=pre_fetched_result,
    )

    try:
        if idempotency_key:
            item = await run_in_threadpool(
                service.bootstrap_idempotent,
                payload=bootstrap_input,
                idempotency_key=idempotency_key,
            )
        else:
            item = await run_in_threadpool(service.bootstrap, bootstrap_input)
    except TrackingBootstrapError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TrackingServiceError as exc:
        if "Idempotency-Key reuse with different payload" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return TrackedItemBootstrapResponse(
        request_id=request_id,
        item=_to_item_response(item),
    )


@router.get("", response_model=TrackedItemListResponse)
async def get_tracked_items(
    service: TrackingService = Depends(get_tracking_service),
) -> TrackedItemListResponse:
    """List tracked items with their latest known state."""
    items = await run_in_threadpool(service.list_items)
    return TrackedItemListResponse(
        items=[_to_item_response(item) for item in items]
    )


@router.get("/{item_id}", response_model=TrackedItemDetailResponse)
async def get_tracked_item(
    item_id: str,
    snapshot_limit: int | None = None,
    service: TrackingService = Depends(get_tracking_service),
) -> TrackedItemDetailResponse:
    """Return one tracked item plus recent historical snapshots."""
    detail = await run_in_threadpool(service.get_detail, item_id, snapshot_limit)
    if detail is None:
        raise HTTPException(status_code=404, detail="tracked item not found")

    return TrackedItemDetailResponse(
        item=_to_item_response(detail.item),
        recent_snapshots=[
            PriceSnapshotResponse.model_validate(asdict(snapshot))
            for snapshot in detail.recent_snapshots
        ],
    )


@router.patch("/{item_id}", response_model=TrackedItemUpdateResponse)
async def patch_tracked_item(
    item_id: str,
    payload: TrackedItemUpdateRequest,
    service: TrackingService = Depends(get_tracking_service),
) -> TrackedItemUpdateResponse:
    """Patch tracking settings without triggering a new scrape."""
    try:
        updated = await run_in_threadpool(
            service.update_item,
            TrackingUpdateInput(
                item_id=item_id,
                status=payload.status,
                check_interval_minutes=payload.check_interval_minutes,
                target_amount=payload.target_amount,
                target_amount_provided="target_amount" in payload.model_fields_set,
            ),
        )
    except TrackingServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if updated is None:
        raise HTTPException(status_code=404, detail="tracked item not found")

    return TrackedItemUpdateResponse(item=_to_item_response(updated))


@router.delete("/{item_id}", status_code=204)
async def delete_tracked_item(
    item_id: str,
    service: TrackingService = Depends(get_tracking_service),
) -> Response:
    """Delete tracked item and all linked snapshots."""
    deleted = await run_in_threadpool(service.delete_item, item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="tracked item not found")
    return Response(status_code=204)
