"""auth.purge_sessions: the worker's only way to remove dead sessions."""

import hashlib
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest

from tests.db.conftest import TENANT_A, owner_conninfo, worker_conninfo

HOUR = 3600


def as_auth_role(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        connection.execute("SET LOCAL ROLE inspection_auth")
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def add_session(
    *, expires_in: int = 24 * HOUR, idle_for: int = 0, revoked_ago: int | None = None, age: int = 0
) -> bytes:
    """A session row with the given timings, in seconds relative to now."""
    token_hash = hashlib.sha256(uuid4().bytes).digest()
    as_auth_role(
        """
        INSERT INTO sessions (user_id, token_sha256, created_at, last_seen_at, expires_at,
                              revoked_at)
        VALUES (%s, %s, now() - make_interval(secs => %s), now() - make_interval(secs => %s),
                now() + make_interval(secs => %s),
                CASE WHEN %s::int IS NULL THEN NULL ELSE now() - make_interval(secs => %s::int) END)
        """,
        (TENANT_A.user_id, token_hash, age, idle_for, expires_in, revoked_ago, revoked_ago),
    )
    return token_hash


def remaining(hashes: list[bytes]) -> set[bytes]:
    rows = as_auth_role("SELECT token_sha256 FROM sessions WHERE token_sha256 = ANY(%s)", (hashes,))
    return {bytes(row[0]) for row in rows}


@pytest.fixture
def worker() -> Iterator[psycopg.Connection]:
    """The worker role. The purge is global, so each test starts from an empty table."""
    as_auth_role("DELETE FROM sessions")
    with psycopg.connect(worker_conninfo(), autocommit=True) as connection:
        yield connection
    as_auth_role("DELETE FROM sessions")


def purge(worker: psycopg.Connection, grace: int = HOUR, limit: int = 100) -> int:
    return worker.execute("SELECT auth.purge_sessions(%s, %s)", (grace, limit)).fetchone()[0]


def test_sessions_that_can_never_resolve_again_are_removed_and_live_ones_kept(worker):
    live = add_session()
    expired = add_session(expires_in=-2 * HOUR)
    revoked = add_session(revoked_ago=2 * HOUR)
    idle = add_session(idle_for=12 * HOUR + 2 * HOUR)
    everything = [live, expired, revoked, idle]

    removed = purge(worker)

    assert removed == 3
    assert remaining(everything) == {live}


def test_a_session_inside_the_grace_period_is_kept(worker):
    just_expired = add_session(expires_in=-60)
    just_revoked = add_session(revoked_ago=60)
    just_idle = add_session(idle_for=12 * HOUR + 60)

    assert purge(worker, grace=HOUR) == 0

    assert len(remaining([just_expired, just_revoked, just_idle])) == 3


def test_an_idle_session_within_the_twelve_hour_window_is_kept(worker):
    idle_but_valid = add_session(idle_for=11 * HOUR)

    assert purge(worker, grace=60) == 0

    assert remaining([idle_but_valid]) == {idle_but_valid}


def test_the_grace_period_has_a_one_minute_floor(worker):
    just_expired = add_session(expires_in=-30)

    assert purge(worker, grace=0) == 0

    assert remaining([just_expired]) == {just_expired}


def test_a_call_removes_at_most_the_limit(worker):
    old = [add_session(expires_in=-2 * HOUR, age=i) for i in range(3)]

    assert purge(worker, limit=2) == 2
    assert purge(worker, limit=2) == 1

    assert remaining(old) == set()


def test_the_limit_has_a_floor_and_a_ceiling(worker):
    old = [add_session(expires_in=-2 * HOUR) for _ in range(2)]

    assert purge(worker, limit=0) == 1
    assert purge(worker, limit=10**9) == 1

    assert remaining(old) == set()


def test_the_app_role_cannot_purge(app_conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
        app_conn.execute("SELECT auth.purge_sessions(3600, 10)")


def test_the_worker_cannot_read_or_delete_sessions_directly(worker_conn):
    for statement in ("SELECT * FROM sessions", "DELETE FROM sessions"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
            worker_conn.execute(statement)
        worker_conn.rollback()


def test_the_worker_can_call_no_other_auth_function(worker_conn):
    for call in (
        "SELECT * FROM auth.verify_login('a@b.example')",
        "SELECT auth.create_session(gen_random_uuid(), '\\x00'::bytea, 60)",
        "SELECT * FROM auth.resolve_session('\\x00'::bytea, 60)",
        "SELECT auth.revoke_session('\\x00'::bytea)",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
            worker_conn.execute(call)
        worker_conn.rollback()
