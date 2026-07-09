from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure import redis_notification_dispatch_queue
from app.infrastructure.redis_notification_dispatch_queue import RedisNotificationDispatchQueue
from app.application.notification_dispatch_queue import NotificationDispatchJob


_STREAM_KEY = "tracking:notifications"
_CONSUMER_GROUP = "tracking-notifier-workers"


class _FakeRedisFactory:
    current: "_FakeRedis"

    @classmethod
    def from_url(cls, *_args, **_kwargs) -> "_FakeRedis":  # noqa: ANN001
        return cls.current


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[dict[str, Any]] = []

    def xadd(self, stream_key: str, fields: dict[str, str], **kwargs: Any) -> None:
        self._ops.append(
            {
                "stream_key": stream_key,
                "fields": fields,
                "kwargs": kwargs,
            }
        )

    def execute(self) -> list[str]:
        results: list[str] = []
        for index, op in enumerate(self._ops, start=1):
            ack_id = f"{index}-0"
            self._redis.enqueued.append(op)
            results.append(ack_id)
        return results

    def __enter__(self) -> "_FakePipeline":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


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
        self.xack_calls: list[tuple[str, ...]] = []
        self.enqueued: list[dict[str, Any]] = []

    def pipeline(self, transaction: bool = True) -> _FakePipeline:  # noqa: ARG002
        return _FakePipeline(self)

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

    def xack(self, stream_key: str, group: str, *ack_ids: str) -> int:
        self.xack_calls.append((stream_key, group, *ack_ids))
        return len(ack_ids)


def _queue(monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis) -> RedisNotificationDispatchQueue:
    _FakeRedisFactory.current = fake_redis
    monkeypatch.setattr(redis_notification_dispatch_queue, "Redis", _FakeRedisFactory)
    return RedisNotificationDispatchQueue(
        redis_url="redis://example",
        stream_key=_STREAM_KEY,
        consumer_group=_CONSUMER_GROUP,
        max_stream_length=100,
    )


def _nogroup_error() -> Exception:
    return redis_notification_dispatch_queue.ResponseError(
        "NOGROUP No such key 'tracking:notifications' or consumer group "
        "'tracking-notifier-workers' in XREADGROUP with GROUP option"
    )


def _message_rows() -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    return [
        (
            _STREAM_KEY,
            [
                (
                    "1-0",
                    {
                        "notification_id": "nev_1",
                        "enqueued_at": "2026-07-09T12:00:00Z",
                    },
                )
            ],
        )
    ]


def test_enqueue_many_writes_notification_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()
    queue = _queue(monkeypatch, fake_redis)

    count = queue.enqueue_many(
        [
            NotificationDispatchJob(
                notification_id="nev_1",
                enqueued_at="2026-07-09T12:00:00Z",
            )
        ]
    )

    assert count == 1
    assert fake_redis.enqueued[0]["fields"]["notification_id"] == "nev_1"
    assert fake_redis.enqueued[0]["kwargs"]["maxlen"] == 100


def test_claim_recreates_group_and_retries_when_xreadgroup_reports_nogroup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_redis = _FakeRedis(xreadgroup_results=[_nogroup_error(), _message_rows()])
    queue = _queue(monkeypatch, fake_redis)

    with caplog.at_level("WARNING", logger="scraper_api.notifier.queue"):
        claimed = queue.claim(
            consumer_name="notifier-a",
            max_items=10,
            block_seconds=1,
            pending_idle_seconds=60,
        )

    assert [job.ack_id for job in claimed] == ["1-0"]
    assert claimed[0].job.notification_id == "nev_1"
    assert fake_redis.xreadgroup_calls == 2
    assert len(fake_redis.xgroup_create_calls) == 2
    assert "notifier_queue_group_missing_recreated" in caplog.text


def test_claim_recreates_group_and_returns_empty_when_xautoclaim_reports_nogroup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_redis = _FakeRedis(
        xreadgroup_results=[[], []],
        xautoclaim_results=[_nogroup_error(), []],
    )
    queue = _queue(monkeypatch, fake_redis)

    with caplog.at_level("WARNING", logger="scraper_api.notifier.queue"):
        claimed = queue.claim(
            consumer_name="notifier-a",
            max_items=10,
            block_seconds=1,
            pending_idle_seconds=60,
        )

    assert claimed == []
    assert fake_redis.xreadgroup_calls == 2
    assert fake_redis.xautoclaim_calls == 2
    assert len(fake_redis.xgroup_create_calls) == 2
    assert "notifier_queue_group_missing_recreated" in caplog.text


def test_ack_many_forwards_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = _FakeRedis()
    queue = _queue(monkeypatch, fake_redis)

    assert queue.ack_many(["1-0", "2-0"]) == 2
    assert fake_redis.xack_calls == [(_STREAM_KEY, _CONSUMER_GROUP, "1-0", "2-0")]


def test_claim_reraises_non_nogroup_redis_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    error = redis_notification_dispatch_queue.ResponseError("WRONGTYPE Operation against wrong key")
    fake_redis = _FakeRedis(xreadgroup_results=[error])
    queue = _queue(monkeypatch, fake_redis)

    with pytest.raises(redis_notification_dispatch_queue.ResponseError, match="WRONGTYPE"):
        queue.claim(
            consumer_name="notifier-a",
            max_items=10,
            block_seconds=1,
            pending_idle_seconds=60,
        )

    assert len(fake_redis.xgroup_create_calls) == 1
