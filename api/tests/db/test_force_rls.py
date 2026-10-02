"""FORCE ROW LEVEL SECURITY: the owner role is bound by the same policies."""

import pytest
from psycopg import errors

from tests.db.conftest import CONTEXT_NOT_SET, TENANT_A, TENANT_B, ids, set_context


def test_owner_without_context_raises(owner_conn):
    with pytest.raises(errors.InsufficientPrivilege, match=CONTEXT_NOT_SET):
        owner_conn.execute("SELECT id FROM projects")


def test_owner_sees_only_its_context_org(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    assert ids(owner_conn, "SELECT id FROM projects") == {TENANT_A.project_id}
    assert ids(owner_conn, "SELECT id FROM files") == {TENANT_A.file_id}


def test_owner_cannot_write_into_another_org(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    with pytest.raises(errors.InsufficientPrivilege, match="violates row-level security"):
        owner_conn.execute(
            "INSERT INTO projects (org_id, name) VALUES (%s, 'Synthetic')", (TENANT_B.org_id,)
        )


def test_owner_cannot_change_another_orgs_rows(owner_conn):
    set_context(owner_conn, org_id=TENANT_A.org_id)
    changed = owner_conn.execute(
        "UPDATE projects SET name = 'changed' WHERE id = %s", (TENANT_B.project_id,)
    )
    assert changed.rowcount == 0
