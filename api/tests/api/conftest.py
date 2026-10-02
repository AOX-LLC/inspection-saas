"""Fixtures for the HTTP API suite.

Runs against `inspection_test` and the real object store. Each run migrates the
test database down and up, creates two orgs through the owner role, gives every
user a known password, and then drives the real app through httpx exactly as a
browser would: by cookie, with an Origin header.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from app.auth.passwords import hash_password
from app.auth.ratelimit import LoginRateLimiter
from app.config import get_settings
from app.main import create_app
from app.seed.images import render_demo_image
from app.storage.keys import original_key
from app.storage.s3 import ObjectStore
from tests.db.conftest import owner_conninfo, set_context

TEST_DATABASE = "inspection_test"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
ORIGIN = "http://127.0.0.1:4700"
PASSWORD = "correct horse battery staple"


@dataclass(frozen=True)
class Person:
    id: UUID
    email: str


@dataclass(frozen=True)
class Org:
    id: UUID
    project_id: UUID
    # A second project, to test ids from the wrong project under the right org.
    other_project_id: UUID
    file_id: UUID
    object_key: str
    owner: Person
    inspector: Person
    viewer: Person


@dataclass(frozen=True)
class World:
    alpha: Org
    beta: Org
    consultant: Person  # a member of both orgs


def _person(label: str) -> Person:
    return Person(id=uuid4(), email=f"{label}-{uuid4().hex[:8]}@test.example")


def _build_org(label: str) -> Org:
    org_id, project_id, other_project_id, file_id = uuid4(), uuid4(), uuid4(), uuid4()
    return Org(
        id=org_id,
        project_id=project_id,
        other_project_id=other_project_id,
        file_id=file_id,
        object_key=original_key(org_id, project_id, file_id),
        owner=_person(f"{label}-owner"),
        inspector=_person(f"{label}-inspector"),
        viewer=_person(f"{label}-viewer"),
    )


def _insert_person(connection: psycopg.Connection, person: Person, password_hash: str) -> None:
    with connection.transaction():
        set_context(connection, user_id=person.id)
        connection.execute(
            "INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'Test person')",
            (person.id, person.email),
        )
        # credentials has no policy; only the auth role, which the owner may
        # SET ROLE to, can write it.
        connection.execute("SET LOCAL ROLE inspection_auth")
        connection.execute(
            "INSERT INTO credentials (user_id, password_hash) VALUES (%s, %s)",
            (person.id, password_hash),
        )


def _insert_org(connection: psycopg.Connection, org: Org, label: str, consultant: Person) -> None:
    with connection.transaction():
        set_context(connection, org_id=org.id)
        connection.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org.id, f"Org {label}"))
        for person, role in (
            (org.owner, "owner"),
            (org.inspector, "inspector"),
            (org.viewer, "viewer"),
            (consultant, "inspector"),
        ):
            connection.execute(
                "INSERT INTO memberships (org_id, user_id, role) VALUES (%s, %s, %s)",
                (org.id, person.id, role),
            )
        for project_id, name in ((org.project_id, "Main"), (org.other_project_id, "Other")):
            connection.execute(
                "INSERT INTO projects (id, org_id, name) VALUES (%s, %s, %s)",
                (project_id, org.id, f"Synthetic {name} {label}"),
            )
        connection.execute(
            "INSERT INTO files (id, org_id, project_id, object_key, content_type, size_bytes,"
            " status, original_filename)"
            " VALUES (%s, %s, %s, %s, 'image/jpeg', 1, 'ready', 'a.jpg')",
            (org.file_id, org.id, org.project_id, org.object_key),
        )


@pytest.fixture(scope="session")
def world() -> Iterator[World]:
    settings = get_settings()
    if settings.db_name != TEST_DATABASE:
        pytest.exit(f"refusing to run: DB_NAME must be {TEST_DATABASE}, not {settings.db_name}")

    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = settings.owner_database_url()
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    alpha, beta = _build_org("alpha"), _build_org("beta")
    consultant = _person("consultant")
    password_hash = hash_password(PASSWORD)
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection:
        for person in (
            alpha.owner, alpha.inspector, alpha.viewer,
            beta.owner, beta.inspector, beta.viewer, consultant,
        ):  # fmt: skip
            _insert_person(connection, person, password_hash)
        _insert_org(connection, alpha, "alpha", consultant)
        _insert_org(connection, beta, "beta", consultant)

    store = ObjectStore(settings)
    for org in (alpha, beta):
        store.put(org.object_key, render_demo_image("test", seed=1), "image/jpeg")
    try:
        yield World(alpha=alpha, beta=beta, consultant=consultant)
    finally:
        for org in (alpha, beta):
            store.delete(org.object_key)


@pytest_asyncio.fixture(scope="module")
async def app() -> AsyncIterator[FastAPI]:
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def fresh_limiter(app: FastAPI) -> LoginRateLimiter:
    """A clean rate limiter, so one test's failed logins never throttle another's."""
    limiter = LoginRateLimiter()
    app.state.login_limiter = limiter
    return limiter


def new_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": ORIGIN},
    )


@pytest_asyncio.fixture
async def anonymous(
    app: FastAPI, fresh_limiter: LoginRateLimiter
) -> AsyncIterator[httpx.AsyncClient]:
    async with new_client(app) as client:
        yield client


async def login(client: httpx.AsyncClient, person: Person) -> httpx.AsyncClient:
    response = await client.post("/auth/login", json={"email": person.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def signed_in(app: FastAPI, world: World, fresh_limiter: LoginRateLimiter):
    """Factory: an httpx client already logged in as the given person."""
    clients: list[httpx.AsyncClient] = []

    async def make(person: Person) -> httpx.AsyncClient:
        client = new_client(app)
        clients.append(client)
        return await login(client, person)

    yield make
    # Clients are closed by the garbage collector; the transport holds no sockets.


def as_auth_role(sql: str, params: tuple = ()) -> list[tuple]:
    """Runs SQL as the auth role, the only thing that can see sessions, for assertions."""
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        connection.execute("SET LOCAL ROLE inspection_auth")
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []
