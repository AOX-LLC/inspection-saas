"""Projects and file transfer: roles, the upload pipeline, and downloads."""

import io
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from PIL import Image

from app.config import get_settings
from app.storage.keys import original_key, staging_key
from app.storage.s3 import ObjectStore
from tests.api.conftest import World
from tests.db.conftest import owner_conninfo, set_context

pytestmark = pytest.mark.asyncio


def image_bytes(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def files_url(org, project) -> str:
    return f"/orgs/{org}/projects/{project}/files"


async def create_upload(client, org, project, *, content_type="image/png", size=1000, name="a.png"):
    return await client.post(
        files_url(org, project),
        json={"filename": name, "content_type": content_type, "size_bytes": size},
    )


async def send_to_store(upload: dict, body: bytes) -> httpx.Response:
    """What a browser does with the presigned POST."""
    async with httpx.AsyncClient(timeout=10) as http:
        return await http.post(
            upload["upload"]["url"],
            data=upload["upload"]["fields"],
            files={"file": ("upload", body)},
        )


async def fetch(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as http:
        return await http.get(url)


def audit_actions(org_id: UUID) -> list[str]:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org_id)
        rows = connection.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'file.%' ORDER BY occurred_at, id"
        ).fetchall()
    return [row[0] for row in rows]


# Reading ----------------------------------------------------------------------


