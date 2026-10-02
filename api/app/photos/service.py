"""Turning a completed upload into a photo with a tiling job.

Called inside the caller's tenant transaction. The photo row and its job commit
together or not at all, so there is never a photo nobody will tile, or a job for
a photo that does not exist.
"""

import json
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TILE_PHOTO = "tile_photo"

_INSERT_PHOTO = text(
    "INSERT INTO photos (id, org_id, project_id, file_id) "
    "VALUES (:id, :org_id, :project_id, :file_id)"
)
_ENQUEUE = text(
    "INSERT INTO jobs (org_id, kind, payload) VALUES (:org_id, :kind, CAST(:payload AS jsonb))"
)


async def register_photo(
    session: AsyncSession, *, org_id: UUID, project_id: UUID, file_id: UUID
) -> UUID:
    """Create the photo for a ready file and enqueue its tiling job. Returns the photo id."""
    photo_id = uuid4()
    await session.execute(
        _INSERT_PHOTO,
        {"id": photo_id, "org_id": org_id, "project_id": project_id, "file_id": file_id},
    )
    # The payload is ids only; the database refuses anything else.
    await session.execute(
        _ENQUEUE,
        {"org_id": org_id, "kind": TILE_PHOTO, "payload": json.dumps({"photo_id": str(photo_id)})},
    )
    return photo_id
