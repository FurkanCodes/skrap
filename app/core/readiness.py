from __future__ import annotations

from dataclasses import dataclass

import psycopg

try:
    from redis import Redis
except ImportError as exc:  # pragma: no cover - dependency guard
    Redis = None  # type: ignore[assignment]
    _REDIS_IMPORT_ERROR: Exception | None = exc
else:
    _REDIS_IMPORT_ERROR = None

from app.core.config import Settings


@dataclass(frozen=True)
class ComponentReadiness:
    status: str
    error: str | None = None


def check_postgres_ready(settings: Settings) -> ComponentReadiness:
    try:
        with psycopg.connect(settings.tracking_database_url, connect_timeout=3) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
    except Exception:
        return ComponentReadiness(status="error", error="postgres_unavailable")
    return ComponentReadiness(status="ok")


def check_redis_ready(settings: Settings) -> ComponentReadiness:
    if _REDIS_IMPORT_ERROR is not None:
        return ComponentReadiness(status="error", error="redis_dependency_missing")
    assert Redis is not None
    try:
        client = Redis.from_url(
            settings.scheduler_queue_redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
    except Exception:
        return ComponentReadiness(status="error", error="redis_unavailable")
    return ComponentReadiness(status="ok")
