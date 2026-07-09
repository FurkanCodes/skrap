from __future__ import annotations

import logging
from datetime import UTC, datetime

try:
    from redis import Redis
except ImportError as exc:  # pragma: no cover - dependency guard
    Redis = None  # type: ignore[assignment]
    _REDIS_IMPORT_ERROR: Exception | None = exc
else:
    _REDIS_IMPORT_ERROR = None

from app.application.tracking_due_index import TrackingDueIndex

_logger = logging.getLogger("scraper_api.scheduler.due_index")


class RedisTrackingDueIndex(TrackingDueIndex):
    """Redis ZSET wake index keyed by next_check_at epoch score."""

    def __init__(self, *, redis_url: str, zset_key: str) -> None:
        if _REDIS_IMPORT_ERROR is not None:
            raise RuntimeError(
                "redis dependency is not installed. Install requirements.txt before "
                "running scheduler due-index runtimes."
            ) from _REDIS_IMPORT_ERROR
        self._zset_key = zset_key.strip()
        if not self._zset_key:
            raise RuntimeError("scheduler due zset key is required")
        assert Redis is not None
        self._redis = Redis.from_url(redis_url, decode_responses=True)

    def upsert(self, *, item_id: str, due_at_epoch: float) -> None:
        cleaned = item_id.strip()
        if not cleaned:
            return
        self._redis.zadd(self._zset_key, {cleaned: float(due_at_epoch)})

    def remove(self, item_ids: list[str]) -> int:
        cleaned = [item_id.strip() for item_id in item_ids if item_id and item_id.strip()]
        if not cleaned:
            return 0
        return int(self._redis.zrem(self._zset_key, *cleaned))

    def peek_due(self, *, now_epoch: float, limit: int) -> list[str]:
        if limit < 1:
            return []
        rows = self._redis.zrangebyscore(
            self._zset_key,
            min="-inf",
            max=now_epoch,
            start=0,
            num=limit,
        )
        return [str(item_id) for item_id in rows or []]

    def next_due_epoch(self, *, after_epoch: float) -> float | None:
        rows = self._redis.zrangebyscore(
            self._zset_key,
            min=f"({after_epoch}",
            max="+inf",
            start=0,
            num=1,
            withscores=True,
        )
        if not rows:
            return None
        _member, score = rows[0]
        try:
            return float(score)
        except (TypeError, ValueError):
            _logger.warning(
                "scheduler_due_index_invalid_score member=%s score=%s",
                _member,
                score,
            )
            return None


def sync_tracked_item_due_index(
    due_index: TrackingDueIndex | None,
    *,
    item_id: str,
    status: str,
    next_check_at: str | None,
) -> None:
    """Best-effort dual-write helper used after Postgres item saves."""
    if due_index is None:
        return
    cleaned_id = item_id.strip()
    if not cleaned_id:
        return
    try:
        if status != "active" or not next_check_at:
            due_index.remove([cleaned_id])
            return
        epoch = _iso_to_epoch(next_check_at)
        if epoch is None:
            due_index.remove([cleaned_id])
            return
        due_index.upsert(item_id=cleaned_id, due_at_epoch=epoch)
    except Exception:
        _logger.exception(
            "scheduler_due_index_sync_failed item_id=%s status=%s",
            cleaned_id,
            status,
        )


def _iso_to_epoch(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()
