"""A batch of 50 synthetic photos, end to end: upload through the API, process in the
background by a real worker, and read the progress back through the API."""

import asyncio
import time
from collections.abc import Iterator
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio

from app.config import get_settings
from app.db.engine import create_engine
from app.seed.batch import BATCH_SIZE, BatchPhoto, batch_photos
from app.storage.keys import original_key
from app.storage.s3 import ObjectStore
from app.tiling import plan_tiles
from app.worker.runner import Worker
from tests.api.conftest import World
from tests.api.test_projects_files import create_upload, files_url, send_to_store
from tests.db.conftest import owner_conninfo, set_context

pytestmark = pytest.mark.asyncio

UPLOAD_CONCURRENCY = 4


@pytest.fixture(scope="module")
def photos() -> list[BatchPhoto]:
    return list(batch_photos(BATCH_SIZE))


@pytest_asyncio.fixture
async def worker():
    settings = get_settings()
    engine = create_engine(settings.worker_database_url(), pool_size=4, max_overflow=2)
    yield Worker(settings, engine, ObjectStore(settings))
    await engine.dispose()


@pytest.fixture
def project(world: World) -> Iterator[UUID]:
    """A project of its own, so the counts are this batch's and nothing else's.

    Removed afterwards, with its files, photos and tiles, because other tests
    count the org's projects.
    """
    project_id = uuid4()
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection:
        with connection.transaction():
            set_context(connection, org_id=world.alpha.id)
            connection.execute(
                "INSERT INTO projects (id, org_id, name) VALUES (%s, %s, 'Synthetic batch run')",
                (project_id, world.alpha.id),
            )
        yield project_id
        with connection.transaction():
            set_context(connection, org_id=world.alpha.id)
            connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))


def progress_url(world: World, project: UUID) -> str:
    return f"/orgs/{world.alpha.id}/projects/{project}/photos/progress"


async def upload_batch(
    client: httpx.AsyncClient, world: World, project: UUID, batch: list[BatchPhoto]
) -> dict[UUID, BatchPhoto]:
    """Every photo goes through the real flow: presign, POST to the store, complete."""
    gate = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    uploaded: dict[UUID, BatchPhoto] = {}

    async def one(photo: BatchPhoto) -> None:
        async with gate:
            start = await create_upload(
                client,
                world.alpha.id,
                project,
                content_type="image/jpeg",
                size=len(photo.data),
                name=photo.filename,
            )
            assert start.status_code == 201, start.text
            upload = start.json()
            assert (await send_to_store(upload, photo.data)).status_code in (200, 201, 204)
            done = await client.post(
                f"{files_url(world.alpha.id, project)}/{upload['file_id']}/complete"
            )
            assert done.status_code == 200, done.text
            uploaded[UUID(upload["file_id"])] = photo

    await asyncio.gather(*(one(photo) for photo in batch))
    return uploaded


def delete_objects(store: ObjectStore, world: World, project: UUID, file_ids) -> None:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=world.alpha.id)
        keys = [r[0] for r in connection.execute("SELECT object_key FROM tiles").fetchall()]
    for key in keys:
        store.delete(key)
    for file_id in file_ids:
        store.delete(original_key(world.alpha.id, project, file_id))


async def test_a_batch_of_fifty_photos_is_processed_in_the_background_and_progress_is_readable(
    signed_in, world: World, project, photos, worker, capsys
):
    client = await signed_in(world.alpha.inspector)
    viewer = await signed_in(world.alpha.viewer)
    settings = get_settings()
    store = ObjectStore(settings)

    uploaded = await upload_batch(client, world, project, photos)

    # Nothing has run yet: every photo is waiting, and a viewer can see that.
    waiting = (await viewer.get(progress_url(world, project))).json()
    assert waiting == {
        "total": BATCH_SIZE,
        "counts": {"queued": BATCH_SIZE, "processing": 0, "tiled": 0, "failed": 0},
        "finished": False,
    }

    started = time.monotonic()
    await worker.run_until_idle()
    elapsed = time.monotonic() - started

    done = (await viewer.get(progress_url(world, project))).json()
    assert done == {
        "total": BATCH_SIZE,
        "counts": {"queued": 0, "processing": 0, "tiled": BATCH_SIZE, "failed": 0},
        "finished": True,
    }
    with capsys.disabled():
        print(f"\nbatch of {BATCH_SIZE} photos tiled in {elapsed:.1f}s by one in-process worker")

    try:
        with (
            psycopg.connect(owner_conninfo(), autocommit=True) as connection,
            connection.transaction(),
        ):
            set_context(connection, org_id=world.alpha.id)
            states = connection.execute(
                "SELECT j.status, count(*) FROM jobs j JOIN photos p"
                " ON p.id = (j.payload->>'photo_id')::uuid WHERE p.project_id = %s"
                " GROUP BY j.status",
                (project,),
            ).fetchall()
            rows = connection.execute(
                "SELECT p.file_id, t.level, t.x, t.y, t.object_key"
                " FROM tiles t JOIN photos p ON p.id = t.photo_id WHERE p.project_id = %s",
                (project,),
            ).fetchall()
            sizes = {
                r[0]: (r[1], r[2])
                for r in connection.execute(
                    "SELECT file_id, width, height FROM photos WHERE project_id = %s", (project,)
                ).fetchall()
            }
        assert states == [("succeeded", BATCH_SIZE)]

        # Every photo has exactly the tiles its size calls for, recorded at the size it is
        # displayed at: the sideways ones were rotated before they were cut.
        by_file: dict[UUID, set[tuple]] = {}
        for file_id, level, x, y, _ in rows:
            by_file.setdefault(file_id, set()).add((level, x, y))
        assert set(by_file) == set(uploaded)
        for file_id, photo in uploaded.items():
            assert sizes[file_id] == (photo.width, photo.height), photo.filename
            expected = plan_tiles(
                photo.width,
                photo.height,
                tile_size=settings.tile_size,
                overlap=settings.tile_overlap,
                max_tiles=settings.max_tiles_per_photo,
            )
            assert by_file[file_id] == {(s.level, s.x, s.y) for s in expected}, photo.filename

        # And the tiles are really in the store, under the photo's own prefix.
        sample = rows[:: max(1, len(rows) // 40)]
        for *_, key in sample:
            info = store.inspect(key)
            assert info is not None and info.head.startswith(b"\xff\xd8\xff"), key
            assert key.startswith(f"orgs/{world.alpha.id}/projects/{project}/photos/")
    finally:
        delete_objects(store, world, project, uploaded)


async def test_a_photo_that_cannot_be_tiled_shows_up_as_failed_in_the_progress(
    signed_in, world: World, project, worker
):
    client = await signed_in(world.alpha.inspector)
    good = list(batch_photos(3, seed=7))
    # Starts with a JPEG's magic bytes, so the upload is accepted, but is not an image.
    corrupt = BatchPhoto(99, "corrupt.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 200, 0, 0)
    store = ObjectStore(get_settings())

    uploaded = await upload_batch(client, world, project, [*good, corrupt])
    await worker.run_until_idle()

    try:
        body = (await client.get(progress_url(world, project))).json()
        assert body["counts"] == {"queued": 0, "processing": 0, "tiled": 3, "failed": 1}
        assert body["finished"] is True
    finally:
        delete_objects(store, world, project, uploaded)