async def test_a_member_lists_their_orgs_projects(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    response = await client.get(f"/orgs/{world.alpha.id}/projects")

    assert response.status_code == 200
    names = {p["name"] for p in response.json()["items"]}
    assert names == {"Synthetic Main alpha", "Synthetic Other alpha"}
    assert not any("beta" in name for name in names)
    assert all(set(p) == {"id", "name", "created_at"} for p in response.json()["items"])


async def test_project_lists_are_paginated(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    first = (await client.get(f"/orgs/{world.alpha.id}/projects?limit=1")).json()
    second = (await client.get(f"/orgs/{world.alpha.id}/projects?limit=1&offset=1")).json()

    assert len(first["items"]) == 1 and first["has_more"] is True
    assert len(second["items"]) == 1 and second["has_more"] is False
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert (await client.get(f"/orgs/{world.alpha.id}/projects?limit=101")).status_code == 422


async def test_a_member_reads_one_project(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    response = await client.get(f"/orgs/{world.alpha.id}/projects/{world.alpha.project_id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(world.alpha.project_id)


async def test_a_non_member_gets_the_same_404_as_a_missing_org(signed_in, world: World):
    client = await signed_in(world.alpha.owner)

    real_but_foreign = await client.get(f"/orgs/{world.beta.id}/projects")
    missing = await client.get(f"/orgs/{uuid4()}/projects")

    assert real_but_foreign.status_code == missing.status_code == 404
    assert real_but_foreign.json() == missing.json()


async def test_files_are_listed_for_the_project(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    response = await client.get(files_url(world.alpha.id, world.alpha.project_id))

    assert response.status_code == 200
    items = response.json()["items"]
    ids = {i["id"] for i in items}
    # Other tests in this module add files to the project, so check membership.
    assert str(world.alpha.file_id) in ids
    assert str(world.beta.file_id) not in ids
    assert "object_key" not in items[0]


# Roles ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "inspector"])
async def test_writers_can_start_an_upload(signed_in, world: World, role, cleanup):
    client = await signed_in(getattr(world.alpha, role))

    response = await create_upload(client, world.alpha.id, world.alpha.project_id)

    assert response.status_code == 201
    cleanup.append(
        original_key(world.alpha.id, world.alpha.project_id, UUID(response.json()["file_id"]))
    )


async def test_a_viewer_cannot_write(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    start = await create_upload(client, world.alpha.id, world.alpha.project_id)
    complete = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{world.alpha.file_id}/complete"
    )

    assert start.status_code == 403
    assert complete.status_code == 403


async def test_a_viewer_can_still_download(signed_in, world: World):
    client = await signed_in(world.alpha.viewer)

    response = await client.get(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{world.alpha.file_id}/download"
    )

    assert response.status_code == 200


async def test_a_non_member_cannot_tell_a_role_from_a_missing_org(signed_in, world: World):
    client = await signed_in(world.alpha.owner)

    response = await create_upload(client, world.beta.id, world.beta.project_id)

    assert response.status_code == 404


# Upload ----------------------------------------------------------------------


async def test_upload_complete_and_download_round_trip(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    png = image_bytes("PNG")
    start = await create_upload(client, world.alpha.id, world.alpha.project_id, size=len(png))
    assert start.status_code == 201
    upload = start.json()
    file_id = UUID(upload["file_id"])
    cleanup.append(original_key(world.alpha.id, world.alpha.project_id, file_id))
    assert upload["expires_in"] == 300
    assert upload["upload"]["url"].startswith(get_settings().s3_public_endpoint)

    stored = await send_to_store(upload, png)
    assert stored.status_code in (200, 201, 204), stored.text

    done = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{file_id}/complete"
    )
    assert done.status_code == 200
    assert done.json()["status"] == "ready"
    assert done.json()["size_bytes"] == len(png)

    link = await client.get(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{file_id}/download"
    )
    assert set(link.json()) == {"url", "expires_in"}
    fetched = await fetch(link.json()["url"])
    assert fetched.status_code == 200
    assert fetched.content == png
    assert fetched.headers["content-type"] == "image/png"
    assert (
        fetched.headers["content-disposition"] == f'attachment; filename="inspection-{file_id}.png"'
    )

    actions = audit_actions(world.alpha.id)
    assert {"file.upload_presigned", "file.upload_completed", "file.download_presigned"} <= set(
        actions
    )


async def test_audit_events_never_hold_signed_urls(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    start = await create_upload(client, world.alpha.id, world.alpha.project_id)
    cleanup.append(
        original_key(world.alpha.id, world.alpha.project_id, UUID(start.json()["file_id"]))
    )

    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=world.alpha.id)
        details = connection.execute("SELECT detail::text FROM audit_events").fetchall()
    assert not any("X-Amz" in row[0] or "http" in row[0] for row in details)


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "image/svg+xml", "image/gif", "application/pdf", "image/jpeg; charset=utf-8", ""],
)
async def test_only_jpeg_png_and_webp_are_accepted(signed_in, world: World, content_type):
    client = await signed_in(world.alpha.inspector)

    response = await create_upload(
        client, world.alpha.id, world.alpha.project_id, content_type=content_type
    )

    assert response.status_code in (415, 422)


async def test_oversized_declarations_are_refused(signed_in, world: World):
    client = await signed_in(world.alpha.inspector)

    too_big = await create_upload(
        client, world.alpha.id, world.alpha.project_id, size=50 * 1024 * 1024 + 1
    )
    zero = await create_upload(client, world.alpha.id, world.alpha.project_id, size=0)

    assert too_big.status_code == 413
    assert zero.status_code == 422


async def test_unfinished_uploads_are_capped_per_org(signed_in, world: World, monkeypatch, cleanup):
    from app.files import router

    client = await signed_in(world.alpha.inspector)
    pending = await _count_pending(world.alpha.id)
    monkeypatch.setattr(router, "MAX_PENDING_UPLOADS", pending + 1)

    first = await create_upload(client, world.alpha.id, world.alpha.project_id)
    second = await create_upload(client, world.alpha.id, world.alpha.project_id)

    cleanup.append(
        original_key(world.alpha.id, world.alpha.project_id, UUID(first.json()["file_id"]))
    )
    assert first.status_code == 201
    assert second.status_code == 429
    # Another org is unaffected.
    other = await signed_in(world.beta.inspector)
    ok = await create_upload(other, world.beta.id, world.beta.project_id)
    cleanup.append(original_key(world.beta.id, world.beta.project_id, UUID(ok.json()["file_id"])))
    assert ok.status_code == 201


async def _count_pending(org_id: UUID) -> int:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=org_id)
        return connection.execute("SELECT count(*) FROM files WHERE status = 'pending'").fetchone()[
            0
        ]


async def test_filenames_lose_control_and_bidi_characters(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)

    response = await create_upload(
        client, world.alpha.id, world.alpha.project_id, name="a\u202egnp.exe\u200b\x85.png"
    )

    file_id = UUID(response.json()["file_id"])
    cleanup.append(original_key(world.alpha.id, world.alpha.project_id, file_id))
    listing = await client.get(files_url(world.alpha.id, world.alpha.project_id))
    stored = next(i for i in listing.json()["items"] if i["id"] == str(file_id))
    assert stored["original_filename"] == "agnp.exe.png"


async def test_the_client_filename_never_reaches_the_key(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)

    response = await create_upload(
        client, world.alpha.id, world.alpha.project_id, name="../../etc/passwd\x00 ${filename}.png"
    )

    assert response.status_code == 201
    upload = response.json()
    file_id = UUID(upload["file_id"])
    key = original_key(world.alpha.id, world.alpha.project_id, file_id)
    cleanup.append(key)
    assert upload["upload"]["fields"]["key"] == staging_key(
        world.alpha.id, world.alpha.project_id, file_id
    )
    listing = await client.get(files_url(world.alpha.id, world.alpha.project_id))
    stored_name = next(i for i in listing.json()["items"] if i["id"] == str(file_id))[
        "original_filename"
    ]
    assert "/" not in stored_name and "\x00" not in stored_name


async def test_the_store_refuses_bytes_larger_than_declared(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    start = await create_upload(client, world.alpha.id, world.alpha.project_id, size=100)
    upload = start.json()
    key = original_key(world.alpha.id, world.alpha.project_id, UUID(upload["file_id"]))
    cleanup.append(key)

    stored = await send_to_store(upload, b"\x89PNG\r\n\x1a\n" + b"0" * 500)

    assert stored.status_code >= 400
    done = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{upload['file_id']}/complete"
    )
    assert done.status_code == 409  # nothing was stored


async def test_complete_before_the_upload_is_a_conflict(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    start = await create_upload(client, world.alpha.id, world.alpha.project_id)
    cleanup.append(
        original_key(world.alpha.id, world.alpha.project_id, UUID(start.json()["file_id"]))
    )

    done = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{start.json()['file_id']}/complete"
    )

    assert done.status_code == 409


@pytest.mark.parametrize(
    ("declared", "body"),
    [
        ("image/png", b"<html><script>alert(1)</script></html>"),
        ("image/png", b"GIF89a" + b"0" * 20),
        ("image/jpeg", b"\x89PNG\r\n\x1a\n" + b"0" * 20),
        ("image/webp", b"RIFF\x00\x00\x00\x00WAVE" + b"0" * 20),
    ],
    ids=["html-as-png", "gif-as-png", "png-as-jpeg", "wav-as-webp"],
)
async def test_complete_checks_the_magic_bytes(signed_in, world: World, declared, body, cleanup):
    client = await signed_in(world.alpha.inspector)
    start = await create_upload(
        client, world.alpha.id, world.alpha.project_id, content_type=declared, size=len(body) + 10
    )
    upload = start.json()
    file_id = UUID(upload["file_id"])
    key = original_key(world.alpha.id, world.alpha.project_id, file_id)
    cleanup.append(key)
    # The store only enforces the declared type, not the bytes, so this lands.
    assert (await send_to_store(upload, body)).status_code in (200, 201, 204)

    done = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{file_id}/complete"
    )

    assert done.status_code == 422
    listing = await client.get(files_url(world.alpha.id, world.alpha.project_id))
    assert next(i for i in listing.json()["items"] if i["id"] == str(file_id))["status"] == "failed"
    store = ObjectStore(get_settings())
    assert store.inspect(key) is None  # rejected bytes are removed
    assert store.inspect(staging_key(world.alpha.id, world.alpha.project_id, file_id)) is None
    link = await client.get(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{file_id}/download"
    )
    assert link.status_code == 409


@pytest.mark.parametrize(
    ("declared", "fmt"), [("image/png", "PNG"), ("image/jpeg", "JPEG"), ("image/webp", "WEBP")]
)
async def test_each_allowed_type_passes_its_own_check(
    signed_in, world: World, declared, fmt, cleanup
):
    client = await signed_in(world.alpha.inspector)
    body = image_bytes(fmt)
    start = await create_upload(
        client, world.alpha.id, world.alpha.project_id, content_type=declared, size=len(body)
    )
    upload = start.json()
    cleanup.append(original_key(world.alpha.id, world.alpha.project_id, UUID(upload["file_id"])))
    assert (await send_to_store(upload, body)).status_code in (200, 201, 204)

    done = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{upload['file_id']}/complete"
    )

    assert done.status_code == 200


