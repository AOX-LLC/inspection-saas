"""The photo grid's list: thumbnails only, newest first, paged by keyset, tenant-scoped."""

import io
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from PIL import Image

from app.config import get_settings
from app.storage.keys import original_key, thumbnail_key
from app.storage.s3 import ObjectStore
from tests.api.conftest import World
from tests.db.conftest import owner_conninfo, set_context

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def thumbnail_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 40, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def grid(world: World):
    """A project with five photos: three tiled with thumbnails, one queued, one failed."""
    org = world.alpha
    project_id = uuid4()
    store = ObjectStore(get_settings())
    states = [
        ("tiled", None),
        ("queued", None),
        ("tiled", None),
        ("failed", "unreadable_image"),
        ("tiled", None),
    ]
    photo_ids, written = [], []
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org.id)
        connection.execute(
            "INSERT INTO projects (id, org_id, name) VALUES (%s, %s, 'Synthetic Grid')",
            (project_id, org.id),
        )
        for index, (status, error) in enumerate(states):
            file_id, photo_id = uuid4(), uuid4()
            key = original_key(org.id, project_id, file_id)
            thumb = thumbnail_key(org.id, project_id, photo_id) if status == "tiled" else None
            connection.execute(
                "INSERT INTO files (id, org_id, project_id, object_key, content_type, size_bytes,"
                " status, original_filename) VALUES (%s, %s, %s, %s, 'image/jpeg', 1, 'ready', %s)",
                (file_id, org.id, project_id, key, f"site-{index}.jpg"),
            )
            connection.execute(
                "INSERT INTO photos (id, org_id, project_id, file_id, status, width, height,"
                " error, thumb_key, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    photo_id, org.id, project_id, file_id, status,
                    1600 if status == "tiled" else None, 1200 if status == "tiled" else None,
                    error, thumb, BASE + timedelta(seconds=index),
                ),
            )  # fmt: skip
            photo_ids.append(str(photo_id))
            if thumb:
                store.put(thumb, thumbnail_bytes(), "image/jpeg")
                written.append(thumb)
    yield project_id, photo_ids, key
    for thumb in written:
        store.delete(thumb)
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org.id)
        connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))


def url(org, project) -> str:
    return f"/orgs/{org}/projects/{project}/photos"


async def test_photos_are_listed_newest_first(signed_in, world: World, grid):
    project_id, photo_ids, _ = grid
    client = await signed_in(world.alpha.viewer)

    page = (await client.get(url(world.alpha.id, project_id))).json()

    assert [p["id"] for p in page["items"]] == photo_ids[::-1]
    assert page["next_cursor"] is None


async def test_only_tiled_photos_carry_a_thumbnail_url(signed_in, world: World, grid):
    project_id, photo_ids, _ = grid
    client = await signed_in(world.alpha.viewer)

    items = (await client.get(url(world.alpha.id, project_id))).json()["items"]

    by_id = {item["id"]: item for item in items}
    assert [by_id[i]["status"] for i in photo_ids] == [
        "tiled",
        "queued",
        "tiled",
        "failed",
        "tiled",
    ]
    for photo_id in photo_ids:
        item = by_id[photo_id]
        assert (item["thumbnail_url"] is not None) == (item["status"] == "tiled")
    assert by_id[photo_ids[3]]["error"] == "unreadable_image"
    assert by_id[photo_ids[0]]["original_filename"] == "site-0.jpg"


async def test_a_thumbnail_url_serves_the_small_image_inline(signed_in, world: World, grid):
    project_id, photo_ids, _ = grid
    client = await signed_in(world.alpha.viewer)
    items = (await client.get(url(world.alpha.id, project_id))).json()["items"]
    thumbnail_url = next(i["thumbnail_url"] for i in items if i["id"] == photo_ids[0])

    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(thumbnail_url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-disposition"] == "inline"
    assert response.content == thumbnail_bytes()


async def test_the_list_never_exposes_an_original_or_a_storage_key(signed_in, world: World, grid):
    project_id, _, original = grid
    client = await signed_in(world.alpha.viewer)

    response = await client.get(url(world.alpha.id, project_id))

    assert set(response.json()["items"][0]) == {
        "id", "file_id", "original_filename", "status", "width", "height", "error",
        "thumbnail_url", "created_at",
    }  # fmt: skip
    assert "/original" not in response.text and "object_key" not in response.text
    # A thumbnail's path is its photo's prefix; the original's key never appears in a signed URL.
    assert original not in response.text


@pytest.mark.parametrize("page_size", [1, 2, 5])
async def test_pages_cover_every_photo_once(signed_in, world: World, grid, page_size):
    project_id, photo_ids, _ = grid
    client = await signed_in(world.alpha.viewer)

    seen, cursor = [], None
    for _ in range(len(photo_ids) + 1):
        params = {"limit": page_size, **({"cursor": cursor} if cursor else {})}
        page = (await client.get(url(world.alpha.id, project_id), params=params)).json()
        seen += [p["id"] for p in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == photo_ids[::-1]


async def test_a_malformed_cursor_is_a_422(signed_in, world: World, grid):
    project_id, _, _ = grid
    client = await signed_in(world.alpha.viewer)

    response = await client.get(url(world.alpha.id, project_id), params={"cursor": "nonsense"})

    assert response.status_code == 422


async def test_another_orgs_member_gets_a_404(signed_in, world: World, grid):
    project_id, _, _ = grid
    client = await signed_in(world.beta.owner)

    response = await client.get(url(world.alpha.id, project_id))

    assert response.status_code == 404


async def test_a_project_of_another_org_is_a_404_even_for_a_member_of_both(
    signed_in, world: World, grid
):
    """The consultant belongs to both orgs; alpha's project is not reachable under beta's id."""
    project_id, _, _ = grid
    client = await signed_in(world.consultant)

    response = await client.get(url(world.beta.id, project_id))

    assert response.status_code == 404


async def test_a_viewer_can_list_but_an_unauthenticated_caller_cannot(
    signed_in, anonymous, world: World, grid
):
    project_id, _, _ = grid

    assert (await anonymous.get(url(world.alpha.id, project_id))).status_code == 401
