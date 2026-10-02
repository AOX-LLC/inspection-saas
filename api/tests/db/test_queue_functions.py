"""The queue's SECURITY DEFINER functions, called as the worker role.

Three claims are proved here. A worker holding one org's job cannot read or
write another org's rows. A job whose worker dies is handed out again once its
lock lapses. And the claim function exposes nothing beyond its four columns.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg
import pytest

from tests.db.conftest import (
    TENANT_A,
    TENANT_B,
    Tenant,
    owner_conninfo,
    set_context,
    worker_conninfo,
)

KINDS = ["tile_photo"]
PHOTO_A = uuid4()
PHOTO_B = uuid4()


# Helpers -----------------------------------------------------------------------


def tile_key(tenant: Tenant, photo_id: UUID) -> str:
    return (
        f"orgs/{tenant.org_id}/projects/{tenant.project_id}/photos/{photo_id}/tiles/{uuid4()}.jpg"
    )


@contextmanager
def owner_in(tenant: Tenant) -> Iterator[psycopg.Connection]:
    """The owner role in one committed transaction under the tenant's context."""
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=tenant.org_id)
        yield connection


def enqueue(tenant: Tenant, payload: dict | None = None, **columns) -> UUID:
    job_id = uuid4()
    names = ["id", "org_id", "kind", "payload", *columns]
    values = [job_id, tenant.org_id, "tile_photo", json.dumps(payload or {}), *columns.values()]
    placeholders = ["%s", "%s", "%s", "%s::jsonb", *["%s"] * len(columns)]
    with owner_in(tenant) as connection:
        connection.execute(
            f"INSERT INTO jobs ({', '.join(names)}) VALUES ({', '.join(placeholders)})", values
        )
    return job_id


def job_row(tenant: Tenant, job_id: UUID) -> dict:
    with owner_in(tenant) as connection:
        cursor = connection.execute(
            "SELECT status, attempts, locked_by, last_error, run_after > now() AS delayed,"
            " locked_until IS NOT NULL AS locked FROM jobs WHERE id = %s",
            (job_id,),
        )
        names = [column.name for column in cursor.description]
        return dict(zip(names, cursor.fetchone(), strict=True))


def run_owner(tenant: Tenant, sql: str, params: tuple = ()) -> None:
    with owner_in(tenant) as connection:
        connection.execute(sql, params)


@pytest.fixture(autouse=True)
def empty_queue() -> Iterator[None]:
    """Every test starts and ends with no jobs, photos, tiles or extra files."""

    def clear() -> None:
        for tenant in (TENANT_A, TENANT_B):
            with owner_in(tenant) as connection:
                connection.execute("DELETE FROM jobs")
                connection.execute("DELETE FROM tiles")
                connection.execute("DELETE FROM photos")
                connection.execute("DELETE FROM files WHERE id <> %s", (tenant.file_id,))
                connection.execute("UPDATE files SET staging_swept_at = now()")

    clear()
    yield
    clear()


@pytest.fixture
def worker() -> Iterator[psycopg.Connection]:
    """The worker role with every statement committed on its own, as the real worker runs."""
    with psycopg.connect(worker_conninfo(), autocommit=True) as connection:
        yield connection


def claim(connection: psycopg.Connection, worker_id: str = "w1", lock_seconds: int = 60):
    return connection.execute(
        "SELECT * FROM queue.jobs_claim(%s, %s, %s)", (worker_id, KINDS, lock_seconds)
    ).fetchone()


def complete(connection: psycopg.Connection, job_id: UUID, worker_id: str = "w1") -> bool:
    return connection.execute("SELECT queue.jobs_complete(%s, %s)", (job_id, worker_id)).fetchone()[
        0
    ]


def fail(
    connection: psycopg.Connection,
    job_id: UUID,
    worker_id: str = "w1",
    *,
    retryable: bool = True,
    backoff: int = 10,
) -> str | None:
    return connection.execute(
        "SELECT queue.jobs_fail(%s, %s, 'boom', %s, %s)", (job_id, worker_id, retryable, backoff)
    ).fetchone()[0]


