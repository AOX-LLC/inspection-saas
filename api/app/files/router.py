"""Upload and download of project files. The API presigns; it never proxies bytes.

Upload is three steps. `POST` creates a pending row and returns a presigned
POST whose policy pins a staging key, the declared content type and the
declared size. The client uploads straight to the object store. `complete`
checks the staged object's size and magic bytes, copies it server-side to the
final key, checks the copy again, and only then marks the row ready. It claims
the row first (pending to completing, atomically), so two concurrent
completions cannot both copy. The
presigned POST never targets the final key, so a finished upload cannot be
overwritten inside the POST's expiry window.
Download signs a short-lived GET that forces the content type stored in the
database, whatever the object's own metadata says, plus Content-Disposition.
"""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Row, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import audit
from app.config import get_settings
from app.db.tenant import tenant_transaction
from app.deps import ObjectStoreDep, SessionFactoryDep
from app.orgs.access import MemberAccess, OrgAccess, WriterAccess
from app.pagination import decode_cursor, next_cursor
from app.photos.service import register_photo
from app.storage.content_types import ALLOWED_CONTENT_TYPES, detect_content_type
from app.storage.keys import assert_key_in_org, original_key, staging_key
from app.storage.s3 import ObjectChanged, ObjectInfo, ObjectStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orgs/{org_id}/projects/{project_id}/files", tags=["files"])

MAX_PAGE_SIZE = 100
# Pending uploads an org may hold at once; bounds what abandoned uploads can cost.
MAX_PENDING_UPLOADS = 100
# Photos an org may have waiting or running at once. Jobs are served first come
# first served across orgs, so this bounds how long one org can keep the others waiting.
MAX_UNPROCESSED_PHOTOS = 2000
# How long before an upload becomes the cleanup's that it stops being completable.
COMPLETION_MARGIN_SECONDS = 600

_PROJECT_EXISTS = text("SELECT 1 FROM projects WHERE id = :project_id AND org_id = :org_id")
_PENDING_COUNT = text(
    "SELECT count(*) FROM files WHERE org_id = :org_id AND status IN ('pending', 'completing')"
)
_UNPROCESSED_COUNT = text(
    "SELECT count(*) FROM photos WHERE org_id = :org_id AND status IN ('queued', 'processing')"
)
# Keyset pages, served by files_project_created_idx. The two forms are separate
# statements so the first page does not carry a placeholder the planner must guess at.
_LIST_FIRST = text(
    """
    SELECT id, content_type, size_bytes, status, original_filename, created_at
    FROM files WHERE project_id = :project_id AND org_id = :org_id
    ORDER BY created_at, id
    LIMIT :limit
    """
)
_LIST_AFTER = text(
    """
    SELECT id, content_type, size_bytes, status, original_filename, created_at
    FROM files WHERE project_id = :project_id AND org_id = :org_id
      AND (created_at, id) > (:after_created_at, :after_id)
    ORDER BY created_at, id
    LIMIT :limit
    """
)
_INSERT = text(
    """
    INSERT INTO files (id, org_id, project_id, object_key, content_type, size_bytes,
                       status, original_filename, uploaded_by)
    VALUES (:id, :org_id, :project_id, :object_key, :content_type, :size_bytes,
            'pending', :original_filename, :uploaded_by)
    """
)
_GET = text(
    """
    SELECT id, object_key, content_type, size_bytes, status
    FROM files WHERE id = :file_id AND project_id = :project_id AND org_id = :org_id
    """
)
_CLAIM = text(
    """
    UPDATE files SET status = 'completing'
    WHERE id = :file_id AND project_id = :project_id AND org_id = :org_id AND status = 'pending'
      AND created_at > now() - make_interval(secs => :max_age)
    RETURNING object_key, content_type, size_bytes
    """
)
_RELEASE = text(
    """
    UPDATE files SET status = 'pending'
    WHERE id = :file_id AND project_id = :project_id AND org_id = :org_id
      AND status = 'completing'
    """
)
_FINISH = text(
    """
    UPDATE files SET status = :status, size_bytes = :size_bytes
    WHERE id = :file_id AND project_id = :project_id AND org_id = :org_id
      AND status = 'completing'
    RETURNING id, content_type, size_bytes, status, original_filename, created_at
    """
)


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)


class PresignedPostOut(BaseModel):
    url: str
    fields: dict[str, str]


class UploadOut(BaseModel):
    file_id: UUID
    upload: PresignedPostOut
    expires_in: int


class DownloadOut(BaseModel):
    url: str
    expires_in: int


class FileOut(BaseModel):
    id: UUID
    content_type: str
    size_bytes: int | None
    status: str
    original_filename: str | None
    created_at: datetime


class FilePage(BaseModel):
    items: list[FileOut]
    # Pass as `cursor` to get the next page; None on the last one.
    next_cursor: str | None


