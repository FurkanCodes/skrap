from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class NotificationDispatchJob:
    """Queue payload for one outbound notification wake signal."""

    notification_id: str
    enqueued_at: str = ""


@dataclass(frozen=True)
class ClaimedNotificationDispatchJob:
    """Queue-delivered notification job with an ack token."""

    ack_id: str
    job: NotificationDispatchJob


class NotificationDispatchQueue(Protocol):
    """Transport boundary used to wake notifier workers ASAP."""

    def enqueue_many(self, jobs: list[NotificationDispatchJob]) -> int:
        """Persist one or more notification wake jobs to the queue."""
        ...

    def claim(
        self,
        *,
        consumer_name: str,
        max_items: int,
        block_seconds: int,
        pending_idle_seconds: int,
    ) -> list[ClaimedNotificationDispatchJob]:
        """Claim a batch of notification jobs for a consumer."""
        ...

    def ack_many(self, ack_ids: list[str]) -> int:
        """Acknowledge processed jobs."""
        ...
