from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.auth import AuthPrincipal
from app.tracking import AppUser, TrackingRepository, UserEntitlement, UsageCounter


@dataclass(frozen=True)
class MeOutput:
    """Resolved current-user profile plus effective entitlement limits."""

    user: AppUser
    entitlement: UserEntitlement
    usage: "MeUsageOutput"


@dataclass(frozen=True)
class MeUsageOutput:
    """Current monthly check-unit usage for one user."""

    user_id: str
    period_start: str
    period_end: str
    monthly_check_units: int
    consumed_check_units: int
    remaining_check_units: int
    updated_at: str


class UserService:
    """Owns app-level user provisioning mapped from auth principals."""

    def __init__(self, repository: TrackingRepository) -> None:
        self._repository = repository

    def provision_from_principal(self, principal: AuthPrincipal) -> AppUser:
        now = _utcnow_iso()
        user = self._repository.upsert_app_user(
            auth_provider=principal.provider,
            auth_subject=principal.subject,
            email=principal.email,
            display_name=principal.name,
            now=now,
        )
        self._repository.ensure_default_user_entitlement(user_id=user.user_id, now=now)
        return user

    def resolve_or_provision_user(self, principal: AuthPrincipal) -> AppUser:
        user = self._repository.get_app_user_by_auth_subject(
            auth_provider=principal.provider,
            auth_subject=principal.subject,
        )
        if user is not None:
            return user
        return self.provision_from_principal(principal)

    def get_me(self, principal: AuthPrincipal) -> MeOutput:
        now = _utcnow_iso()
        user = self.resolve_or_provision_user(principal)
        entitlement = _resolve_user_entitlement(
            repository=self._repository,
            user_id=user.user_id,
            now=now,
        )
        usage = _resolve_me_usage(
            repository=self._repository,
            user_id=user.user_id,
            monthly_check_units=entitlement.monthly_check_units,
            now=now,
        )
        return MeOutput(user=user, entitlement=entitlement, usage=usage)

    def get_usage(self, principal: AuthPrincipal) -> MeUsageOutput:
        now = _utcnow_iso()
        user = self.resolve_or_provision_user(principal)
        entitlement = _resolve_user_entitlement(
            repository=self._repository,
            user_id=user.user_id,
            now=now,
        )
        return _resolve_me_usage(
            repository=self._repository,
            user_id=user.user_id,
            monthly_check_units=entitlement.monthly_check_units,
            now=now,
        )


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _resolve_me_usage(
    *,
    repository: TrackingRepository,
    user_id: str,
    monthly_check_units: int,
    now: str,
) -> MeUsageOutput:
    current_counter = _get_current_usage_counter(
        repository=repository,
        user_id=user_id,
        now=now,
    )
    consumed = current_counter.consumed_check_units
    remaining = max(0, monthly_check_units - consumed)
    return MeUsageOutput(
        user_id=user_id,
        period_start=current_counter.period_start,
        period_end=current_counter.period_end,
        monthly_check_units=monthly_check_units,
        consumed_check_units=consumed,
        remaining_check_units=remaining,
        updated_at=current_counter.updated_at,
    )


def _resolve_user_entitlement(
    *,
    repository: TrackingRepository,
    user_id: str,
    now: str,
) -> UserEntitlement:
    entitlement = repository.get_user_entitlement(user_id=user_id)
    if entitlement is not None:
        return entitlement

    repository.ensure_default_user_entitlement(user_id=user_id, now=now)
    entitlement = repository.get_user_entitlement(user_id=user_id)
    if entitlement is None:  # pragma: no cover - defensive guard
        raise RuntimeError("Failed to resolve default user entitlement")
    return entitlement


def _get_current_usage_counter(
    *,
    repository: TrackingRepository,
    user_id: str,
    now: str,
) -> UsageCounter:
    usage_method = getattr(repository, "get_current_usage_counter", None)
    if callable(usage_method):
        return usage_method(user_id=user_id, now=now)

    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(UTC)
    period_start, period_end = _usage_period_bounds(now_dt)
    return UsageCounter(
        counter_id=f"usc_fallback_{user_id}",
        user_id=user_id,
        period_start=period_start,
        period_end=period_end,
        consumed_check_units=0,
        created_at=now,
        updated_at=now,
    )


def _usage_period_bounds(now: datetime) -> tuple[str, str]:
    period_start = datetime(year=now.year, month=now.month, day=1, tzinfo=UTC)
    if now.month == 12:
        period_end = datetime(year=now.year + 1, month=1, day=1, tzinfo=UTC)
    else:
        period_end = datetime(year=now.year, month=now.month + 1, day=1, tzinfo=UTC)
    return (
        period_start.isoformat().replace("+00:00", "Z"),
        period_end.isoformat().replace("+00:00", "Z"),
    )
