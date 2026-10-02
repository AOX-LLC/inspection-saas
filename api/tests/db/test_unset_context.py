"""With no tenant context, tenant tables raise and identity tables show nothing.

Raising matters: an unset context that returned an empty list would look like
"no data" and hide the bug that forgot to set it.
"""

import psycopg
import pytest
from psycopg import errors

from tests.db.conftest import CONTEXT_NOT_SET, TENANT_A

TENANT_TABLES = ["projects", "files", "audit_events"]
IDENTITY_TABLES = ["orgs", "users", "memberships"]


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_reading_a_tenant_table_without_context_raises(app_conn, table):
    with pytest.raises(errors.InsufficientPrivilege, match=CONTEXT_NOT_SET):
        app_conn.execute(f"SELECT * FROM {table}")


def test_writing_a_tenant_table_without_context_raises(app_conn):
    with pytest.raises(errors.InsufficientPrivilege, match=CONTEXT_NOT_SET):
        app_conn.execute(
            "INSERT INTO projects (org_id, name) VALUES (%s, 'Synthetic')", (TENANT_A.org_id,)
        )


@pytest.mark.parametrize("table", IDENTITY_TABLES)
def test_identity_tables_show_nothing_without_context(app_conn, table):
    count = app_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    assert count == 0


def test_creating_an_org_without_context_raises(app_conn):
    with pytest.raises(errors.InsufficientPrivilege, match=CONTEXT_NOT_SET):
        app_conn.execute("INSERT INTO orgs (name) VALUES ('Synthetic')")


def test_an_empty_setting_counts_as_unset(app_conn):
    # Once set in a session, a setting reverts to '' rather than NULL.
    app_conn.execute("SELECT set_config('app.org_id', '', true)")
    with pytest.raises(errors.InsufficientPrivilege, match=CONTEXT_NOT_SET):
        app_conn.execute("SELECT * FROM projects")


def test_a_malformed_setting_fails_closed(app_conn: psycopg.Connection):
    app_conn.execute("SELECT set_config('app.org_id', 'not-a-uuid', true)")
    with pytest.raises(errors.InvalidTextRepresentation):
        app_conn.execute("SELECT * FROM projects")
