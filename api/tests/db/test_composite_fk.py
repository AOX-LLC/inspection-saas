"""A child row cannot point at another org's parent, even if a policy allowed it.

The FK is (org_id, project_id) -> projects(org_id, id). RLS's WITH CHECK
passes here, because org_id matches the context; only the FK stops it.
"""

import pytest
from psycopg import errors

from tests.db.conftest import TENANT_A, TENANT_B, set_context

INSERT_FILE = """
    INSERT INTO files (org_id, project_id, object_key, content_type)
    VALUES (%s, %s, %s, 'image/jpeg')
"""


@pytest.mark.parametrize("connection_fixture", ["app_conn", "owner_conn"])
def test_file_in_org_a_cannot_reference_a_project_in_org_b(request, connection_fixture):
    connection = request.getfixturevalue(connection_fixture)
    set_context(connection, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    with pytest.raises(errors.ForeignKeyViolation, match="files_project_fkey"):
        connection.execute(INSERT_FILE, (TENANT_A.org_id, TENANT_B.project_id, "probe/cross-org"))


def test_file_in_org_a_can_reference_its_own_project(app_conn):
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    inserted = app_conn.execute(
        INSERT_FILE, (TENANT_A.org_id, TENANT_A.project_id, "probe/same-org")
    )
    assert inserted.rowcount == 1