def _file_out(row: Row) -> FileOut:
    return FileOut(
        id=row.id,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        status=row.status,
        original_filename=row.original_filename,
        created_at=row.created_at,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _clean_filename(name: str) -> str:
    """Display metadata only. It never reaches a key, a header or a path."""
    # Control and format characters (including bidi overrides) never reach metadata.
    printable = "".join(c for c in name if unicodedata.category(c) not in {"Cc", "Cf"})
    base = re.split(r"[\\/]", printable)[-1].strip()
    return base[:255] or "upload"


async def _require_project(session: AsyncSession, org_id: UUID, project_id: UUID) -> None:
    params = {"project_id": project_id, "org_id": org_id}
    if (await session.execute(_PROJECT_EXISTS, params)).first() is None:
        raise _not_found()


@router.get("")
async def list_files(
    project_id: UUID,
    access: MemberAccess,
    factory: SessionFactoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> FilePage:
    after = decode_cursor(cursor)
    params: dict = {"project_id": project_id, "org_id": access.org_id, "limit": limit + 1}
    if after is not None:
        params |= {"after_created_at": after.created_at, "after_id": after.id}
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        await _require_project(session, access.org_id, project_id)
        # One extra row says whether another page exists.
        rows = (await session.execute(_LIST_FIRST if after is None else _LIST_AFTER, params)).all()
    return FilePage(
        items=[_file_out(r) for r in rows[:limit]], next_cursor=next_cursor(rows, limit)
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_upload(
    project_id: UUID,
    body: UploadRequest,
    access: WriterAccess,
    factory: SessionFactoryDep,
    store: ObjectStoreDep,
) -> UploadOut:
    settings = get_settings()
    if body.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only JPEG, PNG and WebP images are accepted",
        )
    if body.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="File is too large"
        )

    file_id = uuid4()
    key = original_key(access.org_id, project_id, file_id)
    staging = staging_key(access.org_id, project_id, file_id)
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        await _require_project(session, access.org_id, project_id)
        org = {"org_id": access.org_id}
        pending = (await session.execute(_PENDING_COUNT, org)).scalar_one()
        if pending >= MAX_PENDING_UPLOADS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many unfinished uploads. Complete or wait for them to expire.",
            )
        waiting = (await session.execute(_UNPROCESSED_COUNT, org)).scalar_one()
        if waiting >= MAX_UNPROCESSED_PHOTOS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many photos are waiting to be processed. Try again shortly.",
            )
        await session.execute(
            _INSERT,
            {
                "id": file_id,
                "org_id": access.org_id,
                "project_id": project_id,
                "object_key": key,
                "content_type": body.content_type,
                "size_bytes": body.size_bytes,
                "original_filename": _clean_filename(body.filename),
                "uploaded_by": access.user_id,
            },
        )
        await audit.record(
            session,
            org_id=access.org_id,
            actor_user_id=access.user_id,
            action="file.upload_presigned",
            target_type="file",
            target_id=file_id,
            detail={"content_type": body.content_type, "size_bytes": body.size_bytes},
        )
        post = store.presign_upload(
            staging, body.content_type, body.size_bytes, settings.presign_ttl_seconds
        )
    return UploadOut(
        file_id=file_id,
        upload=PresignedPostOut(url=post.url, fields=post.fields),
        expires_in=settings.presign_ttl_seconds,
    )


