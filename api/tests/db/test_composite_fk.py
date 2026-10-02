"""A file row cannot point at another org's project or another org's objects.

The FK is (org_id, project_id) -> projects(org_id, id). RLS's WITH CHECK
passes here, because org_id matches the context; only the FK stops it. The
object key must also be built from the row's own org, project and file ids.
"""

from uuid import UUID, uuid4

import pytest
from psycopg import errors

from tests.db.conftest import TENANT_A, TENANT_B, set_context

INSERT_FILE = """
    INSERT INTO files (id, org_id, project_id, object_key, content_type)
    VALUES (%s, %s, %s, %s, 'image/jpeg')
"""


def object_key(org_id: UUID, project_id: UUID, file_id: UUID) -> str:
    return f"orgs/{org_id}/projects/{project_id}/files/{file_id}/original"


@pytest.mark.parametrize("connection_fixture", ["app_conn", "owner_conn"])
def test_file_in_org_a_cannot_reference_a_project_in_org_b(request, connection_fixture):
    connection = request.getfixturevalue(connection_fixture)
    set_context(connection, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    file_id = uuid4()
    key = object_key(TENANT_A.org_id, TENANT_B.project_id, file_id)
    with pytest.raises(errors.ForeignKeyViolation, match="files_project_fkey"):
        connection.execute(INSERT_FILE, (file_id, TENANT_A.org_id, TENANT_B.project_id, key))


def test_file_in_org_a_can_reference_its_own_project(app_conn):
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    file_id = uuid4()
    key = object_key(TENANT_A.org_id, TENANT_A.project_id, file_id)
    inserted = app_conn.execute(INSERT_FILE, (file_id, TENANT_A.org_id, TENANT_A.project_id, key))
    assert inserted.rowcount == 1


@pytest.mark.parametrize(
    "key",
    [
        object_key(TENANT_B.org_id, TENANT_B.project_id, uuid4()),
        "orgs/../../elsewhere",
        "anything",
    ],
    ids=["another-orgs-key", "traversal", "free-form"],
)
def test_file_object_key_must_belong_to_its_own_row(app_conn, key):
    set_context(app_conn, org_id=TENANT_A.org_id, user_id=TENANT_A.user_id)
    with pytest.raises(errors.CheckViolation, match="files_object_key_in_org"):
        app_conn.execute(INSERT_FILE, (uuid4(), TENANT_A.org_id, TENANT_A.project_id, key))