def make_due(tenant: Tenant, job_id: UUID) -> None:
    run_owner(
        tenant, "UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = %s", (job_id,)
    )


def lapse_lock(tenant: Tenant, job_id: UUID) -> None:
    run_owner(
        tenant,
        "UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = %s",
        (job_id,),
    )


# What the claim function exposes -----------------------------------------------


def test_claim_returns_exactly_four_columns(worker):
    enqueue(TENANT_A, {"photo_id": str(PHOTO_A)})

    cursor = worker.execute("SELECT * FROM queue.jobs_claim('w1', %s, 60)", (KINDS,))

    assert [column.name for column in cursor.description] == [
        "job_id",
        "org_id",
        "kind",
        "payload",
    ]
    row = cursor.fetchone()
    assert len(row) == 4


def test_claim_declares_exactly_four_output_columns(worker):
    declared = worker.execute(
        "SELECT pg_get_function_result('queue.jobs_claim(text,text[],integer)'::regprocedure)"
    ).fetchone()[0]

    assert declared == "TABLE(job_id uuid, org_id uuid, kind text, payload jsonb)"


def test_the_other_functions_return_a_flag_a_status_or_ids(worker):
    results = {
        signature: worker.execute(
            "SELECT pg_get_function_result(%s::regprocedure)", (signature,)
        ).fetchone()[0]
        for signature in (
            "queue.jobs_complete(uuid,text)",
            "queue.jobs_fail(uuid,text,text,boolean,integer)",
            "queue.abandoned_uploads(integer,integer)",
            "queue.stuck_photos(integer)",
        )
    }

    assert results == {
        "queue.jobs_complete(uuid,text)": "boolean",
        "queue.jobs_fail(uuid,text,text,boolean,integer)": "text",
        "queue.abandoned_uploads(integer,integer)": "TABLE(org_id uuid, file_id uuid)",
        "queue.stuck_photos(integer)": "TABLE(org_id uuid, photo_id uuid)",
    }


def test_the_claimed_payload_holds_ids_and_the_row_nothing_else(worker):
    job_id = enqueue(TENANT_A, {"photo_id": str(PHOTO_A)})

    claimed_id, org_id, kind, payload = claim(worker)

    assert (claimed_id, org_id, kind, payload) == (
        job_id,
        TENANT_A.org_id,
        "tile_photo",
        {"photo_id": str(PHOTO_A)},
    )


def test_a_payload_that_is_not_ids_cannot_be_stored_so_cannot_be_claimed():
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_payload_check"):
        enqueue(TENANT_A, {"note": "customer name"})


# Claiming ----------------------------------------------------------------------


def test_an_empty_queue_claims_nothing(worker):
    assert claim(worker) is None


def test_a_claim_locks_the_job_for_one_worker(worker):
    job_id = enqueue(TENANT_A)

    first = claim(worker, "w1")
    second = claim(worker, "w2")

    assert first[0] == job_id
    assert second is None
    row = job_row(TENANT_A, job_id)
    assert (row["status"], row["attempts"], row["locked_by"], row["locked"]) == (
        "running",
        1,
        "w1",
        True,
    )


def test_a_claim_takes_jobs_from_any_org_and_names_the_org(worker):
    job_a = enqueue(TENANT_A)
    job_b = enqueue(TENANT_B)

    claimed = {claim(worker)[:2], claim(worker)[:2]}

    assert claimed == {(job_a, TENANT_A.org_id), (job_b, TENANT_B.org_id)}


def test_a_job_that_is_not_due_is_not_claimed(worker):
    enqueue(TENANT_A, run_after="2999-01-01T00:00:00Z")

    assert claim(worker) is None


def test_concurrent_workers_never_receive_the_same_job():
    job_1, job_2 = enqueue(TENANT_A), enqueue(TENANT_A)
    with (
        psycopg.connect(worker_conninfo()) as first,
        psycopg.connect(worker_conninfo()) as second,
    ):
        # Both claims stay uncommitted, so the second must skip the first's row lock.
        claimed_first = claim(first, "w1")
        claimed_second = claim(second, "w2")
        first.commit()
        second.commit()

    assert {claimed_first[0], claimed_second[0]} == {job_1, job_2}


