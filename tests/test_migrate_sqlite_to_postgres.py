from __future__ import annotations

import sqlite3
from pathlib import Path

from app.jobs.migrate_sqlite_to_postgres import _load_sqlite, _source_counts


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def test_load_sqlite_reads_extended_tracking_tables(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "tracking.sqlite3"
    conn = _connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE tracked_items (
                item_id TEXT,
                source_url TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE price_snapshots (
                snapshot_id TEXT,
                item_id TEXT,
                checked_at TEXT,
                success INTEGER
            );
            CREATE TABLE tracking_check_runs (
                run_id TEXT,
                item_id TEXT,
                scheduled_for TEXT,
                started_at TEXT,
                status TEXT,
                attempt INTEGER
            );
            CREATE TABLE tracking_bootstrap_idempotency (
                idempotency_key TEXT,
                request_hash TEXT,
                item_id TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE alert_events (
                event_id TEXT,
                idempotency_key TEXT,
                item_id TEXT,
                snapshot_id TEXT,
                rule_type TEXT,
                current_amount TEXT,
                target_amount TEXT,
                triggered_at TEXT
            );
            CREATE TABLE notification_events (
                notification_id TEXT,
                idempotency_key TEXT,
                alert_event_id TEXT,
                item_id TEXT,
                channel TEXT,
                payload TEXT,
                status TEXT,
                attempt_count INTEGER,
                max_attempts INTEGER,
                next_attempt_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO tracked_items (item_id, source_url, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("trk_1", "https://example.com/a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO price_snapshots (snapshot_id, item_id, checked_at, success) VALUES (?, ?, ?, ?)",
            ("snp_1", "trk_1", "2026-01-01T00:01:00Z", 1),
        )
        conn.execute(
            "INSERT INTO tracking_check_runs (run_id, item_id, scheduled_for, started_at, status, attempt) VALUES (?, ?, ?, ?, ?, ?)",
            ("run_1", "trk_1", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", "succeeded", 1),
        )
        conn.execute(
            "INSERT INTO tracking_bootstrap_idempotency (idempotency_key, request_hash, item_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("key_1", "hash_1", "trk_1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO alert_events (event_id, idempotency_key, item_id, snapshot_id, rule_type, current_amount, target_amount, triggered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("aev_1", "aev_key_1", "trk_1", "snp_1", "price_below", "95.00", "100.00", "2026-01-01T00:01:00Z"),
        )
        conn.execute(
            "INSERT INTO notification_events (notification_id, idempotency_key, alert_event_id, item_id, channel, payload, status, attempt_count, max_attempts, next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "nev_1",
                "nev_key_1",
                "aev_1",
                "trk_1",
                "log",
                '{"ok":true}',
                "queued",
                0,
                5,
                "2026-01-01T00:01:00Z",
                "2026-01-01T00:01:00Z",
                "2026-01-01T00:01:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    (
        tracked_items,
        snapshots,
        check_runs,
        idempotency,
        alert_events,
        notification_events,
    ) = _load_sqlite(sqlite_path)
    counts = _source_counts(
        tracked_items=tracked_items,
        snapshots=snapshots,
        check_runs=check_runs,
        idempotency=idempotency,
        alert_events=alert_events,
        notification_events=notification_events,
    )

    assert counts == {
        "tracked_items": 1,
        "price_snapshots": 1,
        "tracking_check_runs": 1,
        "tracking_bootstrap_idempotency": 1,
        "alert_events": 1,
        "notification_events": 1,
    }


def test_load_sqlite_handles_missing_optional_tables(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "tracking.sqlite3"
    conn = _connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE tracked_items (
                item_id TEXT,
                source_url TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO tracked_items (item_id, source_url, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("trk_1", "https://example.com/a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    (
        tracked_items,
        snapshots,
        check_runs,
        idempotency,
        alert_events,
        notification_events,
    ) = _load_sqlite(sqlite_path)

    assert len(tracked_items) == 1
    assert snapshots == []
    assert check_runs == []
    assert idempotency == []
    assert alert_events == []
    assert notification_events == []
