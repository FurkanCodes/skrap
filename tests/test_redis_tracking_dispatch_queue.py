from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure import redis_tracking_dispatch_queue
from app.infrastructure.redis_tracking_dispatch_queue import RedisTrackingDispatchQueue


_STREAM_KEY = "tracking:scheduler:jobs"
_CONSUMER_GROUP = "tracking-scheduler-workers"


class _FakeRedisFactory:
    current: "_FakeRedis"

    @classmethod
    def from_url(cls, *_args, **_kwargs) -> "_FakeRedis":  # noqa: ANN001
        return cls.current


class _FakeRedis:
    def __init__(
        self,
        *,
        xreadgroup_results: list[Any] | None = None,
        xautoclaim_results: list[Any] | None = None,
    ) -> None:
        self.xreadgroup_results = list(xreadgroup_results or [])
        self.xautoclaim_results = list(xautoclaim_results or [])
        self.xgroup_create_calls: list[dict[str, Any]] = []
        self.xreadgroup_calls = 0
        self.xautoclaim_calls = 0

    def xgroup_create(self, **kwargs: Any) -> None:
        self.xgroup_create_calls.append(kwargs)

    def xreadgroup(self, **_kwargs: Any) -> Any:
        self.xreadgroup_calls += 1
        result = self.xreadgroup_results.pop(0) if self.xreadgroup_results else []
        if isinstance(result, Exception):
            raise result
        return result

    def xautoclaim(self, **_kwargs: Any) -> Any:
        self.xautoclaim_calls += 1
        result = self.xautoclaim_results.pop(0) if self.xautoclaim_results else []
        if isinstance(result, Exception):
            raise result
        return result


def _queue(monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis) -> RedisTrackingDispatchQueue:
    _FakeRedisFactory.current = fake_redis
    monkeypatch.setattr(redis_tracking_dispatch_queue, "Redis", _FakeRedisFactory)
    return RedisTrackingDispatchQueue(
        redis_url="redis://example",
        stream_key=_STREAM_KEY,
        consumer_group=_CONSUMER_GROUP,
        max_stream_length=100,
    )


def _nogroup_error() -> Exception:
    return redis_tracking_dispatch_queue.ResponseError(
        "NOGROUP No such key 'tracking:scheduler:jobs' or consumer group "
        "'tracking-scheduler-workers' in XREADGROUP with GROUP option"
    )


def _message_rows() -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    return [
        (
            _STREAM_KEY,
            [
                (
                    "1-0",
                    {
                        "item_id": "trk_1",
                        "scheduled_for": "2026-04-25T12:00:00Z",
                        "claimed_at": "2026-04-25T12:00:01Z",
                        "lease_owner": "dispatcher-a",
                    },
                )
            ],
        )
    ]


def test_claim_recreates_group_and_retries_when_xreadgroup_reports_nogroup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_redis = _FakeRedis(xreadgroup_results=[_nogroup_error(), _message_rows()])
    queue = _queue(monkeypatch, fake_redis)

    with caplog.at_level("WARNING", logger="scraper_api.scheduler.queue"):
        claimed = queue.claim(
            consumer_name="worker-a",
            max_items=10,
            block_seconds=1,
            pending_idle_seconds=60,
        )

    assert [job.ack_id for job in claimed] == ["1-0"]
    assert claimed[0].job.item_id == "trk_1"
    assert fake_redis.xreadgroup_calls == 2
    assert len(fake_redis.xgroup_create_calls) == 2
    assert "scheduler_queue_group_missing_recreated" in caplog.text


def test_claim_recreates_group_and_returns_empty_when_xautoclaim_reports_nogroup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_redis = _FakeRedis(
        xreadgroup_results=[[], []],
        xautoclaim_results=[_nogroup_error(), []],
    )
    queue = _queue(monkeypatch, fake_redis)

    with caplog.at_level("WARNING", logger="scraper_api.scheduler.queue"):
        claimed = queue.claim(
            consumer_name="worker-a",
            max_items=10,
            block_seconds=1,
            pending_idle_seconds=60,
        )

    assert claimed == []
    assert fake_redis.xreadgroup_calls == 2
    assert fake_redis.xautoclaim_calls == 2
    assert len(fake_redis.xgroup_create_calls) == 2
    assert "scheduler_queue_group_missing_recreated" in caplog.text


def test_claim_reraises_non_nogroup_redis_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    error = redis_tracking_dispatch_queue.ResponseError("WRONGTYPE Operation against wrong key")
    fake_redis = _FakeRedis(xreadgroup_results=[error])
    queue = _queue(monkeypatch, fake_redis)

    with pytest.raises(redis_tracking_dispatch_queue.ResponseError, match="WRONGTYPE"):
        queue.claim(
            consumer_name="worker-a",
            max_items=10,
            block_seconds=1,
            pending_idle_seconds=60,
        )

    assert len(fake_redis.xgroup_create_calls) == 1
