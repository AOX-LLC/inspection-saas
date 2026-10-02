"""Tenant context for database work.

Row-level security policies read two settings, `app.org_id` and `app.user_id`.
They are only ever set with `set_config(name, value, true)`, which scopes the
value to the current transaction. When the transaction commits or rolls back,
Postgres reverts the setting, so a pooled connection cannot carry one tenant's
context into the next request. Session-level `SET` is never used.

Every unit of tenant work opens exactly one transaction through one of the
context managers below. Code that needs the database without tenant context
gets none: tenant tables raise, and identity tables return nothing.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SET_CONTEXT = text(
    "SELECT set_config('app.org_id', :org_id, true), set_config('app.user_id', :user_id, true)"
)


@asynccontextmanager
async def tenant_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    user_id: UUID | None,
) -> AsyncGenerator[AsyncSession]:
    """One transaction scoped to an org. Commits on success, rolls back on error.

    The caller must already have checked that `user_id` is a member of `org_id`.
    RLS enforces the org boundary; it does not check membership or roles.
    `user_id` is None only for work with no acting user, such as seeding.
    """
    async with _transaction_with_context(session_factory, org_id, user_id) as session:
        yield session


@asynccontextmanager
async def user_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
) -> AsyncGenerator[AsyncSession]:
    """One transaction with a user but no org, for listing that user's own orgs."""
    async with _transaction_with_context(session_factory, None, user_id) as session:
        yield session


@asynccontextmanager
async def _transaction_with_context(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: UUID | None,
    user_id: UUID | None,
) -> AsyncGenerator[AsyncSession]:
    # UUID-typed arguments mean nothing but a canonical UUID string or '' can
    # reach set_config. An empty setting reads as "unset" to the policies.
    async with session_factory() as session, session.begin():
        await session.execute(
            _SET_CONTEXT,
            {"org_id": _as_setting(org_id), "user_id": _as_setting(user_id)},
        )
        yield session


def _as_setting(value: UUID | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, UUID):
        raise TypeError(f"tenant context must be a UUID, got {type(value).__name__}")
    return str(value)