@router.post("/{file_id}/complete")
async def complete_upload(
    project_id: UUID,
    file_id: UUID,
    access: WriterAccess,
    factory: SessionFactoryDep,
    store: ObjectStoreDep,
) -> FileOut:
    ids = {"file_id": file_id, "project_id": project_id, "org_id": access.org_id}
    # An upload this old may already be the cleanup's to remove, so it can no longer be
    # completed. The margin is longer than any completion takes: a row the cleanup can
    # take was claimed (if at all) well before it became eligible.
    max_age = get_settings().abandoned_upload_seconds - COMPLETION_MARGIN_SECONDS
    # Claim the row before touching the store. Only one caller can move it from
    # pending to completing, so a second, concurrent completion cannot copy over
    # an object the first has already checked and published.
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        row = (await session.execute(_CLAIM, {**ids, "max_age": max_age})).first()
        existing = None if row else (await session.execute(_GET, ids)).first()
    if row is None:
        if existing is None:
            raise _not_found()
        detail = (
            "Upload expired; start a new one"
            if existing.status == "pending"
            else "Upload already finished or being completed"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    try:
        return await _finish_upload(access, factory, store, ids, row)
    except Exception:
        # If nothing was published (a storage error, a missing object) the row goes
        # back to pending so the upload can be completed again. After a rejection or
        # a commit it is no longer completing and this changes nothing. A cancelled
        # request is not released: its copy may still be running, and the cleanup
        # removes a row left completing.
        await _release(factory, access, ids)
        raise


async def _release(factory: async_sessionmaker[AsyncSession], access: OrgAccess, ids: dict) -> None:
    try:
        async with tenant_transaction(
            factory, org_id=access.org_id, user_id=access.user_id
        ) as session:
            await session.execute(_RELEASE, ids)
    except SQLAlchemyError:
        # Left completing; the worker's cleanup removes it if it is never finished.
        logger.exception("could not release upload %s", ids["file_id"])


async def _finish_upload(
    access: OrgAccess,
    factory: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    ids: dict,
    row: Row,
) -> FileOut:
    file_id, project_id = ids["file_id"], ids["project_id"]
    try:
        assert_key_in_org(row.object_key, access.org_id)
    except ValueError:
        # The database constraint should make this impossible.
        logger.error("file row has a key outside its org", extra={"file_id": str(file_id)})
        raise _not_found() from None

    # The database transaction is closed while the store is asked about the object.
    staging = staging_key(access.org_id, project_id, file_id)
    info, problem = await asyncio.to_thread(_promote, store, row, staging)
    if info is None and problem is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload not found")

    accepted = problem is None
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        finished = (
            await session.execute(
                _FINISH,
                {**ids, "status": "ready" if accepted else "failed", "size_bytes": info.size_bytes},
            )
        ).first()
        if finished is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Upload already finished"
            )
        await audit.record(
            session,
            org_id=access.org_id,
            actor_user_id=access.user_id,
            action="file.upload_completed" if accepted else "file.upload_rejected",
            target_type="file",
            target_id=file_id,
            detail={"size_bytes": info.size_bytes},
        )
        if accepted:
            # Same transaction as the status change: a ready file always has a
            # photo and a tiling job, and a failed commit leaves neither.
            await register_photo(
                session, org_id=access.org_id, project_id=project_id, file_id=file_id
            )
    # The staged bytes are never kept. A leftover from a write that landed after
    # this point is removed by the worker's cleanup.
    # Past the commit, a storage hiccup must not turn a finished upload into a 500.
    # What is left behind is removed by the worker's cleanup.
    try:
        await asyncio.to_thread(store.delete, staging)
        if not accepted:
            await asyncio.to_thread(store.delete, row.object_key)
    except (ClientError, BotoCoreError):
        logger.exception("could not delete objects of upload %s", file_id)
    if not accepted:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)

    return _file_out(finished)


def _promote(store: ObjectStore, row: Row, staging: str) -> tuple[ObjectInfo | None, str | None]:
    """Check the staged object, copy it to its final key and check the copy.

    Returns (info, problem). `info` is None when nothing was staged. The final
    key is never a POST target, so what the second check reads is stable; the
    first check is why a bad object is never copied, the etag pin and the
    second check are why a swap between the two is caught.
    """
    staged = store.inspect(staging)
    if staged is None:
        return None, None
    problem = _content_problem(staged, row)
    if problem is not None:
        return staged, problem
    try:
        store.copy(staging, row.object_key, content_type=row.content_type, if_match=staged.etag)
    except ObjectChanged:
        return staged, "Upload changed while it was being checked"
    final = store.inspect(row.object_key)
    if final is None:
        return None, None
    problem = _content_problem(final, row)
    return final, problem


def _content_problem(info: ObjectInfo, row: Row) -> str | None:
    if info.size_bytes > row.size_bytes:
        return "File is larger than declared"
    if detect_content_type(info.head) != row.content_type:
        return "File content does not match the declared type"
    return None


@router.get("/{file_id}/download")
async def download(
    project_id: UUID,
    file_id: UUID,
    access: MemberAccess,
    factory: SessionFactoryDep,
    store: ObjectStoreDep,
) -> DownloadOut:
    settings = get_settings()
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        row = (
            await session.execute(
                _GET, {"file_id": file_id, "project_id": project_id, "org_id": access.org_id}
            )
        ).first()
        if row is None:
            raise _not_found()
        if row.status != "ready":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="File is not ready")
        try:
            assert_key_in_org(row.object_key, access.org_id)
        except ValueError:
            # The database constraint should make this impossible.
            logger.error("file row has a key outside its org", extra={"file_id": str(file_id)})
            raise _not_found() from None
        extension = ALLOWED_CONTENT_TYPES[row.content_type]
        url = store.presign_download(
            row.object_key,
            row.content_type,
            f'attachment; filename="inspection-{file_id}.{extension}"',
            settings.presign_ttl_seconds,
        )
        await audit.record(
            session,
            org_id=access.org_id,
            actor_user_id=access.user_id,
            action="file.download_presigned",
            target_type="file",
            target_id=file_id,
        )
    return DownloadOut(url=url, expires_in=settings.presign_ttl_seconds)
