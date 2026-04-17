from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.tracking.domain import (
    PriceSnapshot,
    TrackingBootstrapIdempotencyRecord,
    TrackedItem,
    TrackingCheckRun,
    TrackingCheckRunStatus,
)


class SQLiteTrackingRepository:
    """SQLite-backed persistence for tracked items, snapshots, and scheduler runs.

    Storage model:
    - `tracked_items` keeps latest state per source URL and scheduler lease fields.
    - `price_snapshots` is append-only history per tracked item.
    - `tracking_check_runs` records each scheduler execution attempt.
    """

    def __init__(self, db_path: str) -> None:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)

        self._db_path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        """Create tables/indexes once at startup and backfill missing columns."""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracked_items (
                    item_id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL UNIQUE,
                    canonical_url TEXT,
                    product_id TEXT,
                    site_key TEXT,
                    adapter_key TEXT,
                    product_name TEXT,
                    brand TEXT,
                    model TEXT,
                    sku TEXT,
                    gtin TEXT,
                    image_url TEXT,
                    availability TEXT,
                    currency TEXT,
                    last_amount TEXT,
                    last_raw_text TEXT,
                    last_source TEXT,
                    status TEXT NOT NULL,
                    check_interval_minutes INTEGER NOT NULL,
                    target_amount TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    next_check_at TEXT,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_check_started_at TEXT,
                    last_check_finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS price_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    amount TEXT,
                    currency TEXT,
                    raw_text TEXT,
                    source TEXT,
                    error TEXT,
                    FOREIGN KEY (item_id) REFERENCES tracked_items(item_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS tracking_check_runs (
                    run_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    error TEXT,
                    FOREIGN KEY (item_id) REFERENCES tracked_items(item_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS tracking_bootstrap_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    item_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (item_id) REFERENCES tracked_items(item_id) ON DELETE SET NULL
                );
                """
            )
            self._ensure_tracked_items_columns()
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_tracked_items_status_next
                  ON tracked_items(status, next_check_at);

                CREATE INDEX IF NOT EXISTS idx_tracked_items_lease_until
                  ON tracked_items(lease_until);

                CREATE INDEX IF NOT EXISTS idx_price_snapshots_item_checked
                  ON price_snapshots(item_id, checked_at DESC);

                CREATE INDEX IF NOT EXISTS idx_check_runs_item_started
                  ON tracking_check_runs(item_id, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_check_runs_item_scheduled
                  ON tracking_check_runs(item_id, scheduled_for, attempt DESC);

                CREATE INDEX IF NOT EXISTS idx_bootstrap_idempotency_item
                  ON tracking_bootstrap_idempotency(item_id);
                """
            )
            self._conn.commit()

    def list_items(self) -> list[TrackedItem]:
        """List tracked items with most recently updated first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM tracked_items
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_item(self, item_id: str) -> TrackedItem | None:
        """Load one tracked item by stable item ID."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM tracked_items
                WHERE item_id = ?
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def get_item_by_source_url(self, source_url: str) -> TrackedItem | None:
        """Load one tracked item using canonical tracking key: source URL."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM tracked_items
                WHERE source_url = ?
                """,
                (source_url,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def save_item(self, item: TrackedItem) -> TrackedItem:
        """Insert or update tracked item state."""
        with self._lock:
            self._upsert_item_locked(item)
            self._conn.commit()

        current = self.get_item_by_source_url(item.source_url)
        if current is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to persist tracked item")
        return current

    def save_item_and_snapshot(self, item: TrackedItem, snapshot: PriceSnapshot) -> TrackedItem:
        """Atomically persist latest item state and an append-only snapshot."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._upsert_item_locked(item)
                self._insert_snapshot_locked(snapshot)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        current = self.get_item(item.item_id)
        if current is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to persist tracking check result")
        return current

    def delete_item(self, item_id: str) -> bool:
        """Delete one tracked item; snapshots/runs are removed via FK cascade."""
        with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM tracked_items
                WHERE item_id = ?
                """,
                (item_id,),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        """Append one immutable snapshot row for historical analysis."""
        with self._lock:
            self._insert_snapshot_locked(snapshot)
            self._conn.commit()

    def list_snapshots(self, item_id: str, limit: int) -> list[PriceSnapshot]:
        """Return most recent snapshots first with caller-provided cap."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM price_snapshots
                WHERE item_id = ?
                ORDER BY checked_at DESC
                LIMIT ?
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    def claim_due_items(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[TrackedItem]:
        """Claim due active items by assigning a lease to this scheduler owner."""
        if limit < 1:
            return []

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT item_id
                    FROM tracked_items
                    WHERE status = 'active'
                      AND (next_check_at IS NULL OR next_check_at <= ?)
                      AND (lease_until IS NULL OR lease_until <= ?)
                    ORDER BY COALESCE(next_check_at, updated_at, created_at) ASC, item_id ASC
                    LIMIT ?
                    """,
                    (now, now, limit),
                ).fetchall()
                item_ids = [str(row["item_id"]) for row in rows]
                if not item_ids:
                    self._conn.commit()
                    return []

                placeholders = ",".join("?" for _ in item_ids)
                self._conn.execute(
                    f"""
                    UPDATE tracked_items
                    SET lease_owner = ?,
                        lease_until = ?,
                        last_check_started_at = ?,
                        updated_at = ?
                    WHERE item_id IN ({placeholders})
                    """,
                    (lease_owner, lease_until, now, now, *item_ids),
                )

                claimed_rows = self._conn.execute(
                    f"""
                    SELECT *
                    FROM tracked_items
                    WHERE item_id IN ({placeholders})
                    """,
                    tuple(item_ids),
                ).fetchall()
                by_id = {str(row["item_id"]): self._row_to_item(row) for row in claimed_rows}
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return [by_id[item_id] for item_id in item_ids if item_id in by_id]

    def start_check_run(
        self,
        *,
        run_id: str,
        item_id: str,
        scheduled_for: str,
        started_at: str,
    ) -> TrackingCheckRun:
        """Insert a run row and increment attempt count for same item+schedule."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) AS max_attempt
                FROM tracking_check_runs
                WHERE item_id = ?
                  AND scheduled_for = ?
                """,
                (item_id, scheduled_for),
            ).fetchone()
            attempt = int((row["max_attempt"] if row is not None else 0) or 0) + 1

            self._conn.execute(
                """
                INSERT INTO tracking_check_runs (
                    run_id,
                    item_id,
                    scheduled_for,
                    started_at,
                    finished_at,
                    status,
                    attempt,
                    error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, item_id, scheduled_for, started_at, None, "running", attempt, None),
            )
            self._conn.commit()

        return TrackingCheckRun(
            run_id=run_id,
            item_id=item_id,
            scheduled_for=scheduled_for,
            started_at=started_at,
            finished_at=None,
            status="running",
            attempt=attempt,
            error=None,
        )

    def finish_check_run(
        self,
        *,
        run_id: str,
        status: TrackingCheckRunStatus,
        finished_at: str,
        error: str | None,
    ) -> None:
        """Mark a scheduler run as completed with final status/error."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE tracking_check_runs
                SET status = ?,
                    finished_at = ?,
                    error = ?
                WHERE run_id = ?
                """,
                (status, finished_at, error, run_id),
            )
            self._conn.commit()

    def list_check_runs(self, item_id: str, limit: int) -> list[TrackingCheckRun]:
        """Return recent check runs for one item."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM tracking_check_runs
                WHERE item_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_check_run(row) for row in rows]

    def get_bootstrap_idempotency_record(
        self,
        idempotency_key: str,
    ) -> TrackingBootstrapIdempotencyRecord | None:
        """Load saved bootstrap idempotency key metadata."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM tracking_bootstrap_idempotency
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_bootstrap_idempotency(row)

    def save_bootstrap_idempotency_record(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        """Persist final idempotency result for tracked-item bootstrap."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tracking_bootstrap_idempotency (
                    idempotency_key,
                    request_hash,
                    item_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    request_hash = excluded.request_hash,
                    item_id = excluded.item_id,
                    updated_at = excluded.updated_at
                """,
                (idempotency_key, request_hash, item_id, now, now),
            )
            self._conn.commit()

    def _upsert_item_locked(self, item: TrackedItem) -> None:
        self._conn.execute(
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
                :item_id,
                :source_url,
                :canonical_url,
                :product_id,
                :site_key,
                :adapter_key,
                :product_name,
                :brand,
                :model,
                :sku,
                :gtin,
                :image_url,
                :availability,
                :currency,
                :last_amount,
                :last_raw_text,
                :last_source,
                :status,
                :check_interval_minutes,
                :target_amount,
                :created_at,
                :updated_at,
                :last_checked_at,
                :next_check_at,
                :lease_owner,
                :lease_until,
                :last_check_started_at,
                :last_check_finished_at
            )
            ON CONFLICT(source_url) DO UPDATE SET
                canonical_url = excluded.canonical_url,
                product_id = excluded.product_id,
                site_key = excluded.site_key,
                adapter_key = excluded.adapter_key,
                product_name = excluded.product_name,
                brand = excluded.brand,
                model = excluded.model,
                sku = excluded.sku,
                gtin = excluded.gtin,
                image_url = excluded.image_url,
                availability = excluded.availability,
                currency = excluded.currency,
                last_amount = excluded.last_amount,
                last_raw_text = excluded.last_raw_text,
                last_source = excluded.last_source,
                status = excluded.status,
                check_interval_minutes = excluded.check_interval_minutes,
                target_amount = excluded.target_amount,
                updated_at = excluded.updated_at,
                last_checked_at = excluded.last_checked_at,
                next_check_at = excluded.next_check_at,
                lease_owner = excluded.lease_owner,
                lease_until = excluded.lease_until,
                last_check_started_at = excluded.last_check_started_at,
                last_check_finished_at = excluded.last_check_finished_at
            """,
            {
                "item_id": item.item_id,
                "source_url": item.source_url,
                "canonical_url": item.canonical_url,
                "product_id": item.product_id,
                "site_key": item.site_key,
                "adapter_key": item.adapter_key,
                "product_name": item.product_name,
                "brand": item.brand,
                "model": item.model,
                "sku": item.sku,
                "gtin": item.gtin,
                "image_url": item.image_url,
                "availability": item.availability,
                "currency": item.currency,
                "last_amount": item.last_amount,
                "last_raw_text": item.last_raw_text,
                "last_source": item.last_source,
                "status": item.status,
                "check_interval_minutes": item.check_interval_minutes,
                "target_amount": item.target_amount,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "last_checked_at": item.last_checked_at,
                "next_check_at": item.next_check_at,
                "lease_owner": item.lease_owner,
                "lease_until": item.lease_until,
                "last_check_started_at": item.last_check_started_at,
                "last_check_finished_at": item.last_check_finished_at,
            },
        )

    def _insert_snapshot_locked(self, snapshot: PriceSnapshot) -> None:
        self._conn.execute(
            """
            INSERT INTO price_snapshots (
                snapshot_id,
                item_id,
                checked_at,
                success,
                amount,
                currency,
                raw_text,
                source,
                error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.item_id,
                snapshot.checked_at,
                1 if snapshot.success else 0,
                snapshot.amount,
                snapshot.currency,
                snapshot.raw_text,
                snapshot.source,
                snapshot.error,
            ),
        )

    def _ensure_tracked_items_columns(self) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(tracked_items)").fetchall()
        }
        wanted = {
            "next_check_at": "TEXT",
            "lease_owner": "TEXT",
            "lease_until": "TEXT",
            "last_check_started_at": "TEXT",
            "last_check_finished_at": "TEXT",
        }
        for column_name, column_type in wanted.items():
            if column_name in existing:
                continue
            self._conn.execute(
                f"ALTER TABLE tracked_items ADD COLUMN {column_name} {column_type}"
            )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> TrackedItem:
        return TrackedItem(
            item_id=row["item_id"],
            source_url=row["source_url"],
            canonical_url=row["canonical_url"],
            product_id=row["product_id"],
            site_key=row["site_key"],
            adapter_key=row["adapter_key"],
            product_name=row["product_name"],
            brand=row["brand"],
            model=row["model"],
            sku=row["sku"],
            gtin=row["gtin"],
            image_url=row["image_url"],
            availability=row["availability"],
            currency=row["currency"],
            last_amount=row["last_amount"],
            last_raw_text=row["last_raw_text"],
            last_source=row["last_source"],
            status=row["status"],
            check_interval_minutes=row["check_interval_minutes"],
            target_amount=row["target_amount"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_checked_at=row["last_checked_at"],
            next_check_at=row["next_check_at"],
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            last_check_started_at=row["last_check_started_at"],
            last_check_finished_at=row["last_check_finished_at"],
        )

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> PriceSnapshot:
        return PriceSnapshot(
            snapshot_id=row["snapshot_id"],
            item_id=row["item_id"],
            checked_at=row["checked_at"],
            success=bool(row["success"]),
            amount=row["amount"],
            currency=row["currency"],
            raw_text=row["raw_text"],
            source=row["source"],
            error=row["error"],
        )

    @staticmethod
    def _row_to_check_run(row: sqlite3.Row) -> TrackingCheckRun:
        return TrackingCheckRun(
            run_id=row["run_id"],
            item_id=row["item_id"],
            scheduled_for=row["scheduled_for"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            attempt=int(row["attempt"]),
            error=row["error"],
        )

    @staticmethod
    def _row_to_bootstrap_idempotency(
        row: sqlite3.Row,
    ) -> TrackingBootstrapIdempotencyRecord:
        return TrackingBootstrapIdempotencyRecord(
            idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            item_id=row["item_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
