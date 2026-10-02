"""Audit events. Written inside the caller's tenant transaction, so an event and
the change it records commit or roll back together. The app role can only
insert and read this table. `actor_user_id` is None for work no person started,
such as the worker's cleanup."""

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT = text(
    """
    INSERT INTO audit_events (org_id, actor_user_id, action, target_type, target_id, detail)
    VALUES (:org_id, :actor_user_id, :action, :target_type, :target_id, CAST(:detail AS jsonb))
    """
)


async def record(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_user_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID,
    detail: dict[str, str | int] | None = None,
) -> None:
    """`detail` must never hold signed URLs, tokens or other secrets."""
    await session.execute(
        _INSERT,
        {
            "org_id": org_id,
            "actor_user_id": actor_user_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "detail": json.dumps(detail or {}),
        },
    )
