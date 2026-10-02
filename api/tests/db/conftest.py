"""Fixtures for the database isolation suite.

The suite runs against `inspection_test`, never the seeded database. Each run
migrates it down to nothing and back up, inserts two tenants as the owner role,
and then connects as the app role exactly as the API does.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config

from alembic import command
from app.config import get_settings

TEST_DATABASE = "inspection_test"
# Matches the error app.org_id() raises when no org context is set.
CONTEXT_NOT_SET = r"app\.org_id is not set"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@dataclass(frozen=True)
class Tenant:
    org_id: UUID
    user_id: UUID
    project_id: UUID
    file_id: UUID
    audit_event_id: UUID
    project_name: str


def _new_tenant(label: str) -> Tenant:
    return Tenant(
        org_id=uuid4(),
        user_id=uuid4(),
        project_id=uuid4(),
        file_id=uuid4(),
        audit_event_id=uuid4(),
        project_name=f"Synthetic project {label}",
    )


TENANT_A = _new_tenant("A")
TENANT_B = _new_tenant("B")
# A member of both orgs, as a consultant would be.
SHARED_USER_ID = uuid4()


def set_context(
    connection: psycopg.Connection, *, org_id: UUID | None = None, user_id: UUID | None = None
) -> None:
    """Sets tenant context for the current transaction, as app/db/tenant.py does."""
    connection.execute(
        "SELECT set_config('app.org_id', %s, true), set_config('app.user_id', %s, true)",
        (str(org_id) if org_id else "", str(user_id) if user_id else ""),
    )


def _conninfo(user: str, password_file: Path) -> str:
    settings = get_settings()
    return psycopg.conninfo.make_conninfo(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=user,
        password=password_file.read_text().strip(),
    )


def app_conninfo() -> str:
    settings = get_settings()
    return _conninfo(settings.db_app_user, settings.db_app_password_file)


def owner_conninfo() -> str:
    settings = get_settings()
    return _conninfo(settings.db_owner_user, settings.db_owner_password_file)


def _insert_tenant(connection: psycopg.Connection, tenant: Tenant) -> None:
    # Each insert happens under the context the policies demand for it.
    with connection.transaction():
        set_context(connection, user_id=tenant.user_id)
        connection.execute(
            "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
            (tenant.user_id, f"user-{tenant.user_id}@test.example", "Test user"),
        )
    with connection.transaction():
        set_context(connection, org_id=tenant.org_id)
        connection.execute(
            "INSERT INTO orgs (id, name) VALUES (%s, %s)", (tenant.org_id, "Test org")
        )
        connection.execute(
            "INSERT INTO memberships (org_id, user_id, role)"
            " VALUES (%s, %s, 'owner'), (%s, %s, 'inspector')",
            (tenant.org_id, tenant.user_id, tenant.org_id, SHARED_USER_ID),
        )
        connection.execute(
            "INSERT INTO projects (id, org_id, name) VALUES (%s, %s, %s)",
            (tenant.project_id, tenant.org_id, tenant.project_name),
        )
        connection.execute(
            """
            INSERT INTO files (id, org_id, project_id, object_key, content_type)
            VALUES (%s, %s, %s, %s, 'image/jpeg')
            """,
            (
                tenant.file_id,
                tenant.org_id,
                tenant.project_id,
                f"orgs/{tenant.org_id}/projects/{tenant.project_id}/files/{tenant.file_id}/original",
            ),
        )
        connection.execute(
            "INSERT INTO audit_events (id, org_id, actor_user_id, action)"
            " VALUES (%s, %s, %s, 'test.seeded')",
            (tenant.audit_event_id, tenant.org_id, tenant.user_id),
        )


@pytest.fixture(scope="session", autouse=True)
def tenants() -> tuple[Tenant, Tenant]:
    settings = get_settings()
    if settings.db_name != TEST_DATABASE:
        pytest.exit(f"refusing to run: DB_NAME must be {TEST_DATABASE}, not {settings.db_name}")

    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = settings.owner_database_url()
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    with psycopg.connect(owner_conninfo(), autocommit=True) as connection:
        with connection.transaction():
            set_context(connection, user_id=SHARED_USER_ID)
            connection.execute(
                "INSERT INTO users (id, email, display_name) VALUES (%s, %s, %s)",
                (SHARED_USER_ID, "shared@test.example", "Shared user"),
            )
        _insert_tenant(connection, TENANT_A)
        _insert_tenant(connection, TENANT_B)
    return TENANT_A, TENANT_B


def _rolled_back_connection(conninfo: str) -> Iterator[psycopg.Connection]:
    connection = psycopg.connect(conninfo)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def app_conn() -> Iterator[psycopg.Connection]:
    """The app role, inside one transaction that is rolled back afterwards."""
    yield from _rolled_back_connection(app_conninfo())


@pytest.fixture
def owner_conn() -> Iterator[psycopg.Connection]:
    """The owner role, inside one transaction that is rolled back afterwards."""
    yield from _rolled_back_connection(owner_conninfo())


def ids(connection: psycopg.Connection, query: str, params: tuple = ()) -> set[UUID]:
    return {row[0] for row in connection.execute(query, params).fetchall()}
