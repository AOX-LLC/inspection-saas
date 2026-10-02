"""Tenant context never outlives its transaction on a reused connection."""

from collections.abc import AsyncGenerator
from uuid import UUID

import psycopg
import pytest
import pytest_asyncio
from psycopg import errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.engine import TENANT_CONTEXT_KEY, create_engine, create_session_factory
from app.db.tenant import tenant_transaction, user_transaction
from tests.db.conftest import CONTEXT_NOT_SET, TENANT_A, app_conninfo, ids, set_context


class _Rollback(Exception):
    pass


@pytest.mark.parametrize("end_transaction", ["commit", "rollback"])
def test_context_is_gone_in_the_next_transaction(end_transaction):
    with psycopg.connect(app_conninfo()) as connection:
        set_context(connection, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
        assert ids(connection, "SELECT id FROM projects") == {TENANT_A.project_id}
        getattr(connection, end_transaction)()

        setting = connection.execute("SELECT current_setting('app.org_id', true)").fetchone()
        assert setting == ("",)
        with pytest.raises(errors.InsufficientPrivilege, match=CONTEXT_NOT_SET):
            connection.execute("SELECT id FROM projects")


@pytest_asyncio.fixture
async def single_connection_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """The app's own engine, squeezed to one pooled connection so it must be reused."""
    engine = create_engine(get_settings().app_database_url(), pool_size=1, max_overflow=0)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def _backend_pid(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()


async def _assert_no_context_on_reuse(
    factory: async_sessionmaker[AsyncSession], expected_pid: int
) -> None:
    async with factory() as session, session.begin():
        # This probes the database with no context on purpose, which the app's
        # session guard would refuse (see test_session_guard.py).
        session.sync_session.info[TENANT_CONTEXT_KEY] = True
        assert await _backend_pid(session) == expected_pid
        settings = await session.execute(
            text("SELECT current_setting('app.org_id', true), current_setting('app.user_id', true)")
        )
        assert settings.one() == ("", "")
        orgs = await session.execute(text("SELECT count(*) FROM orgs"))
        assert orgs.scalar_one() == 0
        with pytest.raises(DBAPIError) as raised:
            await session.execute(text("SELECT id FROM projects"))
        assert isinstance(raised.value.orig, errors.InsufficientPrivilege)


@pytest.mark.asyncio
async def test_pooled_connection_does_not_carry_context(single_connection_factory):
    factory = single_connection_factory
    async with tenant_transaction(
        factory, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id
    ) as session:
        pid = await _backend_pid(session)
        project_ids = (await session.execute(text("SELECT id FROM projects"))).scalars().all()
        assert set(project_ids) == {TENANT_A.project_id}

    await _assert_no_context_on_reuse(factory, pid)


@pytest.mark.asyncio
async def test_pooled_connection_is_clean_after_a_failed_transaction(single_connection_factory):
    factory = single_connection_factory
    with pytest.raises(_Rollback):
        async with tenant_transaction(
            factory, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id
        ) as session:
            pid = await _backend_pid(session)
            raise _Rollback

    await _assert_no_context_on_reuse(factory, pid)


@pytest.mark.asyncio
async def test_user_transaction_sets_no_org(single_connection_factory):
    async with user_transaction(single_connection_factory, user_id=TENANT_A.user_id) as session:
        org_ids = (await session.execute(text("SELECT id FROM orgs"))).scalars().all()
        assert set(org_ids) == {TENANT_A.org_id}
        with pytest.raises(DBAPIError) as raised:
            await session.execute(text("SELECT id FROM projects"))
        assert isinstance(raised.value.orig, errors.InsufficientPrivilege)


@pytest.mark.asyncio
async def test_tenant_transaction_rejects_a_non_uuid(single_connection_factory):
    forged_org_id: UUID = "' OR true --"  # type: ignore[assignment]
    with pytest.raises(TypeError, match="must be a UUID"):
        async with tenant_transaction(
            single_connection_factory, org_id=forged_org_id, user_id=None
        ):
            pass
