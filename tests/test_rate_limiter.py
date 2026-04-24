from __future__ import annotations

from app.infrastructure import redis_rate_limiter
from app.infrastructure.redis_rate_limiter import RedisFixedWindowRateLimiter


class _FakePipeline:
    def __init__(self, client: "_FakeRedis") -> None:
        self._client = client
        self._key = ""

    def __enter__(self) -> "_FakePipeline":
        return self

    def __exit__(self, *_args) -> None:  # noqa: ANN002
        return None

    def incr(self, key: str) -> None:
        self._key = key

    def expire(self, _key: str, _seconds: int) -> None:
        return None

    def execute(self) -> list[int | bool]:
        self._client.counts[self._key] = self._client.counts.get(self._key, 0) + 1
        return [self._client.counts[self._key], True]


class _FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    @classmethod
    def from_url(cls, *_args, **_kwargs) -> "_FakeRedis":  # noqa: ANN001
        return cls()

    def pipeline(self, *, transaction: bool) -> _FakePipeline:  # noqa: ARG002
        return _FakePipeline(self)

    def ping(self) -> bool:
        return True


def test_redis_fixed_window_rate_limiter_rejects_after_limit(monkeypatch) -> None:
    monkeypatch.setattr(redis_rate_limiter, "Redis", _FakeRedis)

    limiter = RedisFixedWindowRateLimiter(redis_url="redis://example")

    first = limiter.check(
        bucket="scrapes",
        subject="user_1",
        limit=2,
        window_seconds=60,
    )
    second = limiter.check(
        bucket="scrapes",
        subject="user_1",
        limit=2,
        window_seconds=60,
    )
    third = limiter.check(
        bucket="scrapes",
        subject="user_1",
        limit=2,
        window_seconds=60,
    )

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.remaining == 0
