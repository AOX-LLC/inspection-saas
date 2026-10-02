"""The worker's side of the queue: the SECURITY DEFINER functions in schema `queue`.

The worker role has no grant on the `jobs` table. It claims, completes and fails
jobs only through these functions, which check that it still holds the lock. A
claim crosses tenants and so returns ids alone; what a job means is read later,
under row-level security, by a transaction scoped to the claimed job's org.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_CLAIM = text("SELECT * FROM queue.jobs_claim(:worker_id, CAST(:kinds AS text[]), :lock_seconds)")
_COMPLETE = text("SELECT queue.jobs_complete(:job_id, :worker_id)")
_FAIL = text("SELECT queue.jobs_fail(:job_id, :worker_id, :error, :retryable, :backoff_seconds)")


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    org_id: UUID
    kind: str
    payload: dict[str, str]


class JobQueue:
    """One worker's view of the queue. `worker_id` is what the locks are held under."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        worker_id: str,
        kinds: list[str],
        lock_seconds: int,
        backoff_seconds: int,
    ) -> None:
        self._engine = engine
        self.worker_id = worker_id
        self._kinds = kinds
        self._lock_seconds = lock_seconds
        self._backoff_seconds = backoff_seconds

    async def claim(self) -> ClaimedJob | None:
        # Its own transaction, committed before any work starts: the lock must
        # outlive nothing but the claim, and be visible to other workers at once.
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    _CLAIM,
                    {
                        "worker_id": self.worker_id,
                        "kinds": self._kinds,
                        "lock_seconds": self._lock_seconds,
                    },
                )
            ).first()
        if row is None:
            return None
        return ClaimedJob(id=row.job_id, org_id=row.org_id, kind=row.kind, payload=row.payload)

    async def complete(self, job: ClaimedJob) -> bool:
        """False if the lock had been lost to another worker."""
        async with self._engine.begin() as connection:
            held = (
                await connection.execute(_COMPLETE, {"job_id": job.id, "worker_id": self.worker_id})
            ).scalar_one()
        if not held:
            logger.warning("job %s finished after its lock was lost", job.id)
        return held

    async def fail(self, job: ClaimedJob, code: str, *, retryable: bool) -> str | None:
        """'queued' (will retry), 'failed' (final), or None if the lock had been lost.

        `code` is a short fixed word such as `storage_error`; it is stored on the
        job and must never hold data from the photo or the exception message.
        """
        async with self._engine.begin() as connection:
            return (
                await connection.execute(
                    _FAIL,
                    {
                        "job_id": job.id,
                        "worker_id": self.worker_id,
                        "error": code,
                        "retryable": retryable,
                        "backoff_seconds": self._backoff_seconds,
                    },
                )
            ).scalar_one()
