from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from threading import RLock

from app.scraper import PriceResult


@dataclass(frozen=True)
class CachedScrapeRefs:
    """Reference mapping returned when a scrape batch is cached."""

    scrape_ref: str
    result_refs: list[str]


@dataclass
class _CachedBatch:
    created_at: float
    results_by_ref: dict[str, PriceResult]


class ScrapeResultCache:
    """In-memory short-lived cache for scrape results used by tracking bootstrap."""

    def __init__(self, *, ttl_seconds: int = 900, max_batches: int = 500) -> None:
        self._ttl_seconds = max(60, ttl_seconds)
        self._max_batches = max(1, max_batches)
        self._lock = RLock()
        self._batches: dict[str, _CachedBatch] = {}
        self._result_to_batch: dict[str, str] = {}

    def store(self, results: list[PriceResult]) -> CachedScrapeRefs:
        now = time.time()
        scrape_ref = f"scf_{uuid.uuid4().hex[:20]}"
        result_refs = [f"srf_{scrape_ref}_{index}" for index in range(len(results))]
        batch = _CachedBatch(
            created_at=now,
            results_by_ref={result_ref: result for result_ref, result in zip(result_refs, results, strict=False)},
        )

        with self._lock:
            self._cleanup(now)
            self._batches[scrape_ref] = batch
            for result_ref in result_refs:
                self._result_to_batch[result_ref] = scrape_ref
            self._trim_if_needed()
        return CachedScrapeRefs(scrape_ref=scrape_ref, result_refs=result_refs)

    def get_result(self, result_ref: str) -> PriceResult | None:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            batch_ref = self._result_to_batch.get(result_ref)
            if batch_ref is None:
                return None
            batch = self._batches.get(batch_ref)
            if batch is None:
                self._result_to_batch.pop(result_ref, None)
                return None
            return batch.results_by_ref.get(result_ref)

    def _cleanup(self, now: float) -> None:
        expired = [
            scrape_ref
            for scrape_ref, batch in self._batches.items()
            if now - batch.created_at > self._ttl_seconds
        ]
        for scrape_ref in expired:
            self._evict_batch(scrape_ref)

    def _trim_if_needed(self) -> None:
        if len(self._batches) <= self._max_batches:
            return
        ordered = sorted(
            self._batches.items(),
            key=lambda pair: pair[1].created_at,
        )
        overflow = len(self._batches) - self._max_batches
        for index in range(overflow):
            self._evict_batch(ordered[index][0])

    def _evict_batch(self, scrape_ref: str) -> None:
        batch = self._batches.pop(scrape_ref, None)
        if batch is None:
            return
        for result_ref in batch.results_by_ref:
            self._result_to_batch.pop(result_ref, None)
