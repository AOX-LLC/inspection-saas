"""Upload and download of project files. The API presigns; it never proxies bytes.

Upload is three steps. `POST` creates a pending row and returns a presigned
POST whose policy pins the key, the declared content type and the declared
size. The client uploads straight to the object store. `complete` then checks
the stored object's size and magic bytes and only then marks the row ready.
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

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import get_settings
from app.db.tenant import tenant_transaction
from app.deps import ObjectStoreDep, SessionFactoryDep
from app.orgs.access import MemberAccess, WriterAccess
from app.storage.content_types import ALLOWED_CONTENT_TYPES, detect_content_type
from app.storage.keys import assert_key_in_org, original_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orgs/{org_id}/projects/{project_id}/files", tags=["files"])

MAX_PAGE_SIZE = 100
# Pending uploads an org may hold at once; bounds what abandoned uploads can cost.
MAX_PENDING_UPLOADS = 100

_PROJECT_EXISTS = text("SELECT 1 FROM projects WHERE id = :project_id AND org_id = :org_id")
_PENDING_COUNT = text("SELECT count(*) FROM files WHERE org_id = :org_id AND status = 'pending'")
_LIST = text(
    """
    SELECT id, content_type, size_bytes, status, original_filename, created_at
    FROM files WHERE project_id = :project_id AND org_id = :org_id
    ORDER BY created_at, id
    LIMIT :limit OFFSET :offset
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
_FINISH = text(
    """
    UPDATE files SET status = :status, size_bytes = :size_bytes
    WHERE id = :file_id AND project_id = :project_id AND org_id = :org_id AND status = 'pending'
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
    has_more: bool


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
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FilePage:
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        await _require_project(session, access.org_id, project_id)
        rows = (
            await session.execute(
                _LIST,
                {
                    "project_id": project_id,
                    "org_id": access.org_id,
                    "limit": limit + 1,
                    "offset": offset,
                },
            )
        ).all()
    return FilePage(
        items=[_file_out(r) for r in rows[:limit]],
        has_more=len(rows) > limit,
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
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        await _require_project(session, access.org_id, project_id)
        pending = (await session.execute(_PENDING_COUNT, {"org_id": access.org_id})).scalar_one()
        if pending >= MAX_PENDING_UPLOADS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many unfinished uploads. Complete or wait for them to expire.",
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
            key, body.content_type, body.size_bytes, settings.presign_ttl_seconds
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
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        row = (
            await session.execute(
                _GET, {"file_id": file_id, "project_id": project_id, "org_id": access.org_id}
            )
        ).first()
    if row is None:
        raise _not_found()
    if row.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload already finished")
    try:
        assert_key_in_org(row.object_key, access.org_id)
    except ValueError:
        # The database constraint should make this impossible.
        logger.error("file row has a key outside its org", extra={"file_id": str(file_id)})
        raise _not_found() from None

    # The database transaction is closed while the store is asked about the object.
    info = await asyncio.to_thread(store.inspect, row.object_key)
    if info is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload not found")

    problem = None
    if info.size_bytes > row.size_bytes:
        problem = "File is larger than declared"
    elif detect_content_type(info.head) != row.content_type:
        problem = "File content does not match the declared type"

    accepted = problem is None
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        finished = (
            await session.execute(
                _FINISH,
                {
                    "file_id": file_id,
                    "project_id": project_id,
                    "org_id": access.org_id,
                    "status": "ready" if accepted else "failed",
                    "size_bytes": info.size_bytes,
                },
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
    if not accepted:
        # Rejected bytes are not kept.
        await asyncio.to_thread(store.delete, row.object_key)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)

    return _file_out(finished)


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
