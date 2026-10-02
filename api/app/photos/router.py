"""The photo grid's data and batch progress.

The grid lists a project's photos newest first, by keyset. A tiled photo carries
a short-lived signed URL for its thumbnail; no route here ever signs an original.
Progress counts a project's photos by processing status.
"""

import asyncio
import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.config import get_settings
from app.db.tenant import tenant_transaction
from app.deps import ObjectStoreDep, SessionFactoryDep
from app.orgs.access import MemberAccess
from app.pagination import decode_cursor, next_cursor
from app.storage.keys import assert_thumbnail_key_in_org

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs/{org_id}/projects/{project_id}/photos", tags=["photos"])

PHOTO_STATUSES = ("queued", "processing", "tiled", "failed")
MAX_PAGE_SIZE = 100

_PROJECT_EXISTS = text("SELECT 1 FROM projects WHERE id = :project_id AND org_id = :org_id")
_COUNTS = text(
    "SELECT status, count(*) AS photos FROM photos "
    "WHERE project_id = :project_id AND org_id = :org_id GROUP BY status"
)


# Newest first, so a photo just uploaded is on the first page. Served by
# photos_project_created_idx, read backwards.
_LIST_FIRST = text(
    """
    SELECT p.id, p.file_id, f.original_filename, p.status, p.width, p.height, p.error,
           p.thumb_key, p.created_at
    FROM photos p JOIN files f ON f.org_id = p.org_id AND f.id = p.file_id
    WHERE p.project_id = :project_id AND p.org_id = :org_id
    ORDER BY p.created_at DESC, p.id DESC
    LIMIT :limit
    """
)
_LIST_AFTER = text(
    """
    SELECT p.id, p.file_id, f.original_filename, p.status, p.width, p.height, p.error,
           p.thumb_key, p.created_at
    FROM photos p JOIN files f ON f.org_id = p.org_id AND f.id = p.file_id
    WHERE p.project_id = :project_id AND p.org_id = :org_id
      AND (p.created_at, p.id) < (:after_created_at, :after_id)
    ORDER BY p.created_at DESC, p.id DESC
    LIMIT :limit
    """
)


class PhotoOut(BaseModel):
    id: UUID
    file_id: UUID
    original_filename: str | None
    status: str
    width: int | None
    height: int | None
    # A short code for why tiling failed, never a message that could echo input.
    error: str | None
    # Set once the photo is tiled. A signed URL that expires; fetch the list again for a new one.
    thumbnail_url: str | None
    created_at: datetime


class PhotoPage(BaseModel):
    items: list[PhotoOut]
    # Pass as `cursor` to get the next, older page; None on the last one.
    next_cursor: str | None


class ProgressOut(BaseModel):
    """`counts` always has every status, so a client never has to guess at a missing key."""

    total: int
    counts: dict[str, int]
    # True once nothing is waiting or running; failed photos still count as finished.
    finished: bool


@router.get("")
async def list_photos(
    project_id: UUID,
    access: MemberAccess,
    factory: SessionFactoryDep,
    store: ObjectStoreDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 48,
    cursor: Annotated[str | None, Query()] = None,
) -> PhotoPage:
    after = decode_cursor(cursor)
    params: dict = {"project_id": project_id, "org_id": access.org_id, "limit": limit + 1}
    if after is not None:
        params |= {"after_created_at": after.created_at, "after_id": after.id}
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        if (await session.execute(_PROJECT_EXISTS, params)).first() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        rows = (await session.execute(_LIST_FIRST if after is None else _LIST_AFTER, params)).all()
    ttl = get_settings().thumbnail_ttl_seconds
    # Signing is CPU work, a fraction of a millisecond each; a page of 100 would hold the
    # event loop for tens of milliseconds, so it runs in a thread.
    urls = await asyncio.to_thread(
        lambda: [_thumbnail_url(store, row, access.org_id, ttl) for row in rows[:limit]]
    )
    return PhotoPage(
        items=[
            PhotoOut(
                id=row.id,
                file_id=row.file_id,
                original_filename=row.original_filename,
                status=row.status,
                width=row.width,
                height=row.height,
                error=row.error,
                thumbnail_url=url,
                created_at=row.created_at,
            )
            for row, url in zip(rows, urls, strict=False)
        ],
        next_cursor=next_cursor(rows, limit),
    )


def _thumbnail_url(store, row, org_id: UUID, ttl: int) -> str | None:
    if row.status != "tiled" or row.thumb_key is None:
        return None
    try:
        assert_thumbnail_key_in_org(row.thumb_key, org_id)
    except ValueError:
        # The database constraint should make this impossible.
        logger.error(
            "photo row has a thumbnail key outside its org", extra={"photo_id": str(row.id)}
        )
        return None
    return store.presign_thumbnail(row.thumb_key, ttl)


@router.get("/progress")
async def progress(
    project_id: UUID, access: MemberAccess, factory: SessionFactoryDep
) -> ProgressOut:
    params = {"project_id": project_id, "org_id": access.org_id}
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        if (await session.execute(_PROJECT_EXISTS, params)).first() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        rows = (await session.execute(_COUNTS, params)).all()
    counts = dict.fromkeys(PHOTO_STATUSES, 0)
    counts.update({row.status: row.photos for row in rows})
    return ProgressOut(
        total=sum(counts.values()),
        counts=counts,
        finished=counts["queued"] + counts["processing"] == 0,
    )
