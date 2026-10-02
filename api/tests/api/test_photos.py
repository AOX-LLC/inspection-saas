"""A completed upload becomes a photo with a tiling job; progress counts photos by status."""

from uuid import UUID, uuid4

import psycopg
import pytest

from app.storage.keys import original_key
from tests.api.conftest import World
from tests.api.test_projects_files import (
    create_upload,
    files_url,
    image_bytes,
    send_to_store,
)
from tests.db.conftest import owner_conninfo, set_context

pytestmark = pytest.mark.asyncio


def progress_url(org, project) -> str:
    return f"/orgs/{org}/projects/{project}/photos/progress"


def rows(org_id: UUID, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org_id)
        return connection.execute(sql, params).fetchall()


async def upload_image(client, org, project, cleanup, *, body: bytes | None = None):
    body = body or image_bytes("PNG")
    start = await create_upload(client, org, project, size=len(body))
    upload = start.json()
    cleanup.append(original_key(org, project, UUID(upload["file_id"])))
    assert (await send_to_store(upload, body)).status_code in (200, 201, 204)
    done = await client.post(f"{files_url(org, project)}/{upload['file_id']}/complete")
    return UUID(upload["file_id"]), done


async def test_completing_an_upload_creates_a_queued_photo_and_its_job(
    signed_in,
    world: World,
    cleanup,
):
    client = await signed_in(world.alpha.inspector)
    org, project = world.alpha.id, world.alpha.project_id

    file_id, done = await upload_image(client, org, project, cleanup)

    assert done.status_code == 200
    photo = rows(org, "SELECT id, status, project_id FROM photos WHERE file_id = %s", (file_id,))
    assert [(r[1], r[2]) for r in photo] == [("queued", project)]
    jobs = rows(
        org,
        "SELECT kind, status, payload FROM jobs WHERE payload->>'photo_id' = %s",
        (str(photo[0][0]),),
    )
    assert jobs == [("tile_photo", "queued", {"photo_id": str(photo[0][0])})]


async def test_a_rejected_upload_creates_no_photo_and_no_job(
    signed_in,
    world: World,
    cleanup,
):
    client = await signed_in(world.alpha.inspector)
    org, project = world.alpha.id, world.alpha.project_id
    before = rows(org, "SELECT (SELECT count(*) FROM photos), (SELECT count(*) FROM jobs)")

    _, done = await upload_image(client, org, project, cleanup, body=b"<html>not an image</html>")

    assert done.status_code == 422
    assert rows(org, "SELECT (SELECT count(*) FROM photos), (SELECT count(*) FROM jobs)") == before


async def test_completing_twice_makes_one_photo(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    org, project = world.alpha.id, world.alpha.project_id
    file_id, _ = await upload_image(client, org, project, cleanup)

    again = await client.post(f"{files_url(org, project)}/{file_id}/complete")

    assert again.status_code == 409
    assert rows(org, "SELECT count(*) FROM photos WHERE file_id = %s", (file_id,)) == [(1,)]


async def test_the_photo_and_its_job_commit_with_the_status_change(
    signed_in,
    world: World,
    cleanup,
    monkeypatch,
):
    """If enqueueing fails, the file is still pending: nothing half-done is left."""
    from app.files import router

    async def broken(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(router, "register_photo", broken)
    client = await signed_in(world.alpha.inspector)
    org, project = world.alpha.id, world.alpha.project_id
    body = image_bytes("PNG")
    start = (await create_upload(client, org, project, size=len(body))).json()
    cleanup.append(original_key(org, project, UUID(start["file_id"])))
    assert (await send_to_store(start, body)).status_code in (200, 201, 204)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await client.post(f"{files_url(org, project)}/{start['file_id']}/complete")

    status = rows(org, "SELECT status FROM files WHERE id = %s", (UUID(start["file_id"]),))
    assert status == [("pending",)]
    assert rows(
        org, "SELECT count(*) FROM photos WHERE file_id = %s", (UUID(start["file_id"]),)
    ) == [(0,)]


# Progress --------------------------------------------------------------------


async def test_progress_lists_every_status_even_when_there_are_no_photos(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    response = await client.get(progress_url(world.alpha.id, world.alpha.other_project_id))

    assert response.status_code == 200
    assert response.json() == {
        "total": 0,
        "counts": {"queued": 0, "processing": 0, "tiled": 0, "failed": 0},
        "finished": True,
    }


async def test_progress_counts_photos_by_status(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    org, project = world.alpha.id, world.alpha.project_id
    before = (await client.get(progress_url(org, project))).json()
    for _ in range(3):
        await upload_image(client, org, project, cleanup)
    newest = rows(org, "SELECT id FROM photos ORDER BY created_at DESC LIMIT 2")
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org)
        connection.execute("UPDATE photos SET status = 'tiled' WHERE id = %s", (newest[0][0],))
        connection.execute("UPDATE photos SET status = 'failed' WHERE id = %s", (newest[1][0],))

    after = (await client.get(progress_url(org, project))).json()

    assert after["total"] == before["total"] + 3
    assert after["counts"]["tiled"] == before["counts"]["tiled"] + 1
    assert after["counts"]["failed"] == before["counts"]["failed"] + 1
    assert after["counts"]["queued"] == before["counts"]["queued"] + 1
    assert after["finished"] is False


async def test_progress_is_finished_once_nothing_is_queued_or_running(signed_in, world: World):
    client = await signed_in(world.alpha.inspector)
    org, project = world.alpha.id, world.alpha.project_id
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org)
        connection.execute(
            "UPDATE photos SET status = 'failed'"
            " WHERE project_id = %s AND status IN ('queued', 'processing')",
            (project,),
        )

    assert (await client.get(progress_url(org, project))).json()["finished"] is True


async def test_progress_counts_only_the_requested_project(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    org = world.alpha.id
    other_before = (await client.get(progress_url(org, world.alpha.other_project_id))).json()

    await upload_image(client, org, world.alpha.project_id, cleanup)

    other_after = (await client.get(progress_url(org, world.alpha.other_project_id))).json()
    assert other_after == other_before


@pytest.mark.parametrize("person", ["viewer", "inspector", "owner"])
async def test_every_member_may_read_progress(signed_in, world: World, person):
    client = await signed_in(getattr(world.alpha, person))

    response = await client.get(progress_url(world.alpha.id, world.alpha.project_id))

    assert response.status_code == 200


async def test_progress_needs_a_session(anonymous, world: World):
    response = await anonymous.get(progress_url(world.alpha.id, world.alpha.project_id))

    assert response.status_code == 401


async def test_another_orgs_progress_is_a_404(signed_in, world: World):
    client = await signed_in(world.alpha.owner)

    foreign_org = await client.get(progress_url(world.beta.id, world.beta.project_id))
    foreign_project = await client.get(progress_url(world.alpha.id, world.beta.project_id))
    missing = await client.get(progress_url(world.alpha.id, uuid4()))

    assert (foreign_org.status_code, foreign_project.status_code, missing.status_code) == (
        404,
        404,
        404,
    )
    assert foreign_org.json() == foreign_project.json() == missing.json()


async def test_progress_reveals_nothing_but_counts(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    body = (await client.get(progress_url(world.alpha.id, world.alpha.project_id))).json()

    assert set(body) == {"total", "counts", "finished"}
    assert set(body["counts"]) == {"queued", "processing", "tiled", "failed"}
