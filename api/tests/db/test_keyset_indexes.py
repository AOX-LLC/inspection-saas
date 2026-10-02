"""The keyset lists are served by their index, not a scan and sort."""

from uuid import uuid4

import psycopg
import pytest

from tests.db.conftest import owner_conninfo, set_context

QUERIES = {
    "files_project_created_idx": (
        "SELECT id FROM files WHERE org_id = %(org)s AND project_id = %(project)s"
        " AND (created_at, id) > (now(), %(id)s) ORDER BY created_at, id LIMIT 51"
    ),
    "photos_project_created_idx": (
        "SELECT id FROM photos WHERE org_id = %(org)s AND project_id = %(project)s"
        " AND (created_at, id) < (now(), %(id)s) ORDER BY created_at DESC, id DESC LIMIT 51"
    ),
}


@pytest.mark.parametrize("index", QUERIES)
def test_the_page_query_uses_its_index_without_a_sort(index: str):
    params = {"org": uuid4(), "project": uuid4(), "id": uuid4()}
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, org_id=params["org"])
        # Tables here are tiny, so the planner is only asked which plan is possible.
        connection.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(row[0] for row in connection.execute("EXPLAIN " + QUERIES[index], params))

    assert index in plan
    assert "Sort" not in plan
