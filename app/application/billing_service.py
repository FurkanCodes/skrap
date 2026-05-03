from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging

from app.tracking import BillingSubscription, TrackingRepository

PADDLE_PROVIDER = "paddle"
_ACTIVE_ACCESS_STATUSES = {"active", "trialing", "past_due"}
_SUPPORTED_EVENT_TYPES = {
    "subscription.created",
    "subscription.updated",
    "subscription.canceled",
    "subscription.activated",
    "subscription.trialing",
    "subscription.past_due",
    "subscription.paused",
    "subscription.resumed",
    "transaction.completed",
}
_PLAN_KEYS = {"plus", "pro"}
_REJECTED_PLAN_MISMATCH = "rejected_plan_mismatch"
_MISSING_PRICE_CONFIG = "missing_price_config"
_logger = logging.getLogger("scraper_api.billing")


class BillingServiceError(ValueError):
    """Raised when a billing event cannot be safely applied."""


@dataclass(frozen=True)
class PaddleBillingEventInput:
    event_id: str
    event_type: str
    occurred_at: str
    user_id: str | None
    provider_customer_id: str | None
    provider_subscription_id: str | None
    provider_price_id: str | None
    plan_key: str | None
    status: str | None
    current_period_start: str | None
    current_period_end: str | None
    next_billed_at: str | None
    cancel_at_period_end: bool


@dataclass(frozen=True)
class PaddleBillingEventOutput:
    processed: bool
    reason: str | None
    subscription: BillingSubscription | None


class BillingService:
    """Applies billing provider events to app entitlements."""

    def __init__(
        self,
        repository: TrackingRepository,
        *,
        paddle_plus_price_id: str,
        paddle_pro_price_id: str,
    ) -> None:
        self._repository = repository
        self._has_price_config = bool(
            paddle_plus_price_id.strip() and paddle_pro_price_id.strip()
        )
        self._plan_by_price_id = {
            price_id: plan_key
            for price_id, plan_key in (
                (paddle_plus_price_id.strip(), "plus"),
                (paddle_pro_price_id.strip(), "pro"),
            )
            if price_id
        }

    def get_current_subscription(self, *, user_id: str) -> BillingSubscription | None:
        return self._repository.get_billing_subscription_for_user(user_id=user_id)

    def process_paddle_event(
        self,
        event: PaddleBillingEventInput,
    ) -> PaddleBillingEventOutput:
        if event.event_type not in _SUPPORTED_EVENT_TYPES:
            _log_billing_event(event, processed=False, reason="unsupported_event_type")
            return PaddleBillingEventOutput(
                processed=False,
                reason="unsupported_event_type",
                subscription=None,
            )
        if not event.provider_subscription_id:
            _log_billing_event(event, processed=False, reason="missing_subscription_id")
            return PaddleBillingEventOutput(
                processed=False,
                reason="missing_subscription_id",
                subscription=None,
            )
        if not self._has_price_config:
            _log_billing_event(event, processed=False, reason=_MISSING_PRICE_CONFIG)
            return PaddleBillingEventOutput(
                processed=False,
                reason=_MISSING_PRICE_CONFIG,
                subscription=None,
            )

        existing = self._repository.get_billing_subscription_by_provider_subscription_id(
            provider=PADDLE_PROVIDER,
            provider_subscription_id=event.provider_subscription_id,
        )
        user_id = event.user_id
        if not user_id and existing is not None:
            user_id = existing.user_id
        if not user_id:
            _log_billing_event(event, processed=False, reason="missing_user_id")
            return PaddleBillingEventOutput(
                processed=False,
                reason="missing_user_id",
                subscription=None,
            )
        if self._repository.get_app_user(user_id=user_id) is None:
            raise BillingServiceError("billing user does not exist")

        provider_price_id = event.provider_price_id
        if not provider_price_id and existing is not None:
            provider_price_id = existing.provider_price_id
        plan_key_result = self._resolve_plan_key(
            price_id=provider_price_id,
            fallback=event.plan_key or (existing.plan_key if existing else None),
        )
        if plan_key_result.reason:
            _log_billing_event(event, processed=False, reason=plan_key_result.reason)
            return PaddleBillingEventOutput(
                processed=False,
                reason=plan_key_result.reason,
                subscription=existing,
            )
        plan_key = plan_key_result.plan_key
        status = self._resolve_status(event=event, existing=existing)
        effective_plan_key = plan_key if status in _ACTIVE_ACCESS_STATUSES else "free"

        result = self._repository.apply_billing_subscription_update(
            user_id=user_id,
            provider=PADDLE_PROVIDER,
            provider_customer_id=event.provider_customer_id
            or (existing.provider_customer_id if existing else None),
            provider_subscription_id=event.provider_subscription_id,
            provider_price_id=provider_price_id,
            plan_key=effective_plan_key,
            status=status,
            current_period_start=event.current_period_start
            or (existing.current_period_start if existing else None),
            current_period_end=event.current_period_end
            or (existing.current_period_end if existing else None),
            next_billed_at=event.next_billed_at or (existing.next_billed_at if existing else None),
            cancel_at_period_end=event.cancel_at_period_end,
            last_event_id=event.event_id,
            last_event_occurred_at=event.occurred_at,
            now=_utcnow_iso(),
        )
        _log_billing_event(
            event,
            processed=result.processed,
            reason=result.reason,
            plan_key=effective_plan_key,
            status=status,
        )
        return PaddleBillingEventOutput(
            processed=result.processed,
            reason=result.reason,
            subscription=result.subscription,
        )

    def _resolve_plan_key(
        self,
        *,
        price_id: str | None,
        fallback: str | None,
    ) -> "_PlanKeyResolution":
        mapped_plan_key = self._plan_by_price_id.get(price_id or "")
        fallback_plan_key = fallback if fallback in _PLAN_KEYS else None
        if mapped_plan_key and fallback_plan_key and mapped_plan_key != fallback_plan_key:
            return _PlanKeyResolution(plan_key="free", reason=_REJECTED_PLAN_MISMATCH)
        if mapped_plan_key:
            return _PlanKeyResolution(plan_key=mapped_plan_key, reason=None)
        if fallback_plan_key:
            return _PlanKeyResolution(plan_key=fallback_plan_key, reason=None)
        return _PlanKeyResolution(plan_key="free", reason=None)

    @staticmethod
    def _resolve_status(
        *,
        event: PaddleBillingEventInput,
        existing: BillingSubscription | None,
    ) -> str:
        if event.event_type == "transaction.completed":
            return existing.status if existing is not None else "active"
        if event.event_type == "subscription.canceled":
            return "canceled"
        if event.status:
            return event.status
        if existing is not None:
            return existing.status
        return "active"


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class _PlanKeyResolution:
    plan_key: str
    reason: str | None


def _log_billing_event(
    event: PaddleBillingEventInput,
    *,
    processed: bool,
    reason: str | None,
    plan_key: str | None = None,
    status: str | None = None,
) -> None:
    _logger.info(
        "paddle_event_result event_id=%s event_type=%s user_id=%s subscription_id=%s "
        "plan_key=%s status=%s processed=%s reason=%s",
        event.event_id,
        event.event_type,
        event.user_id,
        event.provider_subscription_id,
        plan_key or event.plan_key,
        status or event.status,
        processed,
        reason,
    )
