"""The roles Phase 2 adds: the worker (a plain login role) and the dispatcher (definer owner)."""

import psycopg
import pytest

from tests.db.conftest import worker_conninfo

WORKER_ROLE = "inspection_worker"
DISPATCHER_ROLE = "inspection_dispatcher"


def test_the_worker_can_log_in_and_is_the_current_user(worker_conn):
    assert worker_conn.execute("SELECT current_user").fetchone() == (WORKER_ROLE,)


def test_the_worker_inherits_no_other_role(worker_conn):
    count = worker_conn.execute(
        "SELECT count(*) FROM pg_auth_members WHERE member = %s::regrole", (WORKER_ROLE,)
    ).fetchone()[0]
    assert count == 0


def test_the_worker_owns_nothing(worker_conn):
    owned = worker_conn.execute(
        "SELECT count(*) FROM pg_shdepend WHERE refobjid = %s::regrole AND deptype = 'o'",
        (WORKER_ROLE,),
    ).fetchone()[0]
    databases = worker_conn.execute(
        "SELECT count(*) FROM pg_database WHERE datdba = %s::regrole", (WORKER_ROLE,)
    ).fetchone()[0]
    assert (owned, databases) == (0, 0)


def test_the_worker_cannot_create_objects(worker_conn):
    row = worker_conn.execute(
        """
        SELECT
            has_schema_privilege(%(role)s, 'public', 'CREATE'),
            has_schema_privilege(%(role)s, 'app', 'CREATE'),
            has_schema_privilege(%(role)s, 'auth', 'CREATE'),
            has_database_privilege(%(role)s, current_database(), 'CREATE'),
            has_database_privilege(%(role)s, current_database(), 'TEMPORARY')
        """,
        {"role": WORKER_ROLE},
    ).fetchone()
    assert row == (False, False, False, False, False)


def test_the_worker_cannot_reach_migration_history(worker_conn):
    assert (
        worker_conn.execute(
            "SELECT has_schema_privilege(%s, 'migrations', 'USAGE')", (WORKER_ROLE,)
        ).fetchone()[0]
        is False
    )


def test_the_dispatcher_bypasses_rls_but_cannot_log_in_or_do_anything_else(app_conn):
    row = app_conn.execute(
        """
        SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin
        FROM pg_roles WHERE rolname = %s
        """,
        (DISPATCHER_ROLE,),
    ).fetchone()
    assert row == (False, True, False, False, False, False)


def test_only_the_owner_is_a_member_of_the_dispatcher(app_conn):
    members = {
        row[0]
        for row in app_conn.execute(
            "SELECT member::regrole::text FROM pg_auth_members WHERE roleid = %s::regrole",
            (DISPATCHER_ROLE,),
        ).fetchall()
    }
    assert members == {"inspection_owner"}


def test_nobody_can_log_in_as_the_dispatcher():
    conninfo = worker_conninfo().replace(f"user={WORKER_ROLE}", f"user={DISPATCHER_ROLE}")
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(conninfo, connect_timeout=5)
