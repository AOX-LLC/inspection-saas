"""Indexes behind keyset pagination of the file and photo lists.

Both lists page by `(created_at, id)` within one project, so the matching
index is `(org_id, project_id, created_at, id)`. A btree is read in either
direction, so it serves oldest-first and newest-first lists alike.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = """
CREATE INDEX files_project_created_idx ON files (org_id, project_id, created_at, id);
CREATE INDEX photos_project_created_idx ON photos (org_id, project_id, created_at, id);
"""

DOWNGRADE = """
DROP INDEX photos_project_created_idx, files_project_created_idx;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
