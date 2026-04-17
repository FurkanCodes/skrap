from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from app.core.config import get_settings
from app.infrastructure.postgres_tracking_repository import PostgresTrackingRepository

_TRACKED_ITEMS_COLUMNS = (
    "item_id",
    "source_url",
    "canonical_url",
    "product_id",
    "site_key",
    "adapter_key",
    "product_name",
    "brand",
    "model",
    "sku",
    "gtin",
    "image_url",
    "availability",
    "currency",
    "last_amount",
    "last_raw_text",
    "last_source",
    "status",
    "check_interval_minutes",
    "target_amount",
    "created_at",
    "updated_at",
    "last_checked_at",
    "next_check_at",
    "lease_owner",
    "lease_until",
    "last_check_started_at",
    "last_check_finished_at",
)

_PRICE_SNAPSHOT_COLUMNS = (
    "snapshot_id",
    "item_id",
    "checked_at",
    "scheduled_for",
    "success",
    "amount",
    "currency",
    "raw_text",
    "source",
    "error",
)

_CHECK_RUN_COLUMNS = (
    "run_id",
    "item_id",
    "scheduled_for",
    "job_key",
    "started_at",
    "finished_at",
    "status",
    "attempt",
    "error",
)

_IDEMPOTENCY_COLUMNS = (
    "idempotency_key",
    "request_hash",
    "item_id",
    "created_at",
    "updated_at",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate tracking data from SQLite to Postgres")
    parser.add_argument(
        "--sqlite-path",
        default="./data/tracking.sqlite3",
        help="Path to source SQLite DB (default: ./data/tracking.sqlite3)",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="Target Postgres URL (overrides TRACKING_DATABASE_URL/SUPABASE_DB_URL/DATABASE_URL)",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate Postgres tracking tables before import",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip post-migration row-count verification",
    )
    return parser.parse_args()


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _read_rows(
    conn: sqlite3.Connection,
    table_name: str,
    expected_columns: tuple[str, ...],
) -> list[dict[str, object]]:
    available = _table_columns(conn, table_name)
    if not available:
        return []

    selected = [column for column in expected_columns if column in available]
    if not selected:
        return []

    raw_rows = conn.execute(
        f"SELECT {', '.join(selected)} FROM {table_name}"
    ).fetchall()

    rows: list[dict[str, object]] = []
    for row in raw_rows:
        item = {column: row[column] for column in selected}
        for column in expected_columns:
            item.setdefault(column, None)
        rows.append(item)
    return rows


def _load_sqlite(sqlite_path: Path) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        tracked_items = _read_rows(conn, "tracked_items", _TRACKED_ITEMS_COLUMNS)
        snapshots = _read_rows(conn, "price_snapshots", _PRICE_SNAPSHOT_COLUMNS)
        check_runs = _read_rows(conn, "tracking_check_runs", _CHECK_RUN_COLUMNS)
        idempotency = _read_rows(
            conn,
            "tracking_bootstrap_idempotency",
            _IDEMPOTENCY_COLUMNS,
        )
    finally:
        conn.close()

    return tracked_items, snapshots, check_runs, idempotency


def _dedupe_snapshots(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Collapse duplicate (item_id, scheduled_for) rows to satisfy PG unique index."""
    keep_by_schedule: dict[tuple[str, str], dict[str, object]] = {}
    passthrough: list[dict[str, object]] = []

    for row in rows:
        item_id = str(row["item_id"])
        scheduled_for = row.get("scheduled_for")
        if scheduled_for is None:
            passthrough.append(row)
            continue

        key = (item_id, str(scheduled_for))
        existing = keep_by_schedule.get(key)
        if existing is None:
            keep_by_schedule[key] = row
            continue

        existing_success = bool(existing.get("success"))
        row_success = bool(row.get("success"))
        winner = existing
        loser = row
        if row_success and not existing_success:
            winner, loser = row, existing
            existing_success = bool(winner.get("success"))
            row_success = bool(loser.get("success"))

        success = existing_success or row_success
        replace_values = bool(loser.get("success")) or (not bool(winner.get("success")))
        if replace_values:
            winner = {
                **winner,
                "amount": loser.get("amount"),
                "currency": loser.get("currency"),
                "raw_text": loser.get("raw_text"),
                "source": loser.get("source"),
                "error": loser.get("error"),
                "checked_at": loser.get("checked_at"),
            }
        winner["success"] = success
        if success:
            winner["error"] = None
        keep_by_schedule[key] = winner

    return passthrough + list(keep_by_schedule.values())


def _dedupe_check_runs(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Collapse duplicate job_key rows to satisfy PG unique index."""
    keep_by_job: dict[str, dict[str, object]] = {}
    passthrough: list[dict[str, object]] = []

    for row in rows:
        job_key = row.get("job_key")
        if not job_key:
            passthrough.append(row)
            continue

        key = str(job_key)
        existing = keep_by_job.get(key)
        if existing is None:
            keep_by_job[key] = row
            continue

        existing_started = str(existing.get("started_at") or "")
        row_started = str(row.get("started_at") or "")
        if row_started > existing_started:
            keep_by_job[key] = row
            continue
        if row_started == existing_started:
            existing_attempt = int(existing.get("attempt") or 0)
            row_attempt = int(row.get("attempt") or 0)
            if row_attempt > existing_attempt:
                keep_by_job[key] = row

    return passthrough + list(keep_by_job.values())


def _source_counts(
    tracked_items: list[dict[str, object]],
    snapshots: list[dict[str, object]],
    check_runs: list[dict[str, object]],
    idempotency: list[dict[str, object]],
) -> dict[str, int]:
    return {
        "tracked_items": len(tracked_items),
        "price_snapshots": len(snapshots),
        "tracking_check_runs": len(check_runs),
        "tracking_bootstrap_idempotency": len(idempotency),
    }


def _target_counts(database_url: str) -> dict[str, int]:
    with psycopg.connect(database_url) as conn:
        return {
            "tracked_items": int(
                conn.execute("SELECT COUNT(*) FROM tracked_items").fetchone()[0]
            ),
            "price_snapshots": int(
                conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
            ),
            "tracking_check_runs": int(
                conn.execute("SELECT COUNT(*) FROM tracking_check_runs").fetchone()[0]
            ),
            "tracking_bootstrap_idempotency": int(
                conn.execute("SELECT COUNT(*) FROM tracking_bootstrap_idempotency").fetchone()[0]
            ),
        }


def _print_counts(label: str, counts: dict[str, int]) -> None:
    print(
        f"{label}"
        f" tracked_items={counts['tracked_items']}"
        f" snapshots={counts['price_snapshots']}"
        f" check_runs={counts['tracking_check_runs']}"
        f" idempotency={counts['tracking_bootstrap_idempotency']}"
    )


def _migrate(
    *,
    database_url: str,
    truncate: bool,
    tracked_items: list[dict[str, object]],
    snapshots: list[dict[str, object]],
    check_runs: list[dict[str, object]],
    idempotency: list[dict[str, object]],
) -> None:
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            if truncate:
                conn.execute(
                    """
                    TRUNCATE TABLE
                        tracking_bootstrap_idempotency,
                        tracking_check_runs,
                        price_snapshots,
                        tracked_items
                    RESTART IDENTITY CASCADE
                    """
                )

            if tracked_items:
                conn.executemany(
                    """
                    INSERT INTO tracked_items (
                        item_id,
                        source_url,
                        canonical_url,
                        product_id,
                        site_key,
                        adapter_key,
                        product_name,
                        brand,
                        model,
                        sku,
                        gtin,
                        image_url,
                        availability,
                        currency,
                        last_amount,
                        last_raw_text,
                        last_source,
                        status,
                        check_interval_minutes,
                        target_amount,
                        created_at,
                        updated_at,
                        last_checked_at,
                        next_check_at,
                        lease_owner,
                        lease_until,
                        last_check_started_at,
                        last_check_finished_at
                    ) VALUES (
                        %(item_id)s,
                        %(source_url)s,
                        %(canonical_url)s,
                        %(product_id)s,
                        %(site_key)s,
                        %(adapter_key)s,
                        %(product_name)s,
                        %(brand)s,
                        %(model)s,
                        %(sku)s,
                        %(gtin)s,
                        %(image_url)s,
                        %(availability)s,
                        %(currency)s,
                        %(last_amount)s,
                        %(last_raw_text)s,
                        %(last_source)s,
                        %(status)s,
                        %(check_interval_minutes)s,
                        %(target_amount)s,
                        %(created_at)s,
                        %(updated_at)s,
                        %(last_checked_at)s,
                        %(next_check_at)s,
                        %(lease_owner)s,
                        %(lease_until)s,
                        %(last_check_started_at)s,
                        %(last_check_finished_at)s
                    )
                    ON CONFLICT (source_url) DO UPDATE SET
                        canonical_url = EXCLUDED.canonical_url,
                        product_id = EXCLUDED.product_id,
                        site_key = EXCLUDED.site_key,
                        adapter_key = EXCLUDED.adapter_key,
                        product_name = EXCLUDED.product_name,
                        brand = EXCLUDED.brand,
                        model = EXCLUDED.model,
                        sku = EXCLUDED.sku,
                        gtin = EXCLUDED.gtin,
                        image_url = EXCLUDED.image_url,
                        availability = EXCLUDED.availability,
                        currency = EXCLUDED.currency,
                        last_amount = EXCLUDED.last_amount,
                        last_raw_text = EXCLUDED.last_raw_text,
                        last_source = EXCLUDED.last_source,
                        status = EXCLUDED.status,
                        check_interval_minutes = EXCLUDED.check_interval_minutes,
                        target_amount = EXCLUDED.target_amount,
                        updated_at = EXCLUDED.updated_at,
                        last_checked_at = EXCLUDED.last_checked_at,
                        next_check_at = EXCLUDED.next_check_at,
                        lease_owner = EXCLUDED.lease_owner,
                        lease_until = EXCLUDED.lease_until,
                        last_check_started_at = EXCLUDED.last_check_started_at,
                        last_check_finished_at = EXCLUDED.last_check_finished_at
                    """,
                    [
                        {
                            **row,
                            "check_interval_minutes": int(row["check_interval_minutes"] or 1),
                            "created_at": _parse_timestamp(row["created_at"]),
                            "updated_at": _parse_timestamp(row["updated_at"]),
                            "last_checked_at": _parse_timestamp(row["last_checked_at"]),
                            "next_check_at": _parse_timestamp(row["next_check_at"]),
                            "lease_until": _parse_timestamp(row["lease_until"]),
                            "last_check_started_at": _parse_timestamp(row["last_check_started_at"]),
                            "last_check_finished_at": _parse_timestamp(row["last_check_finished_at"]),
                        }
                        for row in tracked_items
                    ],
                )

            if snapshots:
                conn.executemany(
                    """
                    INSERT INTO price_snapshots (
                        snapshot_id,
                        item_id,
                        checked_at,
                        scheduled_for,
                        success,
                        amount,
                        currency,
                        raw_text,
                        source,
                        error
                    ) VALUES (
                        %(snapshot_id)s,
                        %(item_id)s,
                        %(checked_at)s,
                        %(scheduled_for)s,
                        %(success)s,
                        %(amount)s,
                        %(currency)s,
                        %(raw_text)s,
                        %(source)s,
                        %(error)s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        {
                            **row,
                            "checked_at": _parse_timestamp(row["checked_at"]),
                            "scheduled_for": _parse_timestamp(row["scheduled_for"]),
                            "success": bool(row["success"]),
                        }
                        for row in snapshots
                    ],
                )

            if check_runs:
                conn.executemany(
                    """
                    INSERT INTO tracking_check_runs (
                        run_id,
                        item_id,
                        scheduled_for,
                        job_key,
                        started_at,
                        finished_at,
                        status,
                        attempt,
                        error
                    ) VALUES (
                        %(run_id)s,
                        %(item_id)s,
                        %(scheduled_for)s,
                        %(job_key)s,
                        %(started_at)s,
                        %(finished_at)s,
                        %(status)s,
                        %(attempt)s,
                        %(error)s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        {
                            **row,
                            "scheduled_for": _parse_timestamp(row["scheduled_for"]),
                            "started_at": _parse_timestamp(row["started_at"]),
                            "finished_at": _parse_timestamp(row["finished_at"]),
                            "attempt": int(row["attempt"] or 1),
                        }
                        for row in check_runs
                    ],
                )

            if idempotency:
                conn.executemany(
                    """
                    INSERT INTO tracking_bootstrap_idempotency (
                        idempotency_key,
                        request_hash,
                        item_id,
                        created_at,
                        updated_at
                    ) VALUES (
                        %(idempotency_key)s,
                        %(request_hash)s,
                        %(item_id)s,
                        %(created_at)s,
                        %(updated_at)s
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        request_hash = EXCLUDED.request_hash,
                        item_id = EXCLUDED.item_id,
                        created_at = EXCLUDED.created_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    [
                        {
                            **row,
                            "created_at": _parse_timestamp(row["created_at"]),
                            "updated_at": _parse_timestamp(row["updated_at"]),
                        }
                        for row in idempotency
                    ],
                )


def main() -> None:
    args = _parse_args()
    sqlite_path = Path(args.sqlite_path).expanduser().resolve()

    if not sqlite_path.exists():
        raise RuntimeError(f"SQLite source DB does not exist: {sqlite_path}")

    settings = get_settings()
    database_url = args.database_url.strip() or settings.tracking_database_url
    if not database_url:
        raise RuntimeError("database URL is required via --database-url or TRACKING_DATABASE_URL")

    initializer = PostgresTrackingRepository(
        database_url=database_url,
        min_pool_size=settings.tracking_db_pool_min_size,
        max_pool_size=settings.tracking_db_pool_max_size,
    )
    initializer.initialize()
    initializer.close()

    tracked_items, snapshots, check_runs, idempotency = _load_sqlite(sqlite_path)
    deduped_snapshots = _dedupe_snapshots(snapshots)
    deduped_check_runs = _dedupe_check_runs(check_runs)
    source = _source_counts(
        tracked_items=tracked_items,
        snapshots=deduped_snapshots,
        check_runs=deduped_check_runs,
        idempotency=idempotency,
    )
    _print_counts("source_counts", source)

    _migrate(
        database_url=database_url,
        truncate=args.truncate,
        tracked_items=tracked_items,
        snapshots=deduped_snapshots,
        check_runs=deduped_check_runs,
        idempotency=idempotency,
    )

    target = _target_counts(database_url)
    _print_counts("target_counts", target)
    if not args.skip_verify and args.truncate:
        if source != target:
            raise RuntimeError(
                "verification_failed: source and target row counts differ after truncate migration"
            )
    print("migration_complete")


if __name__ == "__main__":
    main()