async def test_a_finished_upload_cannot_be_completed_again(signed_in, world: World):
    client = await signed_in(world.alpha.inspector)

    again = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{world.alpha.file_id}/complete"
    )

    assert again.status_code == 409


async def test_downloads_force_the_stored_type_whatever_the_object_says(signed_in, world: World):
    """An object whose own metadata claims HTML is still served as its database type."""
    store = ObjectStore(get_settings())
    key = original_key(world.alpha.id, world.alpha.project_id, world.alpha.file_id)
    store.put(key, image_bytes("JPEG"), "text/html")
    client = await signed_in(world.alpha.viewer)

    link = await client.get(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{world.alpha.file_id}/download"
    )
    fetched = await fetch(link.json()["url"])

    assert fetched.headers["content-type"] == "image/jpeg"
    assert fetched.headers["content-disposition"].startswith("attachment;")


# The staging key ------------------------------------------------------------------


async def _finished_upload(client, world: World, body: bytes, cleanup) -> tuple[dict, UUID]:
    start = await create_upload(client, world.alpha.id, world.alpha.project_id, size=len(body))
    upload = start.json()
    file_id = UUID(upload["file_id"])
    cleanup.append(original_key(world.alpha.id, world.alpha.project_id, file_id))
    assert (await send_to_store(upload, body)).status_code in (200, 201, 204)
    done = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{file_id}/complete"
    )
    assert done.status_code == 200, done.text
    return upload, file_id


