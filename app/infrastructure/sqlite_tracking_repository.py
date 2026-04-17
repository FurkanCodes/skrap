from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.tracking.domain import PriceSnapshot, TrackedItem


class SQLiteTrackingRepository:
    """SQLite-backed persistence for tracked items and historical snapshots.

    Storage model:
    - `tracked_items` keeps the latest state per source URL (upsert target).
    - `price_snapshots` is append-only history per tracked item.
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
        """Create tables/indexes once at startup."""
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
                    last_checked_at TEXT
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

                CREATE INDEX IF NOT EXISTS idx_tracked_items_status_updated
                  ON tracked_items(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_price_snapshots_item_checked
                  ON price_snapshots(item_id, checked_at DESC);
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
        """Insert or update tracked item state.

        Conflict target is `source_url`, so the same URL keeps one logical
        tracked record while mutable fields are refreshed.
        """
        with self._lock:
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
                    last_checked_at
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
                    :last_checked_at
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
                    last_checked_at = excluded.last_checked_at
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
                },
            )
            self._conn.commit()

        current = self.get_item_by_source_url(item.source_url)
        if current is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to persist tracked item")
        return current

    def delete_item(self, item_id: str) -> bool:
        """Delete one tracked item; snapshots are removed via FK cascade."""
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