@pytest.mark.parametrize(
    ("worker_id", "kinds"),
    [("", KINDS), ("x" * 101, KINDS), (None, KINDS), ("w1", []), ("w1", None)],
)
def test_a_claim_validates_its_arguments(worker, worker_id, kinds):
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        worker.execute("SELECT * FROM queue.jobs_claim(%s, %s, 60)", (worker_id, kinds))


def test_only_the_requested_kinds_are_claimed(worker):
    enqueue(TENANT_A)

    row = worker.execute("SELECT * FROM queue.jobs_claim('w1', ARRAY['other'], 60)").fetchone()

    assert row is None


# A worker that dies ------------------------------------------------------------


def test_a_job_whose_worker_died_is_reclaimed_after_its_lock_lapses(worker):
    job_id = enqueue(TENANT_A)
    assert claim(worker, "dead-worker")[0] == job_id
    assert claim(worker, "w2") is None  # still locked

    lapse_lock(TENANT_A, job_id)
    reclaimed = claim(worker, "w2")

    assert reclaimed[0] == job_id
    row = job_row(TENANT_A, job_id)
    assert (row["status"], row["attempts"], row["locked_by"]) == ("running", 2, "w2")
    assert row["last_error"] == "worker lost; retried"


def test_the_dead_worker_cannot_finish_a_job_it_no_longer_holds(worker):
    job_id = enqueue(TENANT_A)
    claim(worker, "dead-worker")
    lapse_lock(TENANT_A, job_id)
    claim(worker, "w2")

    assert complete(worker, job_id, "dead-worker") is False
    assert fail(worker, job_id, "dead-worker") is None
    assert job_row(TENANT_A, job_id)["status"] == "running"
    assert complete(worker, job_id, "w2") is True
    assert job_row(TENANT_A, job_id)["status"] == "succeeded"


def test_a_job_that_keeps_killing_its_worker_ends_up_failed(worker):
    job_id = enqueue(TENANT_A, max_attempts=2)

    for _ in range(2):
        assert claim(worker)[0] == job_id
        lapse_lock(TENANT_A, job_id)
    assert claim(worker) is None  # out of attempts: swept to failed, not handed out again

    row = job_row(TENANT_A, job_id)
    assert (row["status"], row["attempts"], row["locked"]) == ("failed", 2, False)
    assert row["last_error"] == "worker lost after the last attempt"


# Completing and failing --------------------------------------------------------


def test_complete_marks_the_job_and_releases_the_lock(worker):
    job_id = enqueue(TENANT_A)
    claim(worker)

    assert complete(worker, job_id) is True

    row = job_row(TENANT_A, job_id)
    assert (row["status"], row["locked"], row["locked_by"]) == ("succeeded", False, None)
    assert complete(worker, job_id) is False  # nothing left to complete


def test_another_worker_cannot_complete_or_fail_a_held_job(worker):
    job_id = enqueue(TENANT_A)
    claim(worker, "w1")

    assert complete(worker, job_id, "w2") is False
    assert fail(worker, job_id, "w2") is None
    assert job_row(TENANT_A, job_id)["locked_by"] == "w1"


