from __future__ import annotations

import logging
from typing import Any

try:
    from redis import Redis
    from redis.exceptions import ResponseError
except ImportError as exc:  # pragma: no cover - dependency guard
    Redis = None  # type: ignore[assignment]
    ResponseError = RuntimeError  # type: ignore[assignment]
    _REDIS_IMPORT_ERROR: Exception | None = exc
else:
    _REDIS_IMPORT_ERROR = None

from app.application.tracking_dispatch_queue import (
    ClaimedTrackingDispatchJob,
    TrackingDispatchJob,
    TrackingDispatchQueue,
)

_logger = logging.getLogger("scraper_api.scheduler.queue")


class RedisTrackingDispatchQueue(TrackingDispatchQueue):
    """Redis Stream transport for scheduler dispatch jobs."""

    def __init__(
        self,
        *,
        redis_url: str,
        stream_key: str,
        consumer_group: str,
        max_stream_length: int = 0,
    ) -> None:
        if _REDIS_IMPORT_ERROR is not None:
            raise RuntimeError(
                "redis dependency is not installed. Install requirements.txt before "
                "running scheduler dispatcher/worker queue runtimes."
            ) from _REDIS_IMPORT_ERROR
        self._stream_key = stream_key.strip()
        self._consumer_group = consumer_group.strip()
        self._max_stream_length = max(0, max_stream_length)
        assert Redis is not None  # narrow for type-checkers
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._ensure_consumer_group()

    def enqueue_many(self, jobs: list[TrackingDispatchJob]) -> int:
        if not jobs:
            return 0
        with self._redis.pipeline(transaction=True) as pipeline:
            for job in jobs:
                fields = {
                    "item_id": job.item_id,
                    "scheduled_for": job.scheduled_for,
                    "claimed_at": job.claimed_at,
                    "lease_owner": job.lease_owner,
                }
                if self._max_stream_length > 0:
                    pipeline.xadd(
                        self._stream_key,
                        fields=fields,
                        maxlen=self._max_stream_length,
                        approximate=True,
                    )
                else:
                    pipeline.xadd(self._stream_key, fields=fields)
            results = pipeline.execute()
        return len(results)

    def claim(
        self,
        *,
        consumer_name: str,
        max_items: int,
        block_seconds: int,
        pending_idle_seconds: int,
    ) -> list[ClaimedTrackingDispatchJob]:
        if max_items < 1:
            return []
        block_ms = max(0, int(block_seconds * 1000))
        try:
            return self._claim_once(
                consumer_name=consumer_name,
                max_items=max_items,
                block_ms=block_ms,
                pending_idle_seconds=pending_idle_seconds,
            )
        except ResponseError as exc:
            if not _is_nogroup_error(exc):
                raise
            self._recreate_missing_consumer_group()
            return self._claim_once(
                consumer_name=consumer_name,
                max_items=max_items,
                block_ms=block_ms,
                pending_idle_seconds=pending_idle_seconds,
            )

    def _claim_once(
        self,
        *,
        consumer_name: str,
        max_items: int,
        block_ms: int,
        pending_idle_seconds: int,
    ) -> list[ClaimedTrackingDispatchJob]:
        fresh = self._redis.xreadgroup(
            groupname=self._consumer_group,
            consumername=consumer_name,
            streams={self._stream_key: ">"},
            count=max_items,
            block=block_ms,
        )
        claimed = self._decode_entries(fresh)
        if claimed:
            return claimed
        reclaimed = self._redis.xautoclaim(
            name=self._stream_key,
            groupname=self._consumer_group,
            consumername=consumer_name,
            min_idle_time=max(1, int(pending_idle_seconds * 1000)),
            start_id="0-0",
            count=max_items,
        )
        reclaimed_entries = self._normalize_xautoclaim_entries(reclaimed)
        return self._decode_entries(reclaimed_entries)

    def ack_many(self, ack_ids: list[str]) -> int:
        if not ack_ids:
            return 0
        return int(self._redis.xack(self._stream_key, self._consumer_group, *ack_ids))

    def _ensure_consumer_group(self) -> None:
        if not self._stream_key:
            raise RuntimeError("scheduler queue stream key is required")
        if not self._consumer_group:
            raise RuntimeError("scheduler queue consumer group is required")
        try:
            self._redis.xgroup_create(
                name=self._stream_key,
                groupname=self._consumer_group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            message = str(exc)
            if "BUSYGROUP" in message:
                return
            raise

    def _recreate_missing_consumer_group(self) -> None:
        self._ensure_consumer_group()
        _logger.warning(
            "scheduler_queue_group_missing_recreated stream_key=%s consumer_group=%s",
            self._stream_key,
            self._consumer_group,
        )

    def _decode_entries(self, rows: Any) -> list[ClaimedTrackingDispatchJob]:
        parsed: list[ClaimedTrackingDispatchJob] = []
        for _stream_name, messages in rows or []:
            for ack_id, fields in messages:
                item_id = _safe_str(fields.get("item_id"))
                scheduled_for = _safe_str(fields.get("scheduled_for"))
                claimed_at = _safe_str(fields.get("claimed_at"))
                lease_owner = _safe_str(fields.get("lease_owner"))
                if not item_id or not scheduled_for:
                    continue
                parsed.append(
                    ClaimedTrackingDispatchJob(
                        ack_id=str(ack_id),
                        job=TrackingDispatchJob(
                            item_id=item_id,
                            scheduled_for=scheduled_for,
                            claimed_at=claimed_at,
                            lease_owner=lease_owner,
                        ),
                    )
                )
        return parsed

    def _normalize_xautoclaim_entries(self, result: Any) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        if not result:
            return []
        messages: Any = []
        if isinstance(result, tuple):
            if len(result) >= 2:
                messages = result[1]
        elif isinstance(result, list):
            if len(result) >= 2 and isinstance(result[0], str):
                messages = result[1]
            else:
                messages = result
        if not isinstance(messages, list):
            return []
        return [(self._stream_key, messages)]


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_nogroup_error(exc: Exception) -> bool:
    return "NOGROUP" in str(exc).upper()
