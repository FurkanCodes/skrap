from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.user_service import UserService
from app.core.auth import AuthPrincipal
from app.tracking import AppUser, UsageCounter, UserEntitlement


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _usage_period_bounds(now_iso: str) -> tuple[str, str]:
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(UTC)
    period_start = datetime(year=now.year, month=now.month, day=1, tzinfo=UTC)
    if now.month == 12:
        period_end = datetime(year=now.year + 1, month=1, day=1, tzinfo=UTC)
    else:
        period_end = datetime(year=now.year, month=now.month + 1, day=1, tzinfo=UTC)
    return (
        period_start.isoformat().replace("+00:00", "Z"),
        period_end.isoformat().replace("+00:00", "Z"),
    )


@dataclass
class _FakeUserRepository:
    user: AppUser | None = None
    entitlement: UserEntitlement | None = None
    consumed_units: int = 0
    upsert_calls: int = 0

    def upsert_app_user(
        self,
        *,
        auth_provider: str,
        auth_subject: str,
        email: str | None,
        display_name: str | None,
        now: str,
    ) -> AppUser:
        self.upsert_calls += 1
        self.user = AppUser(
            user_id=f"usr_{auth_subject}",
            auth_provider=auth_provider,
            auth_subject=auth_subject,
            email=email,
            display_name=display_name,
            created_at=now,
            updated_at=now,
        )
        return self.user

    def get_app_user_by_auth_subject(
        self,
        *,
        auth_provider: str,
        auth_subject: str,
    ) -> AppUser | None:
        if self.user is None:
            return None
        if self.user.auth_provider != auth_provider:
            return None
        if self.user.auth_subject != auth_subject:
            return None
        return self.user

    def ensure_default_user_entitlement(self, *, user_id: str, now: str) -> None:
        if self.entitlement is not None:
            return
        self.entitlement = UserEntitlement(
            user_id=user_id,
            plan_key="free",
            max_active_trackers=5,
            min_check_interval_minutes=180,
            monthly_check_units=1500,
            allowed_channels="telegram",
            overage_policy="block",
            created_at=now,
            updated_at=now,
        )

    def get_user_entitlement(self, *, user_id: str) -> UserEntitlement | None:  # noqa: ARG002
        return self.entitlement

    def get_current_usage_counter(self, *, user_id: str, now: str) -> UsageCounter:
        period_start, period_end = _usage_period_bounds(now)
        return UsageCounter(
            counter_id="usc_test",
            user_id=user_id,
            period_start=period_start,
            period_end=period_end,
            consumed_check_units=self.consumed_units,
            created_at=now,
            updated_at=now,
        )


def test_user_service_get_me_includes_credit_usage() -> None:
    repository = _FakeUserRepository(consumed_units=42)
    service = UserService(repository=repository)
    principal = AuthPrincipal(
        subject="subject_1",
        email="hello@example.com",
        name="Hello",
        issuer="https://auth.example.com",
        audience="web",
    )

    output = service.get_me(principal)

    assert output.entitlement.monthly_check_units == 1500
    assert output.usage.consumed_check_units == 42
    assert output.usage.remaining_check_units == 1458
    assert output.usage.user_id == output.user.user_id
    assert output.usage.updated_at


def test_user_service_get_me_reuses_existing_user_without_upsert() -> None:
    now = _iso(datetime.now(tz=UTC))
    existing = AppUser(
        user_id="usr_existing",
        auth_provider="better_auth",
        auth_subject="subject_1",
        email="existing@example.com",
        display_name="Existing",
        created_at=now,
        updated_at=now,
    )
    repository = _FakeUserRepository(user=existing, consumed_units=1)
    repository.entitlement = UserEntitlement(
        user_id=existing.user_id,
        plan_key="free",
        max_active_trackers=5,
        min_check_interval_minutes=180,
        monthly_check_units=1500,
        allowed_channels="telegram",
        overage_policy="block",
        created_at=now,
        updated_at=now,
    )
    service = UserService(repository=repository)
    principal = AuthPrincipal(
        subject="subject_1",
        email="updated@example.com",
        name="Updated",
        issuer="https://auth.example.com",
        audience="web",
    )

    output = service.get_me(principal)

    assert output.user.user_id == existing.user_id
    assert repository.upsert_calls == 0
