"""Housekeeping: abandoned uploads (row and staged object) and dead sessions."""

import hashlib
from uuid import UUID, uuid4

import psycopg
import pytest
from botocore.exceptions import ClientError

from app.db.engine import create_session_factory
from app.storage.keys import original_key, staging_key
from app.worker.cleanup import Cleanup
from tests.db.conftest import owner_conninfo, set_context
from tests.worker.conftest import Org, owner_in, query

pytestmark = pytest.mark.asyncio

HOUR = 3600
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def cleanup(engine, store, settings) -> Cleanup:
    return Cleanup(engine, create_session_factory(engine), store, settings)


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


def add_file(
    org: Org,
    store,
    *,
    status: str,
    age: int,
    staged: bool = True,
    final: bool = False,
    swept: bool = False,
) -> UUID:
    file_id = uuid4()
    staging, original = (
        staging_key(org.id, org.project_id, file_id),
        original_key(org.id, org.project_id, file_id),
    )
    if staged:
        store.put(staging, PNG, "image/png")
    if final:
        store.put(original, PNG, "image/png")
    with owner_in(org.id) as connection:
        connection.execute(
            """
            INSERT INTO files (id, org_id, project_id, object_key, content_type, status,
                               created_at, staging_swept_at)
            VALUES (%s, %s, %s, %s, 'image/png', %s, now() - make_interval(secs => %s),
                    CASE WHEN %s THEN now() END)
            """,
            (file_id, org.id, org.project_id, original, status, age, swept),
        )
    return file_id


def exists(store, key: str) -> bool:
    return store.inspect(key) is not None


def file_row(org: Org, file_id: UUID) -> tuple | None:
    rows = query(
        org.id, "SELECT status, staging_swept_at IS NOT NULL FROM files WHERE id = %s", (file_id,)
    )
    return rows[0] if rows else None


# Abandoned uploads -----------------------------------------------------------------


async def test_an_abandoned_upload_loses_its_row_and_its_staged_object(cleanup, org, store):
    file_id = add_file(org, store, status="pending", age=2 * HOUR, final=True)

    report = await cleanup.run_once()

    assert report.uploads_removed == 1
    assert file_row(org, file_id) is None
    assert not exists(store, staging_key(org.id, org.project_id, file_id))
    assert not exists(store, original_key(org.id, org.project_id, file_id))


async def test_removing_an_upload_is_audited_with_no_actor(cleanup, org, store):
    file_id = add_file(org, store, status="pending", age=2 * HOUR)

    await cleanup.run_once()

    events = query(
        org.id,
        "SELECT action, actor_user_id, target_type, target_id FROM audit_events",
    )
    assert events == [("upload.abandoned_removed", None, "file", file_id)]


async def test_an_upload_stuck_completing_is_removed_like_an_abandoned_one(cleanup, org, store):
    file_id = add_file(org, store, status="completing", age=2 * HOUR)

    report = await cleanup.run_once()

    assert report.uploads_removed == 1
    assert file_row(org, file_id) is None
    assert not exists(store, staging_key(org.id, org.project_id, file_id))


async def test_a_recent_pending_upload_is_left_alone(cleanup, org, store):
    file_id = add_file(org, store, status="pending", age=60)

    report = await cleanup.run_once()

    assert report.uploads_removed == 0
    assert file_row(org, file_id) == ("pending", False)
    assert exists(store, staging_key(org.id, org.project_id, file_id))


async def test_a_pending_upload_younger_than_the_configured_age_is_left_alone(cleanup, org, store):
    # Past the 15-minute floor, inside the hour the settings allow.
    file_id = add_file(org, store, status="pending", age=30 * 60)

    await cleanup.run_once()

    assert file_row(org, file_id) == ("pending", False)


async def test_a_finished_uploads_staging_key_is_swept_once_and_its_file_kept(cleanup, org, store):
    file_id = add_file(org, store, status="ready", age=2 * HOUR, final=True)  # a replayed POST

    first = await cleanup.run_once()
    second = await cleanup.run_once()

    assert (first.staging_swept, second.staging_swept) == (1, 0)
    assert file_row(org, file_id) == ("ready", True)
    assert not exists(store, staging_key(org.id, org.project_id, file_id))
    assert exists(store, original_key(org.id, org.project_id, file_id))


