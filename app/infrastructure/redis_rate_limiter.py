from __future__ import annotations

import time
from dataclasses import dataclass

try:
    from redis import Redis
except ImportError as exc:  # pragma: no cover - dependency guard
    Redis = None  # type: ignore[assignment]
    _REDIS_IMPORT_ERROR: Exception | None = exc
else:
    _REDIS_IMPORT_ERROR = None


class RateLimitUnavailable(RuntimeError):
    """Raised when the backing limiter store cannot be reached."""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RedisFixedWindowRateLimiter:
    """Small Redis-backed fixed-window limiter for API route protection."""

    def __init__(self, *, redis_url: str, key_prefix: str = "skrap:rate") -> None:
        if _REDIS_IMPORT_ERROR is not None:
            raise RateLimitUnavailable("redis dependency is not installed") from _REDIS_IMPORT_ERROR
        assert Redis is not None
        self._redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        self._key_prefix = key_prefix.strip() or "skrap:rate"

    def check(
        self,
        *,
        bucket: str,
        subject: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        normalized_limit = max(1, limit)
        normalized_window = max(1, window_seconds)
        now = int(time.time())
        window_id = now // normalized_window
        reset_at = (window_id + 1) * normalized_window
        retry_after = max(1, reset_at - now)
        key = f"{self._key_prefix}:{bucket}:{subject}:{window_id}"

        try:
            with self._redis.pipeline(transaction=True) as pipeline:
                pipeline.incr(key)
                pipeline.expire(key, normalized_window + 1)
                count, _expire_set = pipeline.execute()
        except Exception as exc:
            raise RateLimitUnavailable("rate limit store unavailable") from exc

        used = int(count)
        remaining = max(0, normalized_limit - used)
        return RateLimitResult(
            allowed=used <= normalized_limit,
            limit=normalized_limit,
            remaining=remaining,
            retry_after_seconds=retry_after,
        )

    def ping(self) -> None:
        try:
            self._redis.ping()
        except Exception as exc:
            raise RateLimitUnavailable("rate limit store unavailable") from exc