async def test_the_presigned_post_never_targets_the_final_key(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    start = await create_upload(client, world.alpha.id, world.alpha.project_id)
    file_id = UUID(start.json()["file_id"])
    cleanup.append(original_key(world.alpha.id, world.alpha.project_id, file_id))

    key = start.json()["upload"]["fields"]["key"]

    assert key == staging_key(world.alpha.id, world.alpha.project_id, file_id)
    assert key != original_key(world.alpha.id, world.alpha.project_id, file_id)


async def test_a_finished_upload_cannot_be_overwritten_through_its_post(
    signed_in, world: World, cleanup
):
    """The presigned POST outlives `complete` by minutes; the final object must not move."""
    client = await signed_in(world.alpha.inspector)
    png = image_bytes("PNG")
    upload, file_id = await _finished_upload(client, world, png, cleanup)

    replay = await send_to_store(upload, b"\x89PNG\r\n\x1a\n" + b"X" * 50)

    assert replay.status_code in (200, 201, 204)  # the store still honours its own policy
    link = await client.get(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{file_id}/download"
    )
    assert (await fetch(link.json()["url"])).content == png


async def test_complete_removes_the_staged_object(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    _, file_id = await _finished_upload(client, world, image_bytes("PNG"), cleanup)

    store = ObjectStore(get_settings())

    assert store.inspect(staging_key(world.alpha.id, world.alpha.project_id, file_id)) is None
    assert store.inspect(original_key(world.alpha.id, world.alpha.project_id, file_id)) is not None


async def test_the_final_object_carries_the_database_content_type(signed_in, world: World, cleanup):
    client = await signed_in(world.alpha.inspector)
    _, file_id = await _finished_upload(client, world, image_bytes("PNG"), cleanup)

    key = original_key(world.alpha.id, world.alpha.project_id, file_id)
    head = ObjectStore(get_settings())._operations.head_object(
        Bucket=get_settings().s3_bucket, Key=key
    )

    assert head["ContentType"] == "image/png"


async def test_a_swap_after_the_check_is_caught_on_the_copy(
    signed_in, world: World, cleanup, monkeypatch
):
    """If the bytes that land at the final key are not an image, the upload fails."""
    client = await signed_in(world.alpha.inspector)
    body = image_bytes("PNG")
    start = await create_upload(client, world.alpha.id, world.alpha.project_id, size=len(body))
    upload = start.json()
    file_id = UUID(upload["file_id"])
    key = original_key(world.alpha.id, world.alpha.project_id, file_id)
    cleanup.append(key)
    assert (await send_to_store(upload, body)).status_code in (200, 201, 204)

    def hostile_copy(self, source, target, *, content_type, if_match):
        # As if the staged object had been swapped between check and copy and the
        # store's precondition had not caught it.
        self.put(target, b"<html>not an image</html>", content_type)

    monkeypatch.setattr(ObjectStore, "copy", hostile_copy)
    done = await client.post(
        f"{files_url(world.alpha.id, world.alpha.project_id)}/{file_id}/complete"
    )

    assert done.status_code == 422
    store = ObjectStore(get_settings())
    assert store.inspect(key) is None
    assert store.inspect(staging_key(world.alpha.id, world.alpha.project_id, file_id)) is None
