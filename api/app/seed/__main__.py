"""Load the demo seed: `python -m app.seed`, only when APP_ENV=demo.

Each write runs in a transaction whose context matches the row being written,
because row-level security is forced even for the owner role. Every insert is
`ON CONFLICT DO NOTHING`, so a re-run inserts nothing.
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import AppEnv, get_settings
from app.db.engine import create_engine, create_session_factory
from app.db.tenant import tenant_transaction, user_transaction
from app.seed.data import MEMBERSHIPS, ORGS, PROJECTS, USERS, SeedOrg

_INSERT_USER = text(
    "INSERT INTO users (id, email, display_name) VALUES (:id, :email, :display_name) "
    "ON CONFLICT DO NOTHING"
)
_INSERT_ORG = text("INSERT INTO orgs (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING")
_INSERT_MEMBERSHIP = text(
    "INSERT INTO memberships (org_id, user_id, role) VALUES (:org_id, :user_id, :role) "
    "ON CONFLICT DO NOTHING"
)
_INSERT_PROJECT = text(
    "INSERT INTO projects (id, org_id, name) VALUES (:id, :org_id, :name) ON CONFLICT DO NOTHING"
)


async def _seed_users(factory: async_sessionmaker[AsyncSession]) -> int:
    inserted = 0
    for user in USERS:
        async with user_transaction(factory, user_id=user.id) as session:
            result = await session.execute(
                _INSERT_USER,
                {"id": user.id, "email": user.email, "display_name": user.display_name},
            )
            inserted += result.rowcount
    return inserted


async def _seed_org(
    factory: async_sessionmaker[AsyncSession], org: SeedOrg
) -> tuple[int, int, int]:
    """Insert one org with its memberships and projects. Returns the three counts."""
    memberships = [
        {"org_id": m.org_id, "user_id": m.user_id, "role": m.role}
        for m in MEMBERSHIPS
        if m.org_id == org.id
    ]
    projects = [
        {"id": p.id, "org_id": p.org_id, "name": p.name} for p in PROJECTS if p.org_id == org.id
    ]
    async with tenant_transaction(factory, org_id=org.id, user_id=None) as session:
        org_result = await session.execute(_INSERT_ORG, {"id": org.id, "name": org.name})
        membership_result = await session.execute(_INSERT_MEMBERSHIP, memberships)
        project_result = await session.execute(_INSERT_PROJECT, projects)
    return org_result.rowcount, membership_result.rowcount, project_result.rowcount


async def seed() -> None:
    engine = create_engine(get_settings().owner_database_url(), pool_size=1, max_overflow=0)
    try:
        factory = create_session_factory(engine)
        users = await _seed_users(factory)
        orgs = memberships = projects = 0
        for org in ORGS:
            org_count, membership_count, project_count = await _seed_org(factory, org)
            orgs += org_count
            memberships += membership_count
            projects += project_count
    finally:
        await engine.dispose()
    print(f"seed: orgs={orgs} users={users} memberships={memberships} projects={projects} inserted")


def main() -> None:
    # Checked before any connection so a misconfigured environment never touches a database.
    if get_settings().app_env is not AppEnv.DEMO:
        print("seed: refusing to run unless APP_ENV=demo", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(seed())


if __name__ == "__main__":
    main()