async def test_a_failed_upload_is_swept_too(cleanup, org, store):
    file_id = add_file(org, store, status="failed", age=2 * HOUR)

    await cleanup.run_once()

    assert file_row(org, file_id) == ("failed", True)
    assert not exists(store, staging_key(org.id, org.project_id, file_id))


async def test_a_recently_finished_upload_keeps_its_staging_key_until_the_post_expires(
    cleanup, org, store
):
    file_id = add_file(org, store, status="ready", age=60)

    report = await cleanup.run_once()

    assert report.staging_swept == 0
    assert exists(store, staging_key(org.id, org.project_id, file_id))


async def test_an_already_swept_upload_is_not_touched_again(cleanup, org, store):
    file_id = add_file(org, store, status="ready", age=2 * HOUR, swept=True)

    report = await cleanup.run_once()

    assert report.staging_swept == 0
    assert exists(store, staging_key(org.id, org.project_id, file_id))


async def test_each_orgs_uploads_are_handled_under_its_own_context(cleanup, org, other_org, store):
    mine = add_file(org, store, status="pending", age=2 * HOUR)
    theirs = add_file(other_org, store, status="pending", age=2 * HOUR)

    report = await cleanup.run_once()

    assert report.uploads_removed == 2
    assert (file_row(org, mine), file_row(other_org, theirs)) == (None, None)
    assert [row[0] for row in query(org.id, "SELECT target_id FROM audit_events")] == [mine]
    assert [row[0] for row in query(other_org.id, "SELECT target_id FROM audit_events")] == [theirs]


async def test_a_storage_failure_keeps_the_row_so_the_next_run_finishes_the_job(
    cleanup, org, store, monkeypatch
):
    file_id = add_file(org, store, status="pending", age=2 * HOUR)
    real_delete = type(store).delete

    def refuse(self, key):
        raise ClientError({"Error": {"Code": "503", "Message": "down"}}, "DeleteObject")

    monkeypatch.setattr(type(store), "delete", refuse)
    failed = await cleanup.run_once()
    assert failed.uploads_removed == 0
    assert file_row(org, file_id) == ("pending", False)  # not orphaned: row and object remain

    monkeypatch.setattr(type(store), "delete", real_delete)
    retried = await cleanup.run_once()

    assert retried.uploads_removed == 1
    assert file_row(org, file_id) is None
    assert not exists(store, staging_key(org.id, org.project_id, file_id))


async def test_one_failing_upload_does_not_stop_the_others(cleanup, org, store, monkeypatch):
    bad = add_file(org, store, status="pending", age=3 * HOUR)
    good = add_file(org, store, status="pending", age=2 * HOUR)
    real_delete = type(store).delete

    def refuse_one(self, key):
        if str(bad) in key:
            raise ClientError({"Error": {"Code": "500", "Message": "x"}}, "DeleteObject")
        real_delete(self, key)

    monkeypatch.setattr(type(store), "delete", refuse_one)

    report = await cleanup.run_once()

    assert report.uploads_removed == 1
    assert (file_row(org, bad), file_row(org, good)) == (("pending", False), None)
    monkeypatch.setattr(type(store), "delete", real_delete)
    await cleanup.run_once()


async def test_nothing_to_do_reports_zeros(cleanup, org):
    report = await cleanup.run_once()

    assert (report.uploads_removed, report.staging_swept) == (0, 0)


# Sessions --------------------------------------------------------------------------


@pytest.fixture
def user_id() -> UUID:
    """A user to own sessions. The suite does not rely on another module having made one."""
    created = uuid4()
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, user_id=created)
        connection.execute(
            "INSERT INTO users (id, email, display_name) VALUES (%s, %s, 'Session owner')",
            (created, f"sessions-{created}@test.example"),
        )
    return created


