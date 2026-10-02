"""Async engines and session factories for the app and owner roles."""

from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session

# Set on a session by app/db/tenant.py, just before it sets tenant context.
TENANT_CONTEXT_KEY = "tenant_context_opened"


class TenantSession(Session):
    """A session that refuses to run SQL unless tenant.py opened its transaction.

    Code that takes a session from the factory directly would otherwise reach
    the database with no tenant context. The database still fails closed, but
    this turns the mistake into an immediate, explicit error.
    """


@event.listens_for(TenantSession, "do_orm_execute")
def _require_tenant_transaction(state: ORMExecuteState) -> None:
    if not state.session.info.get(TENANT_CONTEXT_KEY):
        raise RuntimeError("database work must go through tenant_transaction or user_transaction")


def create_engine(url: URL, *, pool_size: int = 5, max_overflow: int = 5) -> AsyncEngine:
    # Rollback on return is the default; it is spelled out because tenant
    # context depends on it. set_config(..., true) is transaction-local, so a
    # rolled-back or committed transaction leaves nothing on the connection.
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False, sync_session_class=TenantSession
    )
