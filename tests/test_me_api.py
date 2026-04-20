from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.api.dependencies import get_current_principal, get_user_service
from app.main import app
from app.application.user_service import MeOutput, MeUsageOutput
from app.core.auth import AuthPrincipal
from app.tracking import AppUser, UserEntitlement


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _build_user(now: str) -> AppUser:
    return AppUser(
        user_id="usr_test",
        auth_provider="better_auth",
        auth_subject="subject_test",
        email="test@example.com",
        display_name="Test",
        created_at=now,
        updated_at=now,
    )


def _build_entitlement(now: str) -> UserEntitlement:
    return UserEntitlement(
        user_id="usr_test",
        plan_key="free",
        max_active_trackers=5,
        min_check_interval_minutes=180,
        monthly_check_units=1500,
        allowed_channels="telegram",
        overage_policy="block",
        created_at=now,
        updated_at=now,
    )


@dataclass
class _FakeUserService:
    usage_sequence: list[MeUsageOutput]
    usage_calls: int = 0

    def get_me(self, principal) -> MeOutput:  # noqa: ANN001
        now = _iso(datetime.now(tz=UTC))
        return MeOutput(
            user=_build_user(now),
            entitlement=_build_entitlement(now),
            usage=self.usage_sequence[min(self.usage_calls, len(self.usage_sequence) - 1)],
        )

    def get_usage(self, principal) -> MeUsageOutput:  # noqa: ANN001
        index = min(self.usage_calls, len(self.usage_sequence) - 1)
        usage = self.usage_sequence[index]
        self.usage_calls += 1
        return usage


@dataclass
class _FakeRequest:
    max_checks: int
    checks: int = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.max_checks


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def _override_principal() -> AuthPrincipal:
    return AuthPrincipal(
        subject="subject_test",
        email="test@example.com",
        name="Test",
        issuer="https://auth.example.com",
        audience="web",
    )


def test_me_endpoint_includes_usage_updated_at() -> None:
    now = datetime.now(tz=UTC)
    usage = MeUsageOutput(
        user_id="usr_test",
        period_start=_iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
        period_end=_iso((now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=31))),
        monthly_check_units=1500,
        consumed_check_units=10,
        remaining_check_units=1490,
        updated_at=_iso(now),
    )
    app.dependency_overrides[get_user_service] = lambda: _FakeUserService([usage])
    app.dependency_overrides[get_current_principal] = _override_principal
    client = TestClient(app)

    response = client.get("/api/v1/me")
    _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["usage"]["updated_at"] == usage.updated_at


def test_me_usage_stream_emits_events_only_on_usage_change(monkeypatch) -> None:
    from app.api.routers import me as me_router

    monkeypatch.setattr(me_router, "USAGE_STREAM_POLL_SECONDS", 0.0)
    monkeypatch.setattr(me_router, "USAGE_STREAM_HEARTBEAT_SECONDS", 60.0)

    now = datetime.now(tz=UTC)
    usage_a = MeUsageOutput(
        user_id="usr_test",
        period_start=_iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
        period_end=_iso((now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=31))),
        monthly_check_units=1500,
        consumed_check_units=1,
        remaining_check_units=1499,
        updated_at=_iso(now),
    )
    usage_b = MeUsageOutput(
        user_id="usr_test",
        period_start=usage_a.period_start,
        period_end=usage_a.period_end,
        monthly_check_units=1500,
        consumed_check_units=2,
        remaining_check_units=1498,
        updated_at=_iso(now + timedelta(seconds=5)),
    )
    async def _collect() -> list[str]:
        stream = me_router._usage_stream(
            request=_FakeRequest(max_checks=4),
            principal=_override_principal(),
            service=_FakeUserService([usage_a, usage_a, usage_b, usage_b]),
        )
        chunks: list[str] = []
        async for chunk in stream:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    usage_events = [chunk for chunk in chunks if chunk.startswith("event: usage")]

    assert len(usage_events) == 2
    assert '"consumed_check_units":1' in usage_events[0]
    assert '"consumed_check_units":2' in usage_events[1]


def test_me_usage_stream_emits_heartbeat_when_usage_unchanged(monkeypatch) -> None:
    from app.api.routers import me as me_router

    monkeypatch.setattr(me_router, "USAGE_STREAM_POLL_SECONDS", 0.0)
    monkeypatch.setattr(me_router, "USAGE_STREAM_HEARTBEAT_SECONDS", 0.0)

    now = datetime.now(tz=UTC)
    stable = MeUsageOutput(
        user_id="usr_test",
        period_start=_iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
        period_end=_iso((now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=31))),
        monthly_check_units=1500,
        consumed_check_units=3,
        remaining_check_units=1497,
        updated_at=_iso(now),
    )
    async def _collect() -> list[str]:
        stream = me_router._usage_stream(
            request=_FakeRequest(max_checks=3),
            principal=_override_principal(),
            service=_FakeUserService([stable, stable, stable, stable]),
        )
        chunks: list[str] = []
        async for chunk in stream:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())

    assert any(chunk.startswith(": heartbeat") for chunk in chunks)
