from __future__ import annotations

from dataclasses import replace

from app.application.billing_service import BillingService, PaddleBillingEventInput
from app.tracking import AppUser, BillingSubscription, BillingSubscriptionApplyResult


class _FakeRepository:
    def __init__(self) -> None:
        self.user = AppUser(
            user_id="usr_test",
            auth_provider="better_auth",
            auth_subject="subject_test",
            email="test@example.com",
            display_name="Test",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.subscription: BillingSubscription | None = None
        self.applied_plan_key: str | None = None
        self.apply_result_reason: str | None = None

    def get_app_user(self, *, user_id: str) -> AppUser | None:
        return self.user if user_id == self.user.user_id else None

    def get_billing_subscription_for_user(self, *, user_id: str) -> BillingSubscription | None:  # noqa: ARG002
        return self.subscription

    def get_billing_subscription_by_provider_subscription_id(
        self,
        *,
        provider: str,  # noqa: ARG002
        provider_subscription_id: str,  # noqa: ARG002
    ) -> BillingSubscription | None:
        return self.subscription

    def apply_billing_subscription_update(self, **kwargs) -> BillingSubscriptionApplyResult:  # noqa: ANN003
        if self.apply_result_reason is not None:
            return BillingSubscriptionApplyResult(
                processed=False,
                reason=self.apply_result_reason,
                subscription=self.subscription,
            )
        self.applied_plan_key = kwargs["plan_key"]
        existing = self.subscription
        if existing is None:
            existing = BillingSubscription(
                subscription_id="sub_internal",
                user_id=kwargs["user_id"],
                provider=kwargs["provider"],
                provider_customer_id=None,
                provider_subscription_id=None,
                provider_price_id=None,
                plan_key=None,
                status="active",
                current_period_start=None,
                current_period_end=None,
                next_billed_at=None,
                cancel_at_period_end=False,
                last_event_id=None,
                last_event_occurred_at=None,
                created_at=kwargs["now"],
                updated_at=kwargs["now"],
            )
        self.subscription = replace(
            existing,
            provider_customer_id=kwargs["provider_customer_id"],
            provider_subscription_id=kwargs["provider_subscription_id"],
            provider_price_id=kwargs["provider_price_id"],
            plan_key=kwargs["plan_key"],
            status=kwargs["status"],
            current_period_start=kwargs["current_period_start"],
            current_period_end=kwargs["current_period_end"],
            next_billed_at=kwargs["next_billed_at"],
            cancel_at_period_end=kwargs["cancel_at_period_end"],
            last_event_id=kwargs["last_event_id"],
            last_event_occurred_at=kwargs["last_event_occurred_at"],
            updated_at=kwargs["now"],
        )
        return BillingSubscriptionApplyResult(
            processed=True,
            reason=None,
            subscription=self.subscription,
        )


def _event(**overrides: object) -> PaddleBillingEventInput:
    values = {
        "event_id": "evt_1",
        "event_type": "subscription.created",
        "occurred_at": "2026-01-01T00:00:00Z",
        "user_id": "usr_test",
        "provider_customer_id": "ctm_1",
        "provider_subscription_id": "sub_1",
        "provider_price_id": "pri_plus",
        "plan_key": None,
        "status": "active",
        "current_period_start": "2026-01-01T00:00:00Z",
        "current_period_end": "2026-02-01T00:00:00Z",
        "next_billed_at": "2026-02-01T00:00:00Z",
        "cancel_at_period_end": False,
    }
    values.update(overrides)
    return PaddleBillingEventInput(**values)


def test_subscription_event_applies_price_mapped_plan() -> None:
    repository = _FakeRepository()
    service = BillingService(
        repository=repository,
        paddle_plus_price_id="pri_plus",
        paddle_pro_price_id="pri_pro",
    )

    output = service.process_paddle_event(_event())

    assert output.processed is True
    assert output.subscription is not None
    assert output.subscription.plan_key == "plus"
    assert repository.applied_plan_key == "plus"


def test_canceled_subscription_falls_back_to_free() -> None:
    repository = _FakeRepository()
    service = BillingService(
        repository=repository,
        paddle_plus_price_id="pri_plus",
        paddle_pro_price_id="pri_pro",
    )

    output = service.process_paddle_event(
        _event(event_type="subscription.canceled", status="canceled")
    )

    assert output.processed is True
    assert output.subscription is not None
    assert output.subscription.plan_key == "free"
    assert repository.applied_plan_key == "free"


def test_transaction_completed_grants_active_access() -> None:
    repository = _FakeRepository()
    service = BillingService(
        repository=repository,
        paddle_plus_price_id="pri_plus",
        paddle_pro_price_id="pri_pro",
    )

    output = service.process_paddle_event(
        _event(event_type="transaction.completed", status="completed")
    )

    assert output.processed is True
    assert output.subscription is not None
    assert output.subscription.status == "active"
    assert output.subscription.plan_key == "plus"


def test_price_custom_data_mismatch_is_rejected() -> None:
    repository = _FakeRepository()
    service = BillingService(
        repository=repository,
        paddle_plus_price_id="pri_plus",
        paddle_pro_price_id="pri_pro",
    )

    output = service.process_paddle_event(_event(provider_price_id="pri_plus", plan_key="pro"))

    assert output.processed is False
    assert output.reason == "rejected_plan_mismatch"
    assert repository.subscription is None


def test_missing_price_config_does_not_apply_event() -> None:
    repository = _FakeRepository()
    service = BillingService(
        repository=repository,
        paddle_plus_price_id="",
        paddle_pro_price_id="",
    )

    output = service.process_paddle_event(_event())

    assert output.processed is False
    assert output.reason == "missing_price_config"
    assert repository.subscription is None


def test_duplicate_event_is_reported_without_reapply() -> None:
    repository = _FakeRepository()
    repository.apply_result_reason = "duplicate_event"
    service = BillingService(
        repository=repository,
        paddle_plus_price_id="pri_plus",
        paddle_pro_price_id="pri_pro",
    )

    output = service.process_paddle_event(_event())

    assert output.processed is False
    assert output.reason == "duplicate_event"
    assert repository.applied_plan_key is None


def test_stale_event_is_reported_without_reapply() -> None:
    repository = _FakeRepository()
    repository.apply_result_reason = "stale_event"
    service = BillingService(
        repository=repository,
        paddle_plus_price_id="pri_plus",
        paddle_pro_price_id="pri_pro",
    )

    output = service.process_paddle_event(_event())

    assert output.processed is False
    assert output.reason == "stale_event"
    assert repository.applied_plan_key is None
