"""Tables the app role may not touch, or may only append to."""

import pytest
from psycopg import errors

from tests.db.conftest import TENANT_A, set_context

SESSION_STATEMENTS = [
    "SELECT * FROM sessions",
    "INSERT INTO sessions (user_id, token_sha256, expires_at) "
    "VALUES (gen_random_uuid(), sha256('x'), now())",
    "UPDATE sessions SET revoked_at = now()",
    "DELETE FROM sessions",
]


@pytest.mark.parametrize("statement", SESSION_STATEMENTS)
def test_app_cannot_touch_sessions_even_with_context(app_conn, statement):
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied for table sessions"):
        app_conn.execute(statement)


def test_owner_reads_no_sessions_directly(owner_conn):
    # No policy exists, so FORCE RLS hides every row from the owner too.
    # Phase 1b reaches sessions only through SECURITY DEFINER functions.
    set_context(owner_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    with pytest.raises(errors.InsufficientPrivilege, match="violates row-level security"):
        owner_conn.execute(
            "INSERT INTO sessions (user_id, token_sha256, expires_at) "
            "VALUES (%s, sha256('x'), now())",
            (TENANT_A.user_id,),
        )


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_events SET action = 'tampered'", "DELETE FROM audit_events"],
)
def test_audit_events_are_append_only_for_the_app(app_conn, statement):
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied for table"):
        app_conn.execute(statement)


def test_app_can_append_an_audit_event(app_conn):
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    inserted = app_conn.execute(
        "INSERT INTO audit_events (org_id, actor_user_id, action) VALUES (%s, %s, 'test.ok')",
        (TENANT_A.org_id, TENANT_A.user_id),
    )
    assert inserted.rowcount == 1
