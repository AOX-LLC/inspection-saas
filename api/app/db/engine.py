"""Async engines and session factories for the app and owner roles."""

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
