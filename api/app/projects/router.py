"""Projects, read-only for now. Every query runs under the org's tenant context."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.db.tenant import tenant_transaction
from app.deps import SessionFactoryDep
from app.orgs.access import MemberAccess

router = APIRouter(prefix="/orgs/{org_id}/projects", tags=["projects"])

MAX_PAGE_SIZE = 100

_LIST = text(
    """
    SELECT id, name, created_at FROM projects
    ORDER BY created_at, id
    LIMIT :limit OFFSET :offset
    """
)
_GET = text("SELECT id, name, created_at FROM projects WHERE id = :project_id")


class ProjectOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime


class ProjectPage(BaseModel):
    items: list[ProjectOut]
    has_more: bool


@router.get("")
async def list_projects(
    access: MemberAccess,
    factory: SessionFactoryDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProjectPage:
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        # One extra row says whether another page exists.
        rows = (await session.execute(_LIST, {"limit": limit + 1, "offset": offset})).all()
    return ProjectPage(
        items=[ProjectOut(id=r.id, name=r.name, created_at=r.created_at) for r in rows[:limit]],
        has_more=len(rows) > limit,
    )


@router.get("/{project_id}")
async def get_project(
    project_id: UUID,
    access: MemberAccess,
    factory: SessionFactoryDep,
) -> ProjectOut:
    async with tenant_transaction(factory, org_id=access.org_id, user_id=access.user_id) as session:
        row = (await session.execute(_GET, {"project_id": project_id})).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ProjectOut(id=row.id, name=row.name, created_at=row.created_at)
