"""The tile_photo job end to end: queue, worker, database and object store."""

import io
import json
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.engine import create_session_factory
from app.db.tenant import tenant_transaction
from app.storage.keys import photo_prefix, tile_key
from app.tiling import plan_tiles
from app.worker.jobs import JobQueue
from tests.worker.conftest import (
    Org,
    add_photo,
    enqueue_job,
    job_state,
    make_due,
    owner_in,
    photo_state,
    query,
    tile_rows,
)
from tests.worker.imaging import encode, gradient, header_only_png

pytestmark = pytest.mark.asyncio

SIZE = (1500, 1000)


def jpeg(size=SIZE) -> bytes:
    return encode(gradient(*size), "JPEG", quality=90)


async def test_a_photo_is_tiled_and_its_tiles_recorded(worker, org, store, settings):
    photo = add_photo(org, store, jpeg())

    ran = await worker.run_until_idle()

    assert ran >= 1
    assert photo_state(photo) == ("tiled", 1500, 1000, None)
    assert job_state(org.id, photo.id) == ("succeeded", 1, None)
    expected = plan_tiles(
        *SIZE,
        tile_size=settings.tile_size,
        overlap=settings.tile_overlap,
        max_tiles=settings.max_tiles_per_photo,
    )
    recorded = tile_rows(photo)
    assert len(recorded) == len(expected)
    assert {(r[0], r[1], r[2]) for r in recorded} == {(s.level, s.x, s.y) for s in expected}


async def test_each_tile_is_stored_under_the_photos_prefix_with_its_position(
    worker, org, store, settings
):
    photo = add_photo(org, store, jpeg())

    await worker.run_until_idle()

    prefix = photo_prefix(org.id, org.project_id, photo.id)
    for level, x, y, src_width, src_height, scale, width, height, key in tile_rows(photo):
        assert key == tile_key(org.id, org.project_id, photo.id, level, x, y)
        assert key.startswith(prefix)
        info = store.inspect(key)
        assert info is not None and info.head.startswith(b"\xff\xd8\xff")
        assert (width, height) == (round(src_width * scale), round(src_height * scale))
        assert x + src_width <= SIZE[0] and y + src_height <= SIZE[1]


async def test_the_overview_records_the_scale_used_to_map_boxes_back(worker, org, store, settings):
    photo = add_photo(org, store, jpeg())

    await worker.run_until_idle()

    overview = next(row for row in tile_rows(photo) if row[0] == 1)
    _, x, y, src_width, src_height, scale, width, height, _ = overview
    assert (x, y, src_width, src_height) == (0, 0, *SIZE)
    assert scale == pytest.approx(settings.tile_size / max(SIZE))
    assert max(width, height) == settings.tile_size


async def test_a_photo_smaller_than_a_tile_has_one_tile(worker, org, store):
    photo = add_photo(org, store, jpeg((400, 300)))

    await worker.run_until_idle()

    assert photo_state(photo) == ("tiled", 400, 300, None)
    assert [(r[0], r[1], r[2], r[6], r[7]) for r in tile_rows(photo)] == [(0, 0, 0, 400, 300)]


async def test_a_tiled_photo_is_not_tiled_again(worker, org, store):
    photo = add_photo(org, store, jpeg())
    await worker.run_until_idle()
    first = tile_rows(photo)
    tile_ids = query(org.id, "SELECT id FROM tiles WHERE photo_id = %s ORDER BY id", (photo.id,))

    with owner_in(org.id) as connection:
        enqueue_job(connection, org.id, photo.id)
    await worker.run_until_idle()

    assert tile_rows(photo) == first
    assert query(org.id, "SELECT id FROM tiles WHERE photo_id = %s ORDER BY id", (photo.id,)) == (
        tile_ids
    )


async def test_tiles_are_jpegs_with_the_stored_size(worker, org, store):
    photo = add_photo(org, store, jpeg())
    await worker.run_until_idle()

    for _, _, _, _, _, _, width, height, key in tile_rows(photo):
        data = store.read(key, max_bytes=5_000_000)
        picture = Image.open(io.BytesIO(data))
        assert (picture.format, picture.size) == ("JPEG", (width, height))


# A worker that dies --------------------------------------------------------------


