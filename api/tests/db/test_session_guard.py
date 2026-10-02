"""The app's sessions refuse SQL unless tenant.py opened their transaction."""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.db.engine import create_engine, create_session_factory
from app.db.tenant import tenant_transaction, user_transaction
from tests.db.conftest import TENANT_A

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory():
    engine: AsyncEngine = create_engine(get_settings().app_database_url(), pool_size=1)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def test_a_session_taken_straight_from_the_factory_cannot_run_sql(factory):
    async with factory() as session, session.begin():
        with pytest.raises(RuntimeError, match="tenant_transaction or user_transaction"):
            await session.execute(text("SELECT 1"))


async def test_tenant_and_user_transactions_can(factory):
    async with tenant_transaction(
        factory, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id
    ) as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    async with user_transaction(factory, user_id=TENANT_A.user_id) as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
