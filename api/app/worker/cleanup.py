"""Periodic housekeeping: abandoned uploads and dead sessions.

* An upload that was started and never completed leaves a `pending` row and
  possibly a staged object. Once it is older than the configured age (and
  always past a presigned POST's lifetime) both are removed.
* A finished upload's staging key can be recreated by replaying its presigned
  POST until the POST expires. After that window the worker deletes any such
  object once and records that it did.
* A photo whose job failed without the worker getting to say so (a job failed
  by the claim's sweep, or a database outage at the wrong moment) is marked
  failed, so a project's progress can finish.
* Sessions that can never resolve again are deleted through `auth.purge_sessions`;
  the worker has no access to the sessions table itself.

Which uploads need attention is found across orgs by `queue.abandoned_uploads`,
which returns ids only. Each is then handled in a transaction scoped to its own
org, like any other tenant work. Every step can be repeated.
"""

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import audit
from app.config import Settings
from app.db.tenant import tenant_transaction
from app.storage.keys import original_key, staging_key
from app.storage.s3 import ObjectStore

logger = logging.getLogger(__name__)

BATCH = 100
# Rounds per run, so one stubborn row cannot keep a run going forever.
MAX_ROUNDS = 10

_CANDIDATES = text("SELECT org_id, file_id FROM queue.abandoned_uploads(:age, :limit)")
_REMOVE_PENDING = text(
    """
    DELETE FROM files
    WHERE id = :file_id AND org_id = :org_id AND status IN ('pending', 'completing')
      AND created_at < now() - make_interval(secs => :age)
    RETURNING project_id
    """
)
_SWEEPABLE = text(
    """
    SELECT project_id, status FROM files
    WHERE id = :file_id AND org_id = :org_id AND status IN ('ready', 'failed')
      AND staging_swept_at IS NULL AND created_at < now() - make_interval(secs => :age)
    """
)
_MARK_SWEPT = text(
    "UPDATE files SET staging_swept_at = now() WHERE id = :file_id AND org_id = :org_id"
)
_STUCK_PHOTOS = text("SELECT org_id, photo_id FROM queue.stuck_photos(:limit)")
_FAIL_PHOTO = text(
    "UPDATE photos SET status = 'failed', error = 'worker_lost', updated_at = now() "
    "WHERE id = :photo_id AND org_id = :org_id AND status IN ('queued', 'processing')"
)
_PURGE_SESSIONS = text("SELECT auth.purge_sessions(:grace, :limit)")


@dataclass(frozen=True)
class CleanupReport:
    uploads_removed: int = 0
    staging_swept: int = 0
    sessions_purged: int = 0
    photos_failed: int = 0


class Cleanup:
    def __init__(
        self,
        engine: AsyncEngine,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        settings: Settings,
    ) -> None:
        self._engine = engine
        self._factory = factory
        self._store = store
        self._settings = settings

    async def run_once(self) -> CleanupReport:
        """Every step runs even if another fails: one slow query must not stop the rest."""
        removed, swept = await self._step("uploads", self._uploads(), (0, 0))
        failed = await self._step("stuck photos", self._stuck_photos(), 0)
        purged = await self._step("sessions", self._sessions(), 0)
        return CleanupReport(removed, swept, purged, failed)

    @staticmethod
    async def _step(name: str, work: Awaitable, fallback):
        try:
            return await work
        except (SQLAlchemyError, ClientError, BotoCoreError, OSError):
            logger.exception("cleanup step failed: %s", name)
            return fallback

    async def _uploads(self) -> tuple[int, int]:
        age = self._settings.abandoned_upload_seconds
        removed = swept = 0
        for _ in range(MAX_ROUNDS):
            async with self._engine.begin() as connection:
                candidates = (
                    await connection.execute(_CANDIDATES, {"age": age, "limit": BATCH})
                ).all()
            progressed = 0
            for org_id, file_id in candidates:
                try:
                    outcome = await self._clean_upload(org_id, file_id, age)
                except (SQLAlchemyError, ClientError, BotoCoreError, OSError):
                    # Left as it was; the next run finds it again.
                    logger.exception("upload cleanup failed for file %s", file_id)
                    continue
                removed += outcome == "removed"
                swept += outcome == "swept"
                progressed += outcome is not None
            if len(candidates) < BATCH or progressed == 0:
                break
        if removed or swept:
            logger.info(
                "cleanup: removed %d abandoned uploads, swept %d staging keys", removed, swept
            )
        return removed, swept

    async def _clean_upload(self, org_id: UUID, file_id: UUID, age: int) -> str | None:
        params = {"file_id": file_id, "org_id": org_id, "age": age}
        async with tenant_transaction(self._factory, org_id=org_id, user_id=None) as session:
            pending = (await session.execute(_REMOVE_PENDING, params)).first()
            if pending is not None:
                # The row is deleted in this transaction, so the objects go first
                # and a failure rolls the delete back: nothing is orphaned.
                await self._delete_objects(
                    staging_key(org_id, pending.project_id, file_id),
                    original_key(org_id, pending.project_id, file_id),
                )
                await audit.record(
                    session,
                    org_id=org_id,
                    actor_user_id=None,
                    action="upload.abandoned_removed",
                    target_type="file",
                    target_id=file_id,
                )
                return "removed"
            finished = (await session.execute(_SWEEPABLE, params)).first()
            if finished is not None:
                keys = [staging_key(org_id, finished.project_id, file_id)]
                if finished.status == "failed":
                    # A rejected upload's bytes are never kept; `complete` deletes them,
                    # and this catches a deletion that failed there.
                    keys.append(original_key(org_id, finished.project_id, file_id))
                await self._delete_objects(*keys)
                await session.execute(_MARK_SWEPT, params)
                return "swept"
        return None

    async def _delete_objects(self, *keys: str) -> None:
        for key in keys:
            await asyncio.to_thread(self._store.delete, key)

    async def _stuck_photos(self) -> int:
        async with self._engine.begin() as connection:
            stuck = (await connection.execute(_STUCK_PHOTOS, {"limit": BATCH})).all()
        failed = 0
        for org_id, photo_id in stuck:
            try:
                async with tenant_transaction(self._factory, org_id=org_id, user_id=None) as s:
                    result = await s.execute(_FAIL_PHOTO, {"photo_id": photo_id, "org_id": org_id})
                failed += result.rowcount
            except SQLAlchemyError:
                logger.exception("could not fail stuck photo %s", photo_id)
        if failed:
            logger.warning("cleanup: marked %d photos failed because their job had failed", failed)
        return failed

    async def _sessions(self) -> int:
        grace = self._settings.session_purge_grace_seconds
        total = 0
        for _ in range(MAX_ROUNDS):
            async with self._engine.begin() as connection:
                purged = (
                    await connection.execute(_PURGE_SESSIONS, {"grace": grace, "limit": BATCH})
                ).scalar_one()
            total += purged
            if purged < BATCH:
                break
        if total:
            logger.info("cleanup: purged %d dead sessions", total)
        return total
