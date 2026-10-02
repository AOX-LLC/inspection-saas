"""photos, tiles and jobs: isolation, composite keys, the id-only payload, and wake-ups."""

import json
import select
from uuid import uuid4

import psycopg
import pytest

from tests.db.conftest import (
    TENANT_A,
    TENANT_B,
    Tenant,
    app_conninfo,
    owner_conninfo,
    set_context,
    worker_conninfo,
)

TABLES = ("photos", "tiles", "jobs")


def insert_photo(connection: psycopg.Connection, tenant: Tenant, *, file_id=None) -> str:
    photo_id = uuid4()
    connection.execute(
        "INSERT INTO photos (id, org_id, project_id, file_id) VALUES (%s, %s, %s, %s)",
        (photo_id, tenant.org_id, tenant.project_id, file_id or tenant.file_id),
    )
    return photo_id


def tile_key_for(tenant: Tenant, photo_id, name: str | None = None) -> str:
    return (
        f"orgs/{tenant.org_id}/projects/{tenant.project_id}/photos/{photo_id}/tiles/"
        f"{name or uuid4()}.jpg"
    )


def insert_tile(connection: psycopg.Connection, tenant: Tenant, photo_id) -> None:
    connection.execute(
        """
        INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale,
                           width, height, object_key)
        VALUES (%s, %s, 0, 0, 0, 640, 640, 1, 640, 640, %s)
        """,
        (tenant.org_id, photo_id, tile_key_for(tenant, photo_id)),
    )


def insert_job(connection: psycopg.Connection, tenant: Tenant, payload: dict | None = None) -> None:
    connection.execute(
        "INSERT INTO jobs (org_id, kind, payload) VALUES (%s, 'tile_photo', %s::jsonb)",
        (tenant.org_id, json.dumps(payload if payload is not None else {})),
    )


@pytest.mark.parametrize("table", TABLES)
def test_an_unset_context_reads_nothing_and_writes_nothing(owner_conn, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match=r"app\.org_id is not set"):
        owner_conn.execute(f"SELECT * FROM {table}").fetchall()
        insert_job(owner_conn, TENANT_A)


def test_a_photo_is_invisible_to_another_org(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    photo_id = insert_photo(owner_conn, TENANT_A)
    insert_tile(owner_conn, TENANT_A, photo_id)
    insert_job(owner_conn, TENANT_A)

    owner_conn.execute("SELECT set_config('app.org_id', %s, true)", (str(TENANT_B.org_id),))

    for table in TABLES:
        assert owner_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert owner_conn.execute(f"DELETE FROM {table}").rowcount == 0
    assert owner_conn.execute("UPDATE photos SET status = 'failed'").rowcount == 0


@pytest.mark.parametrize("table", TABLES)
def test_a_row_for_another_org_is_rejected(owner_conn, table):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    photo_id = insert_photo(owner_conn, TENANT_A)
    inserts = {
        "photos": lambda: insert_photo(owner_conn, TENANT_B),
        "tiles": lambda: insert_tile(owner_conn, TENANT_B, photo_id),
        "jobs": lambda: insert_job(owner_conn, TENANT_B),
    }

    with pytest.raises(psycopg.errors.Error, match=r"row-level security|foreign key"):
        inserts[table]()


def test_a_photo_cannot_point_at_another_orgs_file(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="photos_file_fkey"):
        insert_photo(owner_conn, TENANT_A, file_id=TENANT_B.file_id)


def test_a_photo_cannot_point_at_another_orgs_project(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="photos_project_fkey"):
        owner_conn.execute(
            "INSERT INTO photos (org_id, project_id, file_id) VALUES (%s, %s, %s)",
            (TENANT_A.org_id, TENANT_B.project_id, TENANT_A.file_id),
        )


def test_a_tile_cannot_point_at_another_orgs_photo(owner_conn):
    set_context(owner_conn, org_id=TENANT_B.org_id)
    photo_b = insert_photo(owner_conn, TENANT_B)
    owner_conn.execute("SELECT set_config('app.org_id', %s, true)", (str(TENANT_A.org_id),))

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="tiles_photo_fkey"):
        insert_tile(owner_conn, TENANT_A, photo_b)