def test_a_failed_attempt_is_retried_with_exponential_backoff(worker):
    job_id = enqueue(TENANT_A)
    claim(worker)

    assert fail(worker, job_id, backoff=10) == "queued"

    row = job_row(TENANT_A, job_id)
    assert (row["status"], row["delayed"], row["last_error"]) == ("queued", True, "boom")
    assert claim(worker) is None  # not due yet
    delays = []
    for _ in range(2):
        make_due(TENANT_A, job_id)
        claim(worker)
        fail(worker, job_id, backoff=10)
        with owner_in(TENANT_A) as connection:
            delays.append(
                connection.execute(
                    "SELECT extract(epoch FROM run_after - now()) FROM jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()[0]
            )
    # Attempt 2 waits about 20 s and attempt 3 about 40 s.
    assert 15 < delays[0] <= 20
    assert 35 < delays[1] <= 40


def test_backoff_is_capped_at_an_hour(worker):
    job_id = enqueue(TENANT_A, max_attempts=20, attempts=10)
    claim(worker)  # attempt 11

    fail(worker, job_id, backoff=600)

    with owner_in(TENANT_A) as connection:
        delay = connection.execute(
            "SELECT extract(epoch FROM run_after - now()) FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()[0]
    assert 3500 < delay <= 3600


def test_a_job_fails_for_good_after_its_last_attempt(worker):
    job_id = enqueue(TENANT_A, max_attempts=2)

    claim(worker)
    assert fail(worker, job_id) == "queued"
    make_due(TENANT_A, job_id)
    claim(worker)
    assert fail(worker, job_id) == "failed"

    row = job_row(TENANT_A, job_id)
    assert (row["status"], row["attempts"], row["locked"]) == ("failed", 2, False)
    make_due(TENANT_A, job_id)
    assert claim(worker) is None


def test_a_non_retryable_error_fails_at_once(worker):
    job_id = enqueue(TENANT_A)
    claim(worker)

    assert fail(worker, job_id, retryable=False) == "failed"

    assert job_row(TENANT_A, job_id)["attempts"] == 1


def test_the_recorded_error_is_truncated(worker):
    job_id = enqueue(TENANT_A)
    claim(worker)

    worker.execute(
        "SELECT queue.jobs_fail(%s, 'w1', %s, false, 1)", (job_id, "x" * 1000)
    ).fetchone()

    with owner_in(TENANT_A) as connection:
        stored = connection.execute(
            "SELECT length(last_error) FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()[0]
    assert stored == 200


# A worker holding one org's job and another org's rows --------------------------


@pytest.fixture
def both_orgs_have_photos() -> None:
    for tenant, photo_id in ((TENANT_A, PHOTO_A), (TENANT_B, PHOTO_B)):
        with owner_in(tenant) as connection:
            connection.execute(
                "INSERT INTO photos (id, org_id, project_id, file_id) VALUES (%s, %s, %s, %s)",
                (photo_id, tenant.org_id, tenant.project_id, tenant.file_id),
            )
            connection.execute(
                """
                INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale,
                                   width, height, object_key)
                VALUES (%s, %s, 0, 0, 0, 10, 10, 1, 10, 10, %s)
                """,
                (tenant.org_id, photo_id, tile_key(tenant, photo_id)),
            )


@pytest.fixture
def holding_a(worker, both_orgs_have_photos) -> Iterator[psycopg.Connection]:
    """A worker connection inside a transaction under org A's context, from a real claim."""
    enqueue(TENANT_A, {"photo_id": str(PHOTO_A)})
    _, org_id, _, _ = claim(worker)
    assert org_id == TENANT_A.org_id
    with psycopg.connect(worker_conninfo()) as connection:
        set_context(connection, org_id=org_id)
        yield connection
        connection.rollback()


@pytest.mark.parametrize("table", ["photos", "tiles", "files"])
def test_a_worker_holding_org_as_job_cannot_read_org_bs_rows(holding_a, table):
    visible = {row[0] for row in holding_a.execute(f"SELECT org_id FROM {table}").fetchall()}
    asked_for_b = holding_a.execute(
        f"SELECT count(*) FROM {table} WHERE org_id = %s", (TENANT_B.org_id,)
    ).fetchone()[0]

    assert visible == {TENANT_A.org_id}  # org A's own rows are there, and only those
    assert asked_for_b == 0


def test_a_worker_holding_org_as_job_cannot_change_org_bs_rows(holding_a):
    updated = holding_a.execute(
        "UPDATE photos SET status = 'failed' WHERE id = %s", (PHOTO_B,)
    ).rowcount
    deleted_tiles = holding_a.execute(
        "DELETE FROM tiles WHERE org_id = %s", (TENANT_B.org_id,)
    ).rowcount
    deleted_files = holding_a.execute(
        "DELETE FROM files WHERE id = %s", (TENANT_B.file_id,)
    ).rowcount

    assert (updated, deleted_tiles, deleted_files) == (0, 0, 0)
    with owner_in(TENANT_B) as connection:
        photo = connection.execute("SELECT status FROM photos WHERE id = %s", (PHOTO_B,)).fetchone()
        assert photo == ("queued",)
        assert connection.execute("SELECT count(*) FROM tiles").fetchone()[0] == 1


def test_a_worker_holding_org_as_job_cannot_write_rows_for_org_b(holding_a):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        holding_a.execute(
            """
            INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale,
                               width, height, object_key)
            VALUES (%s, %s, 0, 99, 99, 10, 10, 1, 10, 10, %s)
            """,
            (TENANT_B.org_id, PHOTO_B, tile_key(TENANT_B, PHOTO_B)),
        )


def test_a_worker_holding_org_as_job_cannot_attach_org_bs_photo_to_its_own_tile(holding_a):
    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="tiles_photo_fkey"):
        holding_a.execute(
            """
            INSERT INTO tiles (org_id, photo_id, level, x, y, src_width, src_height, scale,
                               width, height, object_key)
            VALUES (%s, %s, 0, 99, 99, 10, 10, 1, 10, 10, %s)
            """,
            (TENANT_A.org_id, PHOTO_B, tile_key(TENANT_A, PHOTO_B)),
        )


def test_a_worker_holding_org_as_job_cannot_audit_into_org_b(holding_a):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        holding_a.execute(
            "INSERT INTO audit_events (org_id, action) VALUES (%s, 'x')", (TENANT_B.org_id,)
        )


def test_a_worker_with_no_context_reads_no_rows(worker, both_orgs_have_photos):
    with (
        psycopg.connect(worker_conninfo()) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege, match=r"app\.org_id is not set"),
    ):
        connection.execute("SELECT * FROM photos").fetchall()


# What the worker may touch directly --------------------------------------------

EXPECTED_WORKER_PRIVILEGES = {
    "files": {"SELECT", "DELETE"},
    "photos": {"SELECT"},
    "tiles": {"SELECT", "INSERT", "DELETE"},
    "audit_events": {"INSERT"},
}


def test_the_worker_holds_exactly_the_table_privileges_it_needs(worker_conn):
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    tables = [
        row[0]
        for row in worker_conn.execute(
            "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        ).fetchall()
    ]
    actual = {
        table: {
            privilege
            for privilege in privileges
            if worker_conn.execute(
                "SELECT has_table_privilege('inspection_worker', %s, %s)", (table, privilege)
            ).fetchone()[0]
        }
        for table in tables
    }

    assert {table: held for table, held in actual.items() if held} == EXPECTED_WORKER_PRIVILEGES


def test_the_worker_has_only_the_column_grants_it_needs(worker_conn):
    rows = worker_conn.execute(
        """
        SELECT c.relname || '.' || a.attname || ':' || acl.privilege_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        CROSS JOIN LATERAL aclexplode(a.attacl) acl
        WHERE a.attacl IS NOT NULL AND acl.grantee = 'inspection_worker'::regrole
        """
    ).fetchall()

    assert {row[0] for row in rows} == {
        "files.staging_swept_at:UPDATE",
        "photos.status:UPDATE",
        "photos.width:UPDATE",
        "photos.height:UPDATE",
        "photos.error:UPDATE",
        "photos.updated_at:UPDATE",
        "photos.thumb_key:UPDATE",
    }


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM jobs",
        "UPDATE jobs SET status = 'queued'",
        "DELETE FROM jobs",
        "INSERT INTO jobs (org_id, kind) VALUES (gen_random_uuid(), 'tile_photo')",
        "SELECT * FROM sessions",
        "SELECT * FROM credentials",
        "SELECT * FROM users",
        "SELECT * FROM memberships",
        "SELECT * FROM orgs",
        "SELECT * FROM projects",
        "SELECT * FROM auth.verify_login('a@b.example')",
        "SET ROLE inspection_dispatcher",
        "SET ROLE inspection_auth",
    ],
)
def test_the_worker_cannot_reach_what_it_has_no_business_with(worker_conn, statement):
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable)):
        worker_conn.execute(statement)


