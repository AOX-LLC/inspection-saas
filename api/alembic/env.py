"""Runs migrations as the owner role. Migrations are plain SQL; there is no ORM metadata."""

import asyncio

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings

# Kept out of `public` so the app role, which has rights in `public`, never sees it.
VERSION_TABLE_SCHEMA = "migrations"


def _owner_url() -> URL:
    # Tests point migrations at their own database through this attribute.
    override = context.config.attributes.get("database_url")
    return override if override is not None else get_settings().owner_database_url()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        version_table_schema=VERSION_TABLE_SCHEMA,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_owner_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")
asyncio.run(_run_online())
