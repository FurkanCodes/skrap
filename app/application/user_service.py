from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.auth import AuthPrincipal
from app.tracking import AppUser, TrackingRepository, UserEntitlement


@dataclass(frozen=True)
class MeOutput:
    """Resolved current-user profile plus effective entitlement limits."""

    user: AppUser
    entitlement: UserEntitlement


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

    def get_me(self, principal: AuthPrincipal) -> MeOutput:
        user = self.provision_from_principal(principal)
        entitlement = self._repository.get_user_entitlement(user_id=user.user_id)
        if entitlement is None:
            now = _utcnow_iso()
            self._repository.ensure_default_user_entitlement(user_id=user.user_id, now=now)
            entitlement = self._repository.get_user_entitlement(user_id=user.user_id)
            if entitlement is None:  # pragma: no cover - defensive guard
                raise RuntimeError("Failed to resolve default user entitlement")
        return MeOutput(user=user, entitlement=entitlement)


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")

