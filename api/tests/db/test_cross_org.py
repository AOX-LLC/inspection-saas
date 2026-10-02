"""With org A's context, org B's rows cannot be seen, changed, or written to."""

import psycopg
import pytest
from psycopg import errors

from tests.db.conftest import (
    CONTEXT_NOT_SET,
    SHARED_USER_ID,
    TENANT_A,
    TENANT_B,
    ids,
    set_context,
)

RLS_VIOLATION = "violates row-level security policy"


@pytest.fixture
def as_a(app_conn: psycopg.Connection) -> psycopg.Connection:
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    return app_conn


def test_only_a_rows_are_visible(as_a):
    assert ids(as_a, "SELECT id FROM projects") == {TENANT_A.project_id}
    assert ids(as_a, "SELECT id FROM files") == {TENANT_A.file_id}
    assert ids(as_a, "SELECT id FROM audit_events") == {TENANT_A.audit_event_id}
    assert ids(as_a, "SELECT id FROM orgs") == {TENANT_A.org_id}
    assert ids(as_a, "SELECT org_id FROM memberships") == {TENANT_A.org_id}
    assert ids(as_a, "SELECT id FROM users") == {TENANT_A.user_id, SHARED_USER_ID}


def test_looking_up_b_by_id_finds_nothing(as_a):
    assert ids(as_a, "SELECT id FROM projects WHERE id = %s", (TENANT_B.project_id,)) == set()
    assert ids(as_a, "SELECT id FROM orgs WHERE id = %s", (TENANT_B.org_id,)) == set()
    assert ids(as_a, "SELECT id FROM users WHERE id = %s", (TENANT_B.user_id,)) == set()


@pytest.mark.parametrize(
    ("statement", "target"),
    [
        ("UPDATE projects SET name = 'changed' WHERE id = %s", TENANT_B.project_id),
        ("DELETE FROM projects WHERE id = %s", TENANT_B.project_id),
        ("UPDATE files SET status = 'failed' WHERE id = %s", TENANT_B.file_id),
        ("DELETE FROM files WHERE id = %s", TENANT_B.file_id),
        ("UPDATE orgs SET name = 'changed' WHERE id = %s", TENANT_B.org_id),
        ("DELETE FROM memberships WHERE org_id = %s", TENANT_B.org_id),
        ("UPDATE users SET display_name = 'changed' WHERE id = %s", TENANT_B.user_id),
    ],
)
def test_changing_b_rows_affects_nothing(as_a, statement, target):
    assert as_a.execute(statement, (target,)).rowcount == 0


def test_an_unfiltered_delete_reaches_only_a(as_a):
    assert as_a.execute("DELETE FROM files").rowcount == 1


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        ("INSERT INTO projects (org_id, name) VALUES (%s, 'Synthetic')", (TENANT_B.org_id,)),
        (
            "INSERT INTO audit_events (org_id, action) VALUES (%s, 'test.forged')",
            (TENANT_B.org_id,),
        ),
        (
            "INSERT INTO memberships (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (TENANT_B.org_id, TENANT_A.user_id),
        ),
        ("INSERT INTO orgs (id, name) VALUES (%s, 'Synthetic')", (TENANT_B.org_id,)),
        (
            "INSERT INTO users (id, email, display_name) VALUES (%s, 'x@test.example', 'X')",
            (TENANT_B.user_id,),
        ),
    ],
)
def test_writing_rows_for_b_is_rejected(as_a, statement, params):
    with pytest.raises(errors.InsufficientPrivilege, match=RLS_VIOLATION):
        as_a.execute(statement, params)


@pytest.mark.parametrize(
    ("statement", "target"),
    [
        ("UPDATE projects SET org_id = %s WHERE id = %s", TENANT_A.project_id),
        ("UPDATE memberships SET org_id = %s WHERE user_id = %s", TENANT_A.user_id),
    ],
)
def test_moving_a_row_from_a_to_b_is_rejected(as_a, statement, target):
    with pytest.raises(errors.InsufficientPrivilege, match=RLS_VIOLATION):
        as_a.execute(statement, (TENANT_B.org_id, target))


def test_b_rows_are_unchanged_after_attempts(as_a):
    as_a.execute("UPDATE projects SET name = 'changed' WHERE id = %s", (TENANT_B.project_id,))
    as_a.execute("DELETE FROM files WHERE id = %s", (TENANT_B.file_id,))

    # Same transaction, so a regression would show here without being committed.
    set_context(as_a, org_id=TENANT_B.org_id, user_id=TENANT_B.user_id)
    project_name = as_a.execute(
        "SELECT name FROM projects WHERE id = %s", (TENANT_B.project_id,)
    ).fetchone()
    assert project_name == (TENANT_B.project_name,)
    assert ids(as_a, "SELECT id FROM files") == {TENANT_B.file_id}


def test_an_upsert_cannot_take_over_a_b_row(as_a):
    with pytest.raises(errors.InsufficientPrivilege, match=RLS_VIOLATION):
        as_a.execute(
            """
            INSERT INTO projects (id, org_id, name) VALUES (%s, %s, 'hijacked')
            ON CONFLICT (id) DO UPDATE SET name = excluded.name
            """,
            (TENANT_B.project_id, TENANT_A.org_id),
        )


def test_a_member_of_both_orgs_sees_only_the_context_org(app_conn):
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=SHARED_USER_ID)
    assert ids(app_conn, "SELECT id FROM projects") == {TENANT_A.project_id}
    # Members of the context org, never B-only members.
    assert ids(app_conn, "SELECT id FROM users") == {TENANT_A.user_id, SHARED_USER_ID}
    # The context org's memberships plus the user's own, never B's other rows.
    memberships = set(app_conn.execute("SELECT org_id, user_id FROM memberships").fetchall())
    assert memberships == {
        (TENANT_A.org_id, TENANT_A.user_id),
        (TENANT_A.org_id, SHARED_USER_ID),
        (TENANT_B.org_id, SHARED_USER_ID),
    }

    app_conn.rollback()
    set_context(app_conn, org_id=TENANT_B.org_id, user_id=SHARED_USER_ID)
    assert ids(app_conn, "SELECT id FROM projects") == {TENANT_B.project_id}


def test_user_context_lists_own_orgs_but_no_tenant_rows(app_conn):
    set_context(app_conn, user_id=SHARED_USER_ID)
    assert ids(app_conn, "SELECT id FROM orgs") == {TENANT_A.org_id, TENANT_B.org_id}
    assert ids(app_conn, "SELECT id FROM users") == {SHARED_USER_ID}
    memberships = set(app_conn.execute("SELECT org_id, user_id FROM memberships").fetchall())
    assert memberships == {(TENANT_A.org_id, SHARED_USER_ID), (TENANT_B.org_id, SHARED_USER_ID)}
    with pytest.raises(errors.InsufficientPrivilege, match=CONTEXT_NOT_SET):
        app_conn.execute("SELECT id FROM projects")
