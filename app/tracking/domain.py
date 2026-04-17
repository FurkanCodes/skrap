from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TrackingStatus = Literal["active", "paused"]
TrackingMode = Literal["monitor", "alert"]


def derive_tracking_mode(target_amount: str | None) -> TrackingMode:
    """Infer tracking mode from target amount presence."""
    if target_amount is None:
        return "monitor"
    return "alert" if target_amount.strip() else "monitor"


@dataclass(frozen=True)
class TrackedItem:
    """Persisted product tracking aggregate."""

    item_id: str
    source_url: str
    canonical_url: str | None
    product_id: str | None
    site_key: str | None
    adapter_key: str | None
    product_name: str | None
    brand: str | None
    model: str | None
    sku: str | None
    gtin: str | None
    image_url: str | None
    availability: str | None
    currency: str | None
    last_amount: str | None
    last_raw_text: str | None
    last_source: str | None
    status: TrackingStatus
    check_interval_minutes: int
    target_amount: str | None
    created_at: str
    updated_at: str
    last_checked_at: str | None

    @property
    def tracking_mode(self) -> TrackingMode:
        """`monitor` when no target is set, `alert` when target is set."""
        return derive_tracking_mode(self.target_amount)

    @property
    def alerts_enabled(self) -> bool:
        """Convenience flag for clients that only need a boolean."""
        return self.tracking_mode == "alert"


@dataclass(frozen=True)
class PriceSnapshot:
    """Historical snapshot of one tracking check execution."""

    snapshot_id: str
    item_id: str
    checked_at: str
    success: bool
    amount: str | None
    currency: str | None
    raw_text: str | None
    source: str | None
    error: str | None
