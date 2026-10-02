"""Batch progress: how many of a project's photos are in each processing status."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.db.tenant import tenant_transaction
from app.deps import SessionFactoryDep
from app.orgs.access import MemberAccess

router = APIRouter(prefix="/orgs/{org_id}/projects/{project_id}/photos", tags=["photos"])

PHOTO_STATUSES = ("queued", "processing", "tiled", "failed")

_PROJECT_EXISTS = text("SELECT 1 FROM projects WHERE id = :project_id AND org_id = :org_id")
_COUNTS = text(
    "SELECT status, count(*) AS photos FROM photos "
    "WHERE project_id = :project_id AND org_id = :org_id GROUP BY status"
)


class ProgressOut(BaseModel):
    """`counts` always has every status, so a client never has to guess at a missing key."""

    total: int
    counts: dict[str, int]
    # True once nothing is waiting or running; failed photos still count as finished.
    finished: bool


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
