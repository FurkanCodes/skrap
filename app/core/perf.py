from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar, Token
from functools import wraps
from typing import Any, Callable, TypeVar, cast

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
_path_var: ContextVar[str] = ContextVar("path", default="-")

_logger = logging.getLogger("scraper_api.perf")
_TRUTHY = {"1", "true", "yes", "on"}
_T = TypeVar("_T")


def perf_enabled() -> bool:
    raw = os.getenv("PERF_TRACE_ENABLED", "false").strip().lower()
    return raw in _TRUTHY


def perf_slow_ms() -> float:
    raw = os.getenv("PERF_SLOW_MS", "100").strip()
    try:
        value = float(raw)
    except ValueError:
        return 100.0
    if value < 0:
        return 0.0
    return value


def set_request_context(*, request_id: str, path: str) -> tuple[Token[str], Token[str]]:
    request_token = _request_id_var.set(request_id)
    path_token = _path_var.set(path)
    return request_token, path_token


def reset_request_context(*, request_token: Token[str], path_token: Token[str]) -> None:
    _request_id_var.reset(request_token)
    _path_var.reset(path_token)


def log_span(
    *,
    component: str,
    operation: str,
    duration_ms: float,
    status: str = "ok",
    always: bool = False,
) -> None:
    if not perf_enabled():
        return
    if not always and duration_ms < perf_slow_ms():
        return
    _logger.info(
        "perf_span component=%s operation=%s duration_ms=%.2f status=%s request_id=%s path=%s",
        component,
        operation,
        duration_ms,
        status,
        _request_id_var.get(),
        _path_var.get(),
    )


def timed(component: str, operation: str, fn: Callable[..., _T]) -> Callable[..., _T]:
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> _T:
        started = time.perf_counter()
        status = "ok"
        try:
            return fn(*args, **kwargs)
        except Exception:
            status = "error"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            log_span(
                component=component,
                operation=operation,
                duration_ms=elapsed_ms,
                status=status,
            )

    return wrapped


class TimedProxy:
    """Callable-method proxy that emits perf spans per method call."""

    def __init__(self, target: object, *, component: str) -> None:
        self._target = target
        self._component = component
        self._cache: dict[str, object] = {}

    def __getattr__(self, name: str) -> object:
        cached = self._cache.get(name)
        if cached is not None:
            return cached

        attr = getattr(self._target, name)
        if not callable(attr) or name.startswith("_"):
            self._cache[name] = attr
            return attr

        wrapped = timed(self._component, name, cast(Callable[..., object], attr))
        self._cache[name] = wrapped
        return wrapped
