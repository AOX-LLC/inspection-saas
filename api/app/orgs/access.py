"""Who may touch an org.

Row-level security keeps rows inside the org the context names, but it does not
ask whether this user belongs to it. That check is here, and it runs before any
org context is set: the membership lookup uses a user-only context, where the
policies show a user just their own memberships.

A user who is not a member gets 404, the same answer as for an org that does
not exist, so a response never confirms an org's existence. Role checks come
after, so 403 is only ever seen by a member.
"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import text

from app.auth.deps import PrincipalDep
from app.db.tenant import user_transaction
from app.deps import SessionFactoryDep

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_INSPECTOR = "inspector"
ROLE_VIEWER = "viewer"

# Roles that may create or change data. Viewers read only.
WRITER_ROLES = frozenset({ROLE_OWNER, ROLE_ADMIN, ROLE_INSPECTOR})

_MEMBERSHIP = text("SELECT role FROM memberships WHERE org_id = :org_id AND user_id = :user_id")


@dataclass(frozen=True)
class OrgAccess:
    org_id: UUID
    user_id: UUID
    role: str


async def org_access(
    org_id: UUID, principal: PrincipalDep, factory: SessionFactoryDep
) -> OrgAccess:
    async with user_transaction(factory, user_id=principal.user_id) as session:
        role = (
            await session.execute(_MEMBERSHIP, {"org_id": org_id, "user_id": principal.user_id})
        ).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return OrgAccess(org_id=org_id, user_id=principal.user_id, role=role)


def require_roles(allowed: frozenset[str]) -> Callable[..., Coroutine[Any, Any, OrgAccess]]:
    """A dependency: the caller is a member of the path's org with one of `allowed` roles."""

    async def dependency(access: Annotated[OrgAccess, Depends(org_access)]) -> OrgAccess:
        if access.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")
        return access

    return dependency


# What a route declares: the caller's access to the path's org, already checked.
MemberAccess = Annotated[OrgAccess, Depends(org_access)]
WriterAccess = Annotated[OrgAccess, Depends(require_roles(WRITER_ROLES))]
