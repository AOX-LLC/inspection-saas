"""Fixtures for the worker suite: the real worker against Postgres and the object store.

Rows are written as the owner role under each org's context (forced RLS binds it
like everyone else), the worker runs as `inspection_worker`, and assertions read
back as the owner. Every test makes its own org, so tests never see each
other's photos; the queue itself is shared, so each test starts by clearing any
job another module left behind.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings, get_settings
from app.db.engine import create_engine
from app.storage.keys import original_key
from app.storage.s3 import ObjectStore
from app.worker.runner import Worker
from tests.db.conftest import owner_conninfo, set_context, worker_conninfo

TEST_DATABASE = "inspection_test"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@dataclass(frozen=True)
class Org:
    id: UUID
    project_id: UUID


@dataclass(frozen=True)
class Photo:
    id: UUID
    file_id: UUID
    org: Org
    object_key: str


@pytest.fixture(scope="session")
def settings() -> Settings:
    base = get_settings()
    if base.db_name != TEST_DATABASE:
        pytest.exit(f"refusing to run: DB_NAME must be {TEST_DATABASE}, not {base.db_name}")
    # Quick polling, and a tile size that makes a small image several tiles.
    return base.model_copy(
        update={
            "worker_poll_seconds": 0.2,
            "job_backoff_seconds": 1,
            "cleanup_interval_seconds": 3600,
        }
    )


@pytest.fixture(scope="session", autouse=True)
def database(settings: Settings) -> None:
    """The schema at head. Other suites rebuild it; this only makes sure it is there."""
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = settings.owner_database_url()
    command.upgrade(config, "head")


@pytest_asyncio.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings.worker_database_url(), pool_size=4, max_overflow=2)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def store(settings: Settings) -> ObjectStore:
    return ObjectStore(settings)


@pytest.fixture
def make_worker(settings: Settings, engine: AsyncEngine, store: ObjectStore):
    def build(**overrides) -> Worker:
        return Worker(settings.model_copy(update=overrides), engine, store)

    return build


@pytest.fixture
def worker(make_worker) -> Worker:
    return make_worker()


@contextmanager
def owner_in(org_id: UUID) -> Iterator[psycopg.Connection]:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org_id)
        yield connection


def query(org_id: UUID, sql: str, params: tuple = ()) -> list[tuple]:
    with owner_in(org_id) as connection:
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


@pytest.fixture(autouse=True)
def empty_queue() -> None:
    """Complete any job another module left behind, so this test sees only its own."""
    with psycopg.connect(worker_conninfo(), autocommit=True) as connection:
        while job := connection.execute(
            "SELECT job_id FROM queue.jobs_claim('drain', ARRAY['tile_photo'], 60)"
        ).fetchone():
            connection.execute("SELECT queue.jobs_complete(%s, 'drain')", (job[0],))


@pytest.fixture
def org(store: ObjectStore) -> Iterator[Org]:
    """A new org and project, and the objects its tests create are removed afterwards."""
    created = Org(id=uuid4(), project_id=uuid4())
    with owner_in(created.id) as connection:
        connection.execute(
            "INSERT INTO orgs (id, name) VALUES (%s, 'Worker test org')", (created.id,)
        )
        connection.execute(
            "INSERT INTO projects (id, org_id, name) VALUES (%s, %s, 'Synthetic worker test')",
            (created.project_id, created.id),
        )
    yield created
    # A job left waiting would be picked up by a later test's worker.
    query(created.id, "DELETE FROM jobs")
    keys = [row[0] for row in query(created.id, "SELECT object_key FROM tiles")]
    keys += [row[0] for row in query(created.id, "SELECT object_key FROM files")]
    for key in keys:
        store.delete(key)


def add_photo(
    org: Org,
    store: ObjectStore,
    data: bytes,
    *,
    content_type: str = "image/jpeg",
    enqueue: bool = True,
    max_attempts: int = 5,
) -> Photo:
    """A ready file with its object stored, a queued photo, and (by default) its job."""
    file_id, photo_id = uuid4(), uuid4()
    key = original_key(org.id, org.project_id, file_id)
    store.put(key, data, content_type)
    with owner_in(org.id) as connection:
        connection.execute(
            "INSERT INTO files (id, org_id, project_id, object_key, content_type, size_bytes,"
            " status) VALUES (%s, %s, %s, %s, %s, %s, 'ready')",
            (file_id, org.id, org.project_id, key, content_type, len(data)),
        )
        connection.execute(
            "INSERT INTO photos (id, org_id, project_id, file_id) VALUES (%s, %s, %s, %s)",
            (photo_id, org.id, org.project_id, file_id),
        )
        if enqueue:
            enqueue_job(connection, org.id, photo_id, max_attempts=max_attempts)
    return Photo(id=photo_id, file_id=file_id, org=org, object_key=key)


def enqueue_job(
    connection: psycopg.Connection, org_id: UUID, photo_id: UUID, *, max_attempts: int = 5
) -> None:
    connection.execute(
        "INSERT INTO jobs (org_id, kind, payload, max_attempts)"
        " VALUES (%s, 'tile_photo', jsonb_build_object('photo_id', %s::text), %s)",
        (org_id, photo_id, max_attempts),
    )


def photo_state(photo: Photo) -> tuple:
    """(status, width, height, error)."""
    return query(
        photo.org.id, "SELECT status, width, height, error FROM photos WHERE id = %s", (photo.id,)
    )[0]


def tile_rows(photo: Photo) -> list[tuple]:
    """(level, x, y, src_width, src_height, scale, width, height, object_key), in order."""
    return query(
        photo.org.id,
        "SELECT level, x, y, src_width, src_height, scale, width, height, object_key"
        " FROM tiles WHERE photo_id = %s ORDER BY level, y, x",
        (photo.id,),
    )


def job_state(org_id: UUID, photo_id: UUID) -> tuple:
    """(status, attempts, last_error) of the job whose payload names the photo."""
    return query(
        org_id,
        "SELECT status, attempts, last_error FROM jobs WHERE payload->>'photo_id' = %s",
        (str(photo_id),),
    )[0]


def make_due(org_id: UUID, photo_id: UUID) -> None:
    query(
        org_id,
        "UPDATE jobs SET run_after = now() - interval '1 second' WHERE payload->>'photo_id' = %s",
        (str(photo_id),),
    )