async def test_a_job_abandoned_mid_way_is_finished_by_the_next_claim(worker, org, store, engine):
    photo = add_photo(org, store, jpeg())
    dead = JobQueue(
        engine, worker_id="dead", kinds=["tile_photo"], lock_seconds=60, backoff_seconds=1
    )
    claimed = await dead.claim()
    assert claimed is not None
    # It started: the photo says processing, and one stale tile row exists from its work.
    query(org.id, "UPDATE photos SET status = 'processing' WHERE id = %s", (photo.id,))
    query(
        org.id,
        "INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale, width,"
        " height, object_key) VALUES (%s, %s, 0, 7, 7, 10, 10, 1, 10, 10, %s)",
        (
            org.id,
            photo.id,
            f"orgs/{org.id}/projects/{org.project_id}/photos/{photo.id}/tiles/stale.jpg",
        ),
    )
    assert await worker.run_until_idle() == 0  # still locked: nothing to do yet

    query(org.id, "UPDATE jobs SET locked_until = now() - interval '1 second'")
    await worker.run_until_idle()

    assert photo_state(photo)[0] == "tiled"
    assert job_state(org.id, photo.id)[:2] == ("succeeded", 2)
    assert (0, 7, 7) not in {(r[0], r[1], r[2]) for r in tile_rows(photo)}  # stale row replaced


async def test_a_failure_after_the_tiles_are_stored_is_repaired_by_the_retry(
    make_worker, org, store, monkeypatch
):
    from app.worker import tile_photo

    photo = add_photo(org, store, jpeg())
    real = tile_photo.TilePhoto._finish
    calls = []

    async def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database went away")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(tile_photo.TilePhoto, "_finish", flaky)
    worker = make_worker()

    await worker.run_until_idle()
    assert job_state(org.id, photo.id) == ("queued", 1, "unexpected")
    assert photo_state(photo)[0] == "processing"
    assert tile_rows(photo) == []  # objects exist, rows do not yet

    make_due(org.id, photo.id)
    await worker.run_until_idle()

    assert photo_state(photo)[0] == "tiled"
    assert job_state(org.id, photo.id)[:2] == ("succeeded", 2)
    assert len(tile_rows(photo)) > 1


# Failures ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"<html>not an image</html>", "unreadable_image"),
        (header_only_png(9_000, 9_000), "too_many_pixels"),
        (header_only_png(100_000, 100_000), "too_many_pixels"),
    ],
    ids=["not-an-image", "bomb-just-over", "bomb-enormous"],
)
async def test_an_image_that_cannot_be_tiled_fails_at_once_without_retries(
    worker, org, store, data, code
):
    photo = add_photo(org, store, data, content_type="image/png")

    await worker.run_until_idle()

    assert photo_state(photo) == ("failed", None, None, code)
    assert job_state(org.id, photo.id) == ("failed", 1, code)
    assert tile_rows(photo) == []


async def test_too_many_tiles_fails_at_once(make_worker, org, store):
    photo = add_photo(org, store, jpeg((3000, 1000)))
    worker = make_worker(max_tiles_per_photo=4)

    await worker.run_until_idle()

    assert photo_state(photo)[3] == "too_many_tiles"
    assert tile_rows(photo) == []
    assert store.inspect(tile_key(org.id, org.project_id, photo.id, 0, 0, 0)) is None


async def test_a_storage_outage_is_retried_then_fails_the_photo_after_the_last_attempt(
    worker, org, store, monkeypatch
):
    photo = add_photo(org, store, jpeg(), max_attempts=2)

    def unavailable(*args, **kwargs):
        raise ClientError({"Error": {"Code": "503", "Message": "down"}}, "GetObject")

    monkeypatch.setattr(type(store), "read", unavailable)

    await worker.run_until_idle()
    assert job_state(org.id, photo.id) == ("queued", 1, "storage_error")
    assert photo_state(photo)[0] == "processing"

    make_due(org.id, photo.id)
    await worker.run_until_idle()

    assert job_state(org.id, photo.id) == ("failed", 2, "storage_error")
    assert photo_state(photo) == ("failed", None, None, "storage_error")


