from __future__ import annotations

"""In-process async job runner for long scrape/discovery requests."""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Literal

from pydantic import BaseModel

ScrapeJobKind = Literal["scrapes", "discover"]
ScrapeJobStatus = Literal["queued", "running", "succeeded", "failed", "expired"]

_logger = logging.getLogger("scraper_api.scrape_jobs")


@dataclass
class ScrapeJob:
    job_id: str
    request_id: str
    kind: ScrapeJobKind
    status: ScrapeJobStatus
    progress: float
    result: BaseModel | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class ScrapeJobService:
    """Stores short-lived scrape jobs in memory for one API replica."""

    def __init__(
        self,
        *,
        max_workers: int,
        ttl_seconds: int,
        max_jobs: int,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers))
        self._ttl_seconds = max(60, ttl_seconds)
        self._max_jobs = max(1, max_jobs)
        self._jobs: dict[str, ScrapeJob] = {}
        self._lock = RLock()

    def submit(
        self,
        *,
        request_id: str,
        kind: ScrapeJobKind,
        run: Callable[[], BaseModel],
    ) -> ScrapeJob:
        now = _utc_now()
        job = ScrapeJob(
            job_id=f"job_{uuid.uuid4().hex[:24]}",
            request_id=request_id,
            kind=kind,
            status="queued",
            progress=0.0,
            result=None,
            error=None,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        with self._lock:
            self._cleanup_locked(now)
            self._trim_locked()
            if len(self._jobs) >= self._max_jobs:
                raise RuntimeError("scrape job queue is full")
            self._jobs[job.job_id] = job

        self._executor.submit(self._run_job, job.job_id, run)
        return self.get(job.job_id) or job

    def get(self, job_id: str) -> ScrapeJob | None:
        now = _utc_now()
        with self._lock:
            self._cleanup_locked(now)
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return _copy_job(job)

    def _run_job(self, job_id: str, run: Callable[[], BaseModel]) -> None:
        self._update(job_id, status="running", progress=0.25)
        try:
            result = run()
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            _logger.exception("scrape_job_failed job_id=%s error=%s", job_id, exc)
            self._update(job_id, status="failed", progress=1.0, error=_safe_error(exc))
            return
        self._update(job_id, status="succeeded", progress=1.0, result=result)

    def _update(
        self,
        job_id: str,
        *,
        status: ScrapeJobStatus,
        progress: float,
        result: BaseModel | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            job.progress = progress
            job.result = result
            job.error = error
            job.updated_at = now
            job.expires_at = now + timedelta(seconds=self._ttl_seconds)

    def _cleanup_locked(self, now: datetime) -> None:
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if now >= job.expires_at
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)

    def _trim_locked(self) -> None:
        if len(self._jobs) < self._max_jobs:
            return
        removable = [
            job
            for job in self._jobs.values()
            if job.status in {"succeeded", "failed", "expired"}
        ]
        removable.sort(key=lambda job: job.updated_at)
        while len(self._jobs) >= self._max_jobs and removable:
            old_job = removable.pop(0)
            self._jobs.pop(old_job.job_id, None)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _copy_job(job: ScrapeJob) -> ScrapeJob:
    return ScrapeJob(
        job_id=job.job_id,
        request_id=job.request_id,
        kind=job.kind,
        status=job.status,
        progress=job.progress,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        expires_at=job.expires_at,
    )


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:500]
