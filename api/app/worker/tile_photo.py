"""The `tile_photo` job: cut one uploaded photo into tiles.

The job's payload names the photo by id. Everything else is read from the
database under the claimed job's org, so a payload that names another org's
photo finds nothing: row-level security hides it, and the explicit `org_id`
in each query says the same thing a second time.

The work is safe to repeat. A tile's key and its row are determined by its
position, tiles are written before their rows, and the photo only becomes
`tiled` after both, so a worker that dies at any point leaves a state the next
attempt overwrites.
"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.tenant import tenant_transaction
from app.storage.keys import assert_key_in_org, tile_key
from app.storage.s3 import ObjectStore
from app.tiling import ImageRejected, TileSpec, tile_image
from app.worker.errors import JobError
from app.worker.jobs import ClaimedJob

logger = logging.getLogger(__name__)

KIND = "tile_photo"

_LOAD = text(
    """
    SELECT p.id, p.project_id, p.status AS photo_status, f.object_key, f.size_bytes
    FROM photos p JOIN files f ON f.org_id = p.org_id AND f.id = p.file_id
    WHERE p.id = :photo_id AND p.org_id = :org_id AND f.status = 'ready'
    """
)
_PROCESSING = text(
    "UPDATE photos SET status = 'processing', error = NULL, updated_at = now() "
    "WHERE id = :photo_id AND org_id = :org_id"
)
_CLEAR_TILES = text("DELETE FROM tiles WHERE photo_id = :photo_id AND org_id = :org_id")
_INSERT_TILE = text(
    """
    INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale,
                       width, height, object_key)
    VALUES (:org_id, :photo_id, :level, :x, :y, :src_width, :src_height, :scale,
            :width, :height, :object_key)
    """
)
_TILED = text(
    "UPDATE photos SET status = 'tiled', width = :width, height = :height, error = NULL, "
    "updated_at = now() WHERE id = :photo_id AND org_id = :org_id"
)
_FAILED = text(
    "UPDATE photos SET status = 'failed', error = :error, updated_at = now() "
    "WHERE id = :photo_id AND org_id = :org_id AND status <> 'tiled'"
)


@dataclass(frozen=True)
class _Source:
    project_id: UUID
    object_key: str
    size_bytes: int


class TilePhoto:
    kind = KIND

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        settings: Settings,
    ) -> None:
        self._factory = factory
        self._store = store
        self._settings = settings

    async def run(self, job: ClaimedJob) -> None:
        photo_id = _photo_id(job)
        source = await self._start(job, photo_id)
        if source is None:
            return  # already tiled by an earlier attempt
        data = await self._download(source)
        width, height, specs = await self._tile(job, photo_id, source, data)
        await self._finish(job, photo_id, source, width, height, specs)

    async def on_failed(self, job: ClaimedJob, code: str) -> None:
        """The job is out of attempts: the photo is failed too, so its status is not stuck."""
        try:
            async with tenant_transaction(self._factory, org_id=job.org_id, user_id=None) as s:
                await s.execute(
                    _FAILED,
                    {"photo_id": _photo_id(job), "org_id": job.org_id, "error": code},
                )
        except (JobError, SQLAlchemyError):
            logger.exception("could not mark the photo of job %s failed", job.id)

    async def _start(self, job: ClaimedJob, photo_id: UUID) -> _Source | None:
        params = {"photo_id": photo_id, "org_id": job.org_id}
        async with tenant_transaction(self._factory, org_id=job.org_id, user_id=None) as session:
            row = (await session.execute(_LOAD, params)).first()
            if row is None:
                # Missing, not ready, or not this org's: all look the same, on purpose.
                raise JobError("photo_not_found", retryable=False)
            if row.photo_status == "tiled":
                return None
            await session.execute(_PROCESSING, params)
        try:
            assert_key_in_org(row.object_key, job.org_id)
        except ValueError:
            raise JobError("photo_not_found", retryable=False) from None
        return _Source(row.project_id, row.object_key, row.size_bytes)

    async def _download(self, source: _Source) -> bytes:
        limit = min(
            source.size_bytes or self._settings.max_upload_bytes, self._settings.max_upload_bytes
        )
        try:
            return await asyncio.to_thread(self._store.read, source.object_key, max_bytes=limit)
        except ValueError:
            raise JobError("object_too_large", retryable=False) from None
        except (ClientError, BotoCoreError):
            raise JobError("storage_error", retryable=True) from None

    async def _tile(
        self, job: ClaimedJob, photo_id: UUID, source: _Source, data: bytes
    ) -> tuple[int, int, list[TileSpec]]:
        settings = self._settings

        def store_tile(spec: TileSpec, jpeg: bytes) -> None:
            key = tile_key(job.org_id, source.project_id, photo_id, spec.level, spec.x, spec.y)
            self._store.put(key, jpeg, "image/jpeg")

        def work() -> tuple[int, int, list[TileSpec]]:
            return tile_image(
                data,
                tile_size=settings.tile_size,
                overlap=settings.tile_overlap,
                quality=settings.tile_jpeg_quality,
                max_pixels=settings.max_image_pixels,
                max_tiles=settings.max_tiles_per_photo,
                store=store_tile,
            )

        try:
            return await asyncio.to_thread(work)
        except ImageRejected as error:
            raise JobError(error.code, retryable=False) from None
        except (ClientError, BotoCoreError):
            raise JobError("storage_error", retryable=True) from None

    async def _finish(
        self,
        job: ClaimedJob,
        photo_id: UUID,
        source: _Source,
        width: int,
        height: int,
        specs: list[TileSpec],
    ) -> None:
        params = {"photo_id": photo_id, "org_id": job.org_id}
        rows = [
            {
                **params,
                "level": spec.level,
                "x": spec.x,
                "y": spec.y,
                "src_width": spec.src_width,
                "src_height": spec.src_height,
                "scale": spec.scale,
                "width": spec.width,
                "height": spec.height,
                "object_key": tile_key(
                    job.org_id, source.project_id, photo_id, spec.level, spec.x, spec.y
                ),
            }
            for spec in specs
        ]
        async with tenant_transaction(self._factory, org_id=job.org_id, user_id=None) as session:
            await session.execute(_CLEAR_TILES, params)
            await session.execute(_INSERT_TILE, rows)
            await session.execute(_TILED, {**params, "width": width, "height": height})


def _photo_id(job: ClaimedJob) -> UUID:
    try:
        return UUID(job.payload["photo_id"])
    except (KeyError, ValueError):
        raise JobError("bad_payload", retryable=False) from None
