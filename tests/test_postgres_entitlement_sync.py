from __future__ import annotations

from typing import Any

import app.infrastructure.postgres_tracking_repository as repository_module
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository


class _FakeResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _FakeConnection:
    def __init__(self) -> None:
        self.executed_sql: list[str] = []
        self.executed_params: list[tuple[object, ...] | None] = []

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> "_FakeConnection":
        return self

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
    ) -> _FakeResult:
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        if "to_regclass(%s) IS NOT NULL AS old_exists" in sql:
            return _FakeResult({"old_exists": False, "new_exists": False})
        return _FakeResult(None)


class _FakePool:
    last_pool: "_FakePool | None" = None

    def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        self.connection_obj = _FakeConnection()
        _FakePool.last_pool = self

    def open(self, wait: bool = True) -> None:  # noqa: ARG002
        return None

    def close(self) -> None:
        return None

    def connection(self) -> _FakeConnection:
        return self.connection_obj


def _compact_statements(executed_sql: list[str]) -> str:
    return "\n".join(statement.strip() for statement in executed_sql)


def test_initialize_registers_plan_entitlement_sync_triggers(monkeypatch) -> None:
    monkeypatch.setattr(repository_module, "ConnectionPool", _FakePool)

    repository = PostgresTrackingRepository(database_url="postgresql://example.test/db")
    repository.initialize()

    pool = _FakePool.last_pool
    assert pool is not None
    executed = _compact_statements(pool.connection_obj.executed_sql)

    assert "CREATE OR REPLACE FUNCTION sync_billing_entitlement_limits_from_plan()" in executed
    assert "CREATE TRIGGER trg_sync_billing_entitlement_limits_from_plan" in executed
    assert "BEFORE INSERT OR UPDATE OF plan_key" in executed
    assert "CREATE OR REPLACE FUNCTION propagate_billing_plan_limits_to_entitlements()" in executed
    assert "CREATE TRIGGER trg_propagate_billing_plan_limits_to_entitlements" in executed
    assert "UPDATE billing_entitlements" in executed
    assert "SET\n    plan_key = plan_key" in executed


def test_launch_plan_definitions_match_product_limits() -> None:
    assert repository_module._PLAN_DEFINITIONS == (
        ("free", "Free", 5, 720, 500, "telegram", "block"),
        ("plus", "Plus", 25, 120, 10_000, "telegram", "block"),
        ("pro", "Pro", 100, 30, 50_000, "telegram", "block"),
    )


def test_sync_function_sql_copies_all_limit_fields() -> None:
    sync_function_sql = repository_module._SYNC_BILLING_ENTITLEMENT_FROM_PLAN_FUNCTION_SQL

    assert "NEW.max_active_trackers := selected_plan.max_active_trackers;" in sync_function_sql
    assert "NEW.min_check_interval_minutes := selected_plan.min_check_interval_minutes;" in sync_function_sql
    assert "NEW.monthly_check_units := selected_plan.monthly_check_units;" in sync_function_sql
    assert "NEW.allowed_channels := selected_plan.allowed_channels;" in sync_function_sql
    assert "NEW.overage_policy := selected_plan.overage_policy;" in sync_function_sql