def test_the_worker_can_delete_only_uploads_that_never_finished(worker_conn):
    add_file(TENANT_A, status="ready", age_seconds=7200)
    with psycopg.connect(worker_conninfo()) as connection:
        set_context(connection, org_id=TENANT_A.org_id)
        removed_ready = connection.execute("DELETE FROM files WHERE status = 'ready'").rowcount
        removed_failed = connection.execute("DELETE FROM files WHERE status = 'failed'").rowcount
        connection.rollback()

    assert (removed_ready, removed_failed) == (0, 0)


def test_the_worker_can_delete_a_pending_or_completing_upload():
    pending = add_file(TENANT_A, status="pending", age_seconds=7200)
    completing = add_file(TENANT_A, status="completing", age_seconds=7200)
    with psycopg.connect(worker_conninfo()) as connection:
        set_context(connection, org_id=TENANT_A.org_id)
        removed = connection.execute(
            "DELETE FROM files WHERE id = ANY(%s)", ([pending, completing],)
        ).rowcount
        connection.rollback()

    assert removed == 2


def test_the_app_role_cannot_call_the_queue_functions(app_conn):
    for call in (
        "SELECT * FROM queue.jobs_claim('w', ARRAY['tile_photo'], 60)",
        "SELECT queue.jobs_complete(gen_random_uuid(), 'w')",
        "SELECT * FROM queue.abandoned_uploads(900, 10)",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
            app_conn.execute(call)
        app_conn.rollback()


# The dispatcher ----------------------------------------------------------------


def test_the_dispatcher_owns_only_the_queue_functions(app_conn):
    owned = app_conn.execute(
        """
        SELECT d.classid::regclass::text, d.objid
        FROM pg_shdepend d
        WHERE d.refobjid = 'inspection_dispatcher'::regrole AND d.deptype = 'o'
          AND d.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
        """
    ).fetchall()
    in_queue_schema = {
        row[0]
        for row in app_conn.execute(
            "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'queue'"
        ).fetchall()
    }

    assert owned, "the dispatcher owns nothing; the functions are missing"
    assert {classid for classid, _ in owned} == {"pg_proc"}
    assert {objid for _, objid in owned} == in_queue_schema


def test_the_dispatcher_holds_only_the_grants_it_needs(app_conn):
    rows = app_conn.execute(
        """
        SELECT c.relname, acl.privilege_type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) acl
        WHERE acl.grantee = 'inspection_dispatcher'::regrole
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        """
    ).fetchall()
    granted: dict[str, set[str]] = {}
    for table, privilege in rows:
        granted.setdefault(table, set()).add(privilege)

    assert granted == {"jobs": {"SELECT", "UPDATE"}, "files": {"SELECT"}, "photos": {"SELECT"}}


def test_nobody_can_create_objects_in_the_queue_schema(app_conn):
    can_create = {
        role: app_conn.execute(
            "SELECT has_schema_privilege(%s, 'queue', 'CREATE')", (role,)
        ).fetchone()[0]
        for role in ("inspection_app", "inspection_worker", "inspection_dispatcher")
    }

    assert can_create == {
        "inspection_app": False,
        "inspection_worker": False,
        "inspection_dispatcher": False,
    }


# Abandoned uploads -------------------------------------------------------------


def add_file(tenant: Tenant, *, status: str, age_seconds: int, swept: bool = False) -> UUID:
    file_id = uuid4()
    key = f"orgs/{tenant.org_id}/projects/{tenant.project_id}/files/{file_id}/original"
    with owner_in(tenant) as connection:
        connection.execute(
            """
            INSERT INTO files (id, org_id, project_id, object_key, content_type, status,
                               created_at, staging_swept_at)
            VALUES (%s, %s, %s, %s, 'image/png', %s, now() - make_interval(secs => %s),
                    CASE WHEN %s THEN now() END)
            """,
            (file_id, tenant.org_id, tenant.project_id, key, status, age_seconds, swept),
        )
    return file_id


def abandoned(worker: psycopg.Connection, older: int = 3600, limit: int = 100) -> set[tuple]:
    rows = worker.execute("SELECT * FROM queue.abandoned_uploads(%s, %s)", (older, limit))
    return {tuple(row) for row in rows.fetchall()}


def test_old_pending_uploads_are_reported_with_their_org(worker):
    stale_a = add_file(TENANT_A, status="pending", age_seconds=7200)
    stale_b = add_file(TENANT_B, status="pending", age_seconds=7200)

    assert abandoned(worker) == {(TENANT_A.org_id, stale_a), (TENANT_B.org_id, stale_b)}


def test_a_recent_pending_upload_is_left_alone(worker):
    add_file(TENANT_A, status="pending", age_seconds=60)

    assert abandoned(worker) == set()


def test_nothing_younger_than_a_presigned_posts_lifetime_is_ever_reported(worker):
    young = add_file(TENANT_A, status="pending", age_seconds=600)

    # Asking for ten seconds still gets the 15-minute floor.
    assert abandoned(worker, older=10) == set()
    old = add_file(TENANT_A, status="pending", age_seconds=1000)
    assert abandoned(worker, older=10) == {(TENANT_A.org_id, old)}
    assert young not in {file_id for _, file_id in abandoned(worker, older=10)}


def test_a_finished_upload_is_reported_until_its_staging_key_is_swept(worker):
    unswept = add_file(TENANT_A, status="ready", age_seconds=7200)
    add_file(TENANT_A, status="ready", age_seconds=7200, swept=True)
    failed = add_file(TENANT_A, status="failed", age_seconds=7200)

    assert abandoned(worker) == {(TENANT_A.org_id, unswept), (TENANT_A.org_id, failed)}


def test_an_upload_stuck_completing_is_reported_like_a_pending_one(worker):
    stuck = add_file(TENANT_A, status="completing", age_seconds=7200)
    add_file(TENANT_A, status="completing", age_seconds=60)

    assert abandoned(worker) == {(TENANT_A.org_id, stuck)}


# Photos whose job has failed ----------------------------------------------------


def add_photo_with_job(tenant: Tenant, *, photo_status: str, job_status: str) -> UUID:
    photo_id = uuid4()
    file_id = add_file(tenant, status="ready", age_seconds=0, swept=True)
    with owner_in(tenant) as connection:
        connection.execute(
            "INSERT INTO photos (id, org_id, project_id, file_id, status)"
            " VALUES (%s, %s, %s, %s, %s)",
            (photo_id, tenant.org_id, tenant.project_id, file_id, photo_status),
        )
        connection.execute(
            "INSERT INTO jobs (org_id, kind, payload, status, attempts, locked_by, locked_until)"
            " VALUES (%s, 'tile_photo', jsonb_build_object('photo_id', %s::text), %s, 5,"
            " CASE WHEN %s = 'running' THEN 'w1' END,"
            " CASE WHEN %s = 'running' THEN now() + interval '1 minute' END)",
            (tenant.org_id, photo_id, job_status, job_status, job_status),
        )
    return photo_id


def stuck(worker: psycopg.Connection, limit: int = 100) -> set[tuple]:
    rows = worker.execute("SELECT * FROM queue.stuck_photos(%s)", (limit,))
    return {tuple(row) for row in rows.fetchall()}


@pytest.mark.parametrize("photo_status", ["queued", "processing"])
def test_a_photo_whose_job_failed_is_reported_with_its_org(worker, photo_status):
    photo = add_photo_with_job(TENANT_A, photo_status=photo_status, job_status="failed")

    assert stuck(worker) == {(TENANT_A.org_id, photo)}


@pytest.mark.parametrize(
    ("photo_status", "job_status"),
    [("tiled", "failed"), ("failed", "failed"), ("processing", "running"), ("queued", "queued")],
)
def test_a_photo_that_is_finished_or_still_has_a_live_job_is_not_stuck(
    worker, photo_status, job_status
):
    add_photo_with_job(TENANT_A, photo_status=photo_status, job_status=job_status)

    assert stuck(worker) == set()


def test_the_batch_size_is_capped(worker):
    for _ in range(3):
        add_file(TENANT_A, status="pending", age_seconds=7200)

    assert len(abandoned(worker, limit=2)) == 2
    assert len(abandoned(worker, limit=0)) == 1  # a floor of one, not an unbounded read
