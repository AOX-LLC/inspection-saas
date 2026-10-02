"""Load the demo seed: `python -m app.seed`, only when APP_ENV=demo.

Each write runs in a transaction whose context matches the row being written,
because row-level security is forced even for the owner role. Every insert is
`ON CONFLICT DO NOTHING`, so a re-run inserts nothing.

Password hashes go in as the NOLOGIN auth role: `credentials` has no policy, so
the owner, bound by forced RLS, cannot write it. The owner may SET ROLE to the
auth role (it does not inherit it), and only this statement does.

Demo images are uploaded to the object store, then a `ready` file row points at
each. Uploads are overwritten on re-run; the bytes are deterministic.
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.config import AppEnv, get_settings
from app.db.engine import create_engine, create_session_factory
from app.db.tenant import tenant_transaction, user_transaction
from app.seed.data import (
    DEMO_PASSWORD,
    FILES,
    MEMBERSHIPS,
    ORGS,
    PROJECTS,
    USERS,
    SeedFile,
    SeedOrg,
)
from app.seed.images import render_demo_image
from app.storage.keys import original_key
from app.storage.s3 import ObjectStore

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
_INSERT_CREDENTIAL = text(
    "INSERT INTO credentials (user_id, password_hash) VALUES (:user_id, :password_hash) "
    "ON CONFLICT DO NOTHING"
)
_INSERT_FILE = text(
    "INSERT INTO files (id, org_id, project_id, object_key, content_type, size_bytes, status, "
    "original_filename) VALUES (:id, :org_id, :project_id, :object_key, 'image/jpeg', "
    ":size_bytes, 'ready', :original_filename) ON CONFLICT DO NOTHING"
)
JPEG = "image/jpeg"


async def _seed_credentials(factory: async_sessionmaker[AsyncSession]) -> int:
    inserted = 0
    for user in USERS:
        password_hash = await asyncio.to_thread(hash_password, DEMO_PASSWORD)
        async with user_transaction(factory, user_id=user.id) as session:
            await session.execute(text("SET LOCAL ROLE inspection_auth"))
            result = await session.execute(
                _INSERT_CREDENTIAL, {"user_id": user.id, "password_hash": password_hash}
            )
            inserted += result.rowcount
    return inserted


async def _seed_files(factory: async_sessionmaker[AsyncSession], store: ObjectStore) -> int:
    project_names = {p.id: p.name for p in PROJECTS}
    inserted = 0
    for item in FILES:
        inserted += await _seed_file(factory, store, item, project_names[item.project_id])
    return inserted


async def _seed_file(
    factory: async_sessionmaker[AsyncSession], store: ObjectStore, item: SeedFile, project: str
) -> int:
    key = original_key(item.org_id, item.project_id, item.id)
    body = render_demo_image(f"{project} #{item.index}", seed=item.id.int)
    await asyncio.to_thread(store.put, key, body, JPEG)
    async with tenant_transaction(factory, org_id=item.org_id, user_id=None) as session:
        result = await session.execute(
            _INSERT_FILE,
            {
                "id": item.id,
                "org_id": item.org_id,
                "project_id": item.project_id,
                "object_key": key,
                "size_bytes": len(body),
                "original_filename": f"synthetic-{item.index}.jpg",
            },
        )
    return result.rowcount


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
        credentials = await _seed_credentials(factory)
        orgs = memberships = projects = 0
        for org in ORGS:
            org_count, membership_count, project_count = await _seed_org(factory, org)
            orgs += org_count
            memberships += membership_count
            projects += project_count
        files = await _seed_files(factory, ObjectStore(get_settings()))
    finally:
        await engine.dispose()
    print(
        f"seed: orgs={orgs} users={users} credentials={credentials} memberships={memberships} "
        f"projects={projects} files={files} inserted"
    )


def main() -> None:
    # Checked before any connection so a misconfigured environment never touches a database.
    if get_settings().app_env is not AppEnv.DEMO:
        print("seed: refusing to run unless APP_ENV=demo", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(seed())


if __name__ == "__main__":
    main()
