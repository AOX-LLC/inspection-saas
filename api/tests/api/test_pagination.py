"""Keyset pagination of the file list: every row once, in order, however the pages fall."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from app.pagination import Cursor, decode_cursor
from app.storage.keys import original_key
from tests.api.conftest import World
from tests.db.conftest import owner_conninfo, set_context

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def paged_project(world: World):
    """A project with seven files; three share a timestamp, to test the id tie-break."""
    org = world.alpha
    project_id = uuid4()
    stamps = [BASE + timedelta(seconds=n) for n in (0, 1, 2, 2, 2, 3, 4)]
    expected: list[tuple[datetime, UUID]] = []
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org.id)
        connection.execute(
            "INSERT INTO projects (id, org_id, name) VALUES (%s, %s, 'Synthetic Paged')",
            (project_id, org.id),
        )
        for stamp in stamps:
            file_id = uuid4()
            connection.execute(
                "INSERT INTO files (id, org_id, project_id, object_key, content_type,"
                " size_bytes, status, original_filename, created_at)"
                " VALUES (%s, %s, %s, %s, 'image/png', 1, 'ready', 'p.png', %s)",
                (file_id, org.id, project_id, original_key(org.id, project_id, file_id), stamp),
            )
            expected.append((stamp, file_id))
    yield project_id, [str(file_id) for _, file_id in sorted(expected)]
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org.id)
        connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))


@pytest.mark.parametrize("page_size", [1, 2, 3, 7, 100])
async def test_pages_cover_every_row_once_in_order(
    signed_in, world: World, paged_project, page_size
):
    project_id, expected = paged_project
    client = await signed_in(world.alpha.viewer)
    url = f"/orgs/{world.alpha.id}/projects/{project_id}/files"

    seen: list[str] = []
    cursor = None
    for _ in range(len(expected) + 1):
        query = f"?limit={page_size}" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url + query)).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == expected


async def test_the_last_page_has_no_cursor(signed_in, world: World, paged_project):
    project_id, expected = paged_project
    client = await signed_in(world.alpha.viewer)

    page = (await client.get(f"/orgs/{world.alpha.id}/projects/{project_id}/files?limit=7")).json()

    assert len(page["items"]) == len(expected)
    assert page["next_cursor"] is None


@pytest.mark.parametrize(
    "cursor",
    ["", "not-a-cursor", "MjAyNi0wMS0wMXxub3QtYS11dWlk", "x" * 500, "%00"],
)
async def test_a_malformed_cursor_is_a_422(signed_in, world: World, paged_project, cursor):
    project_id, _ = paged_project
    client = await signed_in(world.alpha.viewer)

    response = await client.get(
        f"/orgs/{world.alpha.id}/projects/{project_id}/files", params={"cursor": cursor}
    )

    assert response.status_code == 422


async def test_a_cursor_does_not_reach_another_orgs_rows(signed_in, world: World, paged_project):
    """A cursor is only a position: a beta file's cursor shows alpha nothing of beta."""
    project_id, _ = paged_project
    beta_cursor = Cursor(created_at=BASE - timedelta(days=1), id=world.beta.file_id).encode()
    client = await signed_in(world.alpha.viewer)

    page = (
        await client.get(
            f"/orgs/{world.alpha.id}/projects/{project_id}/files", params={"cursor": beta_cursor}
        )
    ).json()

    assert str(world.beta.file_id) not in {item["id"] for item in page["items"]}


def test_a_cursor_round_trips():
    cursor = Cursor(created_at=BASE + timedelta(microseconds=123456), id=uuid4())

    assert decode_cursor(cursor.encode()) == cursor


async def test_the_offset_parameter_is_gone(signed_in, world: World, paged_project):
    project_id, expected = paged_project
    client = await signed_in(world.alpha.viewer)

    page = (
        await client.get(f"/orgs/{world.alpha.id}/projects/{project_id}/files?limit=2&offset=5")
    ).json()

    assert [item["id"] for item in page["items"]] == expected[:2]