def add_session(user_id: UUID, *, expired_for: int) -> bytes:
    token_hash = hashlib.sha256(uuid4().bytes).digest()
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        connection.execute("SET LOCAL ROLE inspection_auth")
        connection.execute(
            "INSERT INTO sessions (user_id, token_sha256, expires_at)"
            " VALUES (%s, %s, now() + make_interval(secs => %s))",
            (user_id, token_hash, -expired_for),
        )
    return token_hash


def session_exists(token_hash: bytes) -> bool:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        connection.execute("SET LOCAL ROLE inspection_auth")
        return (
            connection.execute(
                "SELECT count(*) FROM sessions WHERE token_sha256 = %s", (token_hash,)
            ).fetchone()[0]
            == 1
        )


async def test_dead_sessions_are_purged_and_live_ones_kept(cleanup, user_id):
    dead = add_session(user_id, expired_for=3 * 24 * HOUR)
    recent = add_session(user_id, expired_for=60)
    live = add_session(user_id, expired_for=-24 * HOUR)

    report = await cleanup.run_once()

    assert report.sessions_purged >= 1
    assert (session_exists(dead), session_exists(recent), session_exists(live)) == (
        False,
        True,
        True,
    )


# Photos whose job failed ---------------------------------------------------------


async def test_a_photo_whose_job_failed_without_telling_it_is_marked_failed(cleanup, org, store):
    from tests.worker.conftest import add_photo, job_state, photo_state
    from tests.worker.imaging import encode, gradient

    photo = add_photo(org, store, encode(gradient(400, 300)))
    query(org.id, "UPDATE photos SET status = 'processing' WHERE id = %s", (photo.id,))
    query(
        org.id,
        "UPDATE jobs SET status = 'failed', attempts = 5, finished_at = now(),"
        " last_error = 'worker lost after the last attempt'",
    )

    report = await cleanup.run_once()

    assert report.photos_failed == 1
    assert photo_state(photo) == ("failed", None, None, "worker_lost")
    assert job_state(org.id, photo.id)[0] == "failed"
    assert (await cleanup.run_once()).photos_failed == 0


async def test_a_job_that_kills_its_worker_ends_with_a_failed_photo_after_cleanup(
    cleanup, org, store, engine
):
    """The claim sweep fails the job in SQL; cleanup is what carries that to the photo."""
    from app.worker.jobs import JobQueue
    from tests.worker.conftest import add_photo, job_state, photo_state
    from tests.worker.imaging import encode, gradient

    photo = add_photo(org, store, encode(gradient(400, 300)), max_attempts=1)
    queue = JobQueue(
        engine, worker_id="dies", kinds=["tile_photo"], lock_seconds=60, backoff_seconds=1
    )
    assert await queue.claim() is not None  # then the worker dies
    query(org.id, "UPDATE photos SET status = 'processing' WHERE id = %s", (photo.id,))
    query(org.id, "UPDATE jobs SET locked_until = now() - interval '1 second'")

    assert await queue.claim() is None  # the sweep fails the job instead of handing it out
    assert job_state(org.id, photo.id)[0] == "failed"
    assert photo_state(photo)[0] == "processing"

    await cleanup.run_once()

    assert photo_state(photo)[0] == "failed"


async def test_a_rejected_upload_whose_bytes_were_left_behind_loses_them(cleanup, org, store):
    file_id = add_file(org, store, status="failed", age=2 * HOUR, final=True)

    await cleanup.run_once()

    assert not exists(store, original_key(org.id, org.project_id, file_id))
    assert not exists(store, staging_key(org.id, org.project_id, file_id))


async def test_one_failing_step_does_not_stop_the_others(cleanup, org, store, monkeypatch, user_id):
    from sqlalchemy.exc import OperationalError

    async def broken(self):
        raise OperationalError("SELECT 1", {}, Exception("statement timeout"))

    monkeypatch.setattr(type(cleanup), "_stuck_photos", broken)
    file_id = add_file(org, store, status="pending", age=2 * HOUR)
    dead = add_session(user_id, expired_for=3 * 24 * HOUR)

    report = await cleanup.run_once()

    assert (report.uploads_removed, report.photos_failed) == (1, 0)
    assert file_row(org, file_id) is None
    assert not session_exists(dead)
