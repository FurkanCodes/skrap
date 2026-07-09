from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure import redis_tracking_due_index
from app.infrastructure.redis_tracking_due_index import (
    RedisTrackingDueIndex,
    sync_tracked_item_due_index,
)


_ZSET_KEY = "tracking:due"


class _FakeRedisFactory:
    current: "_FakeRedis"

    @classmethod
    def from_url(cls, *_args, **_kwargs) -> "_FakeRedis":  # noqa: ANN001
        return cls.current


class _FakeRedis:
    def __init__(self) -> None:
        self.zadd_calls: list[dict[str, Any]] = []
        self.zrem_calls: list[tuple[str, ...]] = []
        self.members: dict[str, float] = {}

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zadd_calls.append({"key": key, "mapping": mapping})
        self.members.update(mapping)
        return len(mapping)

    def zrem(self, key: str, *members: str) -> int:
        del key
        self.zrem_calls.append(members)
        removed = 0
        for member in members:
            if member in self.members:
                del self.members[member]
                removed += 1
        return removed

    def zrangebyscore(
        self,
        key: str,
        *,
        min: str | float,
        max: str | float,
        start: int = 0,
        num: int | None = None,
        withscores: bool = False,
    ) -> list[Any]:
        del key
        rows = sorted(self.members.items(), key=lambda item: item[1])
        filtered: list[tuple[str, float]] = []
        for member, score in rows:
            if not _score_in_range(score, min=min, max=max):
                continue
            filtered.append((member, score))
        sliced = filtered[start:]
        if num is not None:
            sliced = sliced[:num]
        if withscores:
            return sliced
        return [member for member, _score in sliced]


def _score_in_range(score: float, *, min: str | float, max: str | float) -> bool:
    min_exclusive = isinstance(min, str) and min.startswith("(")
    max_exclusive = isinstance(max, str) and max.startswith("(")
    min_value = float("-inf") if min == "-inf" else float(str(min).lstrip("("))
    max_value = float("inf") if max == "+inf" else float(str(max).lstrip("("))
    if min_exclusive:
        if not score > min_value:
            return False
    elif not score >= min_value:
        return False
    if max_exclusive:
        if not score < max_value:
            return False
    elif not score <= max_value:
        return False
    return True


def _index(monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis) -> RedisTrackingDueIndex:
    _FakeRedisFactory.current = fake_redis
    monkeypatch.setattr(redis_tracking_due_index, "Redis", _FakeRedisFactory)
    return RedisTrackingDueIndex(redis_url="redis://example", zset_key=_ZSET_KEY)


def test_due_index_upsert_peek_and_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()
    index = _index(monkeypatch, fake_redis)

    index.upsert(item_id="trk_1", due_at_epoch=100.0)
    index.upsert(item_id="trk_2", due_at_epoch=200.0)

    assert index.peek_due(now_epoch=150.0, limit=10) == ["trk_1"]
    assert index.next_due_epoch(after_epoch=150.0) == 200.0
    assert index.remove(["trk_1"]) == 1
    assert index.peek_due(now_epoch=250.0, limit=10) == ["trk_2"]


def test_sync_tracked_item_due_index_upserts_active_and_removes_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    index = _index(monkeypatch, fake_redis)

    sync_tracked_item_due_index(
        index,
        item_id="trk_1",
        status="active",
        next_check_at="2026-07-09T12:00:00Z",
    )
    assert "trk_1" in fake_redis.members

    sync_tracked_item_due_index(
        index,
        item_id="trk_1",
        status="paused",
        next_check_at="2026-07-09T12:00:00Z",
    )
    assert "trk_1" not in fake_redis.members
