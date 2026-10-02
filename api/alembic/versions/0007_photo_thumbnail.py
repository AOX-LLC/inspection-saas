"""Where a photo's thumbnail is.

The worker cuts one small preview of each photo while it tiles it, and records
its key here once the object is written. The grid reads the key, never the
original. The key is bound to the photo's own org and id, as a tile's is.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER_ROLE = "inspection_worker"

UPGRADE = f"""
ALTER TABLE photos ADD COLUMN thumb_key text;
ALTER TABLE photos ADD CONSTRAINT photos_thumb_key_in_photo CHECK (
    thumb_key IS NULL
    OR thumb_key = 'orgs/' || org_id::text || '/projects/' || project_id::text
                   || '/photos/' || id::text || '/thumb.jpg'
);
GRANT UPDATE (thumb_key) ON photos TO {WORKER_ROLE};
"""

DOWNGRADE = f"""
REVOKE UPDATE (thumb_key) ON photos FROM {WORKER_ROLE};
ALTER TABLE photos DROP CONSTRAINT photos_thumb_key_in_photo;
ALTER TABLE photos DROP COLUMN thumb_key;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
