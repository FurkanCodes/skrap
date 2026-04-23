from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TrackingDispatchJob:
    """Queue payload for one claimed scheduler slot."""

    item_id: str
    scheduled_for: str
    claimed_at: str
    lease_owner: str


@dataclass(frozen=True)
class ClaimedTrackingDispatchJob:
    """Queue-delivered job with an ack token."""

    ack_id: str
    job: TrackingDispatchJob


class TrackingDispatchQueue(Protocol):
    """Transport boundary used between dispatcher and scheduler workers."""

    def enqueue_many(self, jobs: list[TrackingDispatchJob]) -> int:
        """Persist one or more dispatch jobs to the queue."""
        ...

    def claim(
        self,
        *,
        consumer_name: str,
        max_items: int,
        block_seconds: int,
        pending_idle_seconds: int,
    ) -> list[ClaimedTrackingDispatchJob]:
        """Claim a batch of jobs for a consumer."""
        ...

    def ack_many(self, ack_ids: list[str]) -> int:
        """Acknowledge processed jobs."""
        ...