async def test_a_job_with_no_photo_id_fails_without_touching_anything(worker, org, store):
    with owner_in(org.id) as connection:
        connection.execute("INSERT INTO jobs (org_id, kind) VALUES (%s, 'tile_photo')", (org.id,))

    await worker.run_until_idle()

    rows = query(org.id, "SELECT status, last_error FROM jobs")
    assert rows == [("failed", "bad_payload")]


async def test_a_photo_whose_file_is_not_ready_is_not_tiled(worker, org, store):
    photo = add_photo(org, store, jpeg())
    query(org.id, "UPDATE files SET status = 'failed' WHERE id = %s", (photo.file_id,))

    await worker.run_until_idle()

    assert job_state(org.id, photo.id) == ("failed", 1, "photo_not_found")
    assert tile_rows(photo) == []


async def test_the_recorded_error_is_a_code_never_a_message(worker, org, store):
    photo = add_photo(org, store, b"customer-name-in-a-corrupt-file.jpg", content_type="image/png")

    await worker.run_until_idle()

    _, _, last_error = job_state(org.id, photo.id)
    assert last_error == "unreadable_image"
    assert "customer" not in json.dumps(query(org.id, "SELECT * FROM photos")[0], default=str)


# A worker holding one org's job and another org's photo --------------------------


@pytest.fixture
def other_org(store) -> Org:
    other = Org(id=uuid4(), project_id=uuid4())
    with owner_in(other.id) as connection:
        connection.execute("INSERT INTO orgs (id, name) VALUES (%s, 'Other org')", (other.id,))
        connection.execute(
            "INSERT INTO projects (id, org_id, name) VALUES (%s, %s, 'Synthetic other')",
            (other.project_id, other.id),
        )
    return other


async def test_a_job_that_names_another_orgs_photo_does_nothing_to_it(
    worker, org, other_org, store
):
    """Org A's job carries org B's photo id. Scoped to A, the worker cannot see B's photo."""
    victim = add_photo(other_org, store, jpeg(), enqueue=False)
    with owner_in(org.id) as connection:
        enqueue_job(connection, org.id, victim.id)

    await worker.run_until_idle()

    assert job_state(org.id, victim.id) == ("failed", 1, "photo_not_found")
    assert photo_state(victim) == ("queued", None, None, None)
    assert tile_rows(victim) == []
    assert store.inspect(tile_key(other_org.id, other_org.project_id, victim.id, 0, 0, 0)) is None
    assert store.inspect(tile_key(org.id, org.project_id, victim.id, 0, 0, 0)) is None


async def test_each_orgs_tiles_land_under_its_own_prefix(worker, org, other_org, store):
    mine = add_photo(org, store, jpeg())
    theirs = add_photo(other_org, store, jpeg((1200, 900)))

    await worker.run_until_idle()

    assert photo_state(mine)[0] == photo_state(theirs)[0] == "tiled"
    for photo, owner in ((mine, org), (theirs, other_org)):
        keys = [row[8] for row in tile_rows(photo)]
        assert keys and all(key.startswith(f"orgs/{owner.id}/") for key in keys)
    other_side = query(other_org.id, "SELECT count(*) FROM tiles")[0][0]
    assert other_side == len(tile_rows(theirs))


async def test_the_worker_cannot_see_another_orgs_rows_even_by_asking_directly(
    worker, org, other_org, store, engine
):
    victim = add_photo(other_org, store, jpeg(), enqueue=False)
    factory = create_session_factory(engine)

    async with tenant_transaction(factory, org_id=org.id, user_id=None) as session:
        seen = (await session.execute(text("SELECT count(*) FROM photos"))).scalar_one()
        by_id = (
            await session.execute(
                text("SELECT count(*) FROM photos WHERE id = :id"), {"id": victim.id}
            )
        ).scalar_one()
        updated = (
            await session.execute(
                text("UPDATE photos SET status = 'failed' WHERE id = :id"), {"id": victim.id}
            )
        ).rowcount
        # The table the worker may insert into: the policy, not a missing grant, refuses.
        with pytest.raises(DBAPIError, match="row-level security"):
            await session.execute(
                text("INSERT INTO audit_events (org_id, action) VALUES (:org, 'x')"),
                {"org": other_org.id},
            )
    assert (seen, by_id, updated) == (0, 0, 0)
    assert photo_state(victim)[0] == "queued"