def test_a_file_has_at_most_one_photo(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    insert_photo(owner_conn, TENANT_A)

    with pytest.raises(psycopg.errors.UniqueViolation, match="photos_file_id_key"):
        insert_photo(owner_conn, TENANT_A)


def test_a_tile_position_is_unique_per_photo_and_level(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    photo_id = insert_photo(owner_conn, TENANT_A)
    insert_tile(owner_conn, TENANT_A, photo_id)

    with pytest.raises(psycopg.errors.UniqueViolation, match="tiles_photo_position_key"):
        insert_tile(owner_conn, TENANT_A, photo_id)


@pytest.mark.parametrize("scale", [0, -1, 1.5])
def test_a_tile_scale_is_in_zero_to_one(owner_conn, scale):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    photo_id = insert_photo(owner_conn, TENANT_A)

    with pytest.raises(psycopg.errors.CheckViolation, match="scale"):
        owner_conn.execute(
            """
            INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale,
                               width, height, object_key)
            VALUES (%s, %s, 0, 0, 0, 10, 10, %s, 10, 10, %s)
            """,
            (TENANT_A.org_id, photo_id, scale, tile_key_for(TENANT_A, photo_id)),
        )


@pytest.mark.parametrize(
    "key",
    [
        lambda photo: (
            f"orgs/{TENANT_B.org_id}/projects/{TENANT_A.project_id}/photos/{photo}/tiles/a.jpg"
        ),
        lambda photo: (
            f"orgs/{TENANT_A.org_id}/projects/{TENANT_A.project_id}/photos/{uuid4()}/tiles/a.jpg"
        ),
        lambda photo: (
            f"orgs/{TENANT_A.org_id}/projects/{TENANT_A.project_id}/files/{TENANT_A.file_id}/original"
        ),
    ],
    ids=["other-org", "other-photo", "an-original"],
)
def test_a_tile_key_must_sit_inside_its_own_org_and_photo(owner_conn, key):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    photo_id = insert_photo(owner_conn, TENANT_A)

    with pytest.raises(psycopg.errors.CheckViolation, match="tiles_object_key_in_photo"):
        owner_conn.execute(
            """
            INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale,
                               width, height, object_key)
            VALUES (%s, %s, 0, 0, 0, 10, 10, 1, 10, 10, %s)
            """,
            (TENANT_A.org_id, photo_id, key(photo_id)),
        )


# The payload crosses tenants in the claim function, so it holds ids and nothing else.

GOOD_ID = "0b9d1c52-6f0e-4f0b-9a5e-0d1f1c2b3a4d"


def test_a_payload_of_ids_is_accepted(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)

    insert_job(owner_conn, TENANT_A, {"photo_id": GOOD_ID, "project_id": str(uuid4())})
    insert_job(owner_conn, TENANT_A, {})


@pytest.mark.parametrize(
    "payload",
    [
        {"photo_id": "not-a-uuid"},
        {"photo_id": GOOD_ID.upper()},
        {"photo_id": GOOD_ID + "x"},
        {"photo_id": 7},
        {"photo_id": None},
        {"photo_id": True},
        {"photo_id": [GOOD_ID]},
        {"photo_id": {"id": GOOD_ID}},
        {"photo_id": GOOD_ID, "note": "alice@example.test"},
        {"photo_id": GOOD_ID, "storage_key": "orgs/x/y"},
        {"alice@example.test": GOOD_ID},
        {"Photo_Id": GOOD_ID},
        {"a" * 41: GOOD_ID},
        {f"k{i}": GOOD_ID for i in range(15)},
    ],
    ids=[
        "word", "upper", "suffix", "number", "null", "bool",
        "array", "object", "extra-string", "extra-key",
        "email-as-key", "upper-key", "long-key", "too-big",
    ],
)  # fmt: skip
def test_a_payload_with_anything_but_ids_is_rejected(owner_conn, payload):
    set_context(owner_conn, org_id=TENANT_A.org_id)

    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_payload_check"):
        insert_job(owner_conn, TENANT_A, payload)


@pytest.mark.parametrize("payload", ['"text"', "[]", "5", "null"])
def test_a_payload_must_be_an_object(owner_conn, payload):
    set_context(owner_conn, org_id=TENANT_A.org_id)

    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_payload_check"):
        owner_conn.execute(
            "INSERT INTO jobs (org_id, kind, payload) VALUES (%s, 'tile_photo', %s::jsonb)",
            (TENANT_A.org_id, payload),
        )


def test_a_running_job_must_hold_a_lock(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)

    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_running_has_lock"):
        owner_conn.execute(
            "INSERT INTO jobs (org_id, kind, status) VALUES (%s, 'tile_photo', 'running')",
            (TENANT_A.org_id,),
        )


# What the API role may do ------------------------------------------------------


def test_the_app_can_record_a_photo_and_enqueue_its_job(app_conn):
    set_context(app_conn, org_id=TENANT_A.org_id)

    insert_photo(app_conn, TENANT_A)
    insert_job(app_conn, TENANT_A, {"photo_id": GOOD_ID})
    visible = app_conn.execute("SELECT count(*) FROM photos").fetchone()[0]

    assert visible == 1


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM jobs",
        "UPDATE jobs SET status = 'failed'",
        "DELETE FROM jobs",
        "UPDATE photos SET status = 'tiled'",
        "DELETE FROM photos",
        "DELETE FROM tiles",
    ],
)
def test_the_app_cannot_do_what_only_the_worker_does(app_conn, statement):
    set_context(app_conn, org_id=TENANT_A.org_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
        app_conn.execute(statement)


def test_the_app_cannot_write_tiles(app_conn):
    set_context(app_conn, org_id=TENANT_A.org_id)
    photo_id = insert_photo(app_conn, TENANT_A)

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
        insert_tile(app_conn, TENANT_A, photo_id)


# Wake-ups ----------------------------------------------------------------------


def test_enqueueing_wakes_a_listener_only_after_commit():
    with (
        psycopg.connect(worker_conninfo(), autocommit=True) as listener,
        psycopg.connect(app_conninfo()) as enqueuer,
    ):
        listener.execute("LISTEN inspection_jobs")
        set_context(enqueuer, org_id=TENANT_A.org_id)
        insert_job(enqueuer, TENANT_A)

        before_commit = select.select([listener.fileno()], [], [], 0.3)[0]
        enqueuer.rollback()
        after_rollback = list(listener.notifies(timeout=0.3))

        set_context(enqueuer, org_id=TENANT_A.org_id)
        insert_job(enqueuer, TENANT_A)
        enqueuer.commit()
        after_commit = list(listener.notifies(timeout=2, stop_after=1))
        _delete_committed_jobs()

    assert before_commit == []
    assert after_rollback == []
    assert [n.channel for n in after_commit] == ["inspection_jobs"]
    assert [n.payload for n in after_commit] == [""]


def _delete_committed_jobs() -> None:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=TENANT_A.org_id)
        connection.execute("DELETE FROM jobs")
