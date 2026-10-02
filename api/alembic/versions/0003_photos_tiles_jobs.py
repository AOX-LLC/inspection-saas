"""Photos, tiles and the job queue's table.

All three are tenant-owned: `org_id NOT NULL`, forced row-level security, and a
policy comparing `org_id` with `app.org_id()`. Children reference parents by
`(org_id, id)`.

* `photos` is one row per uploaded image. The API creates it when an upload
  completes; the worker moves it through `queued`, `processing`, `tiled` or
  `failed`.
* `tiles` records where each tile sits in the oriented original (`x`, `y`,
  `src_width`, `src_height`) and the `scale` it was resized by, so a box
  detected in a tile maps back: `original = origin + tile_px / scale`.
* `jobs` is the queue. A job's `payload` holds ids only (a CHECK enforces it),
  because the claim function that hands jobs across tenants returns it. The
  app may insert jobs and nothing else; workers never touch the table
  directly, only through the SECURITY DEFINER functions of the next migration.

Inserting a job wakes listening workers with `NOTIFY`, delivered when the
enqueuing transaction commits, so a worker never wakes for a job it cannot see.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "inspection_app"

_UUID_REGEX = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# A jsonpath that finds any top-level value that is not a canonical UUID string.
PAYLOAD_VALUE_IS_NOT_AN_ID = (
    f'strict $.* ? (!(@.type() == "string" && @ like_regex "{_UUID_REGEX}"))'
)

UPGRADE = f"""
-- The composite FK from photos needs a (org_id, id) key on files, as
-- projects already has.
ALTER TABLE files ADD CONSTRAINT files_org_id_id_key UNIQUE (org_id, id);

CREATE TABLE photos (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL,
    project_id  uuid NOT NULL,
    file_id     uuid NOT NULL,
    status      text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'tiled', 'failed')),
    -- Size after EXIF orientation is applied, known once the photo is decoded.
    width       integer CHECK (width > 0),
    height      integer CHECK (height > 0),
    error       text CHECK (length(error) <= 200),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT photos_org_id_id_key UNIQUE (org_id, id),
    CONSTRAINT photos_file_id_key UNIQUE (file_id),
    CONSTRAINT photos_file_fkey FOREIGN KEY (org_id, file_id)
        REFERENCES files (org_id, id) ON DELETE CASCADE,
    CONSTRAINT photos_project_fkey FOREIGN KEY (org_id, project_id)
        REFERENCES projects (org_id, id) ON DELETE CASCADE
);
-- Serves the per-project count by status.
CREATE INDEX photos_org_id_project_id_status_idx ON photos (org_id, project_id, status);

CREATE TABLE tiles (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL,
    photo_id    uuid NOT NULL,
    -- 0 is full resolution; 1 is a single downscaled overview of the whole photo.
    level       smallint NOT NULL CHECK (level IN (0, 1)),
    -- Top-left corner and size of the region this tile covers, in pixels of the
    -- oriented original.
    x           integer NOT NULL CHECK (x >= 0),
    y           integer NOT NULL CHECK (y >= 0),
    src_width   integer NOT NULL CHECK (src_width > 0),
    src_height  integer NOT NULL CHECK (src_height > 0),
    -- Tile pixels per original pixel; the stored tile is src_* * scale, rounded.
    scale       double precision NOT NULL CHECK (scale > 0 AND scale <= 1),
    width       integer NOT NULL CHECK (width > 0),
    height      integer NOT NULL CHECK (height > 0),
    object_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tiles_photo_position_key UNIQUE (photo_id, level, x, y),
    CONSTRAINT tiles_object_key_key UNIQUE (object_key),
    CONSTRAINT tiles_photo_fkey FOREIGN KEY (org_id, photo_id)
        REFERENCES photos (org_id, id) ON DELETE CASCADE,
    CONSTRAINT tiles_object_key_in_org CHECK (object_key LIKE 'orgs/' || org_id::text || '/%')
);
CREATE INDEX tiles_org_id_photo_id_idx ON tiles (org_id, photo_id);

CREATE TABLE jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('tile_photo')),
    -- Ids only: the claim function returns this across tenants. Every value
    -- must be a canonical lower-case UUID string. The path is strict: in lax
    -- mode `$.*` would unwrap an array and judge its elements instead.
    payload       jsonb NOT NULL DEFAULT '{{}}'::jsonb CHECK (
        jsonb_typeof(payload) = 'object'
        AND NOT jsonb_path_exists(
            payload,
            '{PAYLOAD_VALUE_IS_NOT_AN_ID}'
        )
    ),
    status        text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    run_after     timestamptz NOT NULL DEFAULT now(),
    attempts      integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts  integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
    -- A running job whose lock has lapsed belongs to a worker that died.
    locked_until  timestamptz,
    locked_by     text CHECK (length(locked_by) <= 100),
    last_error    text CHECK (length(last_error) <= 200),
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    CONSTRAINT jobs_running_has_lock CHECK (
        (status = 'running') = (locked_until IS NOT NULL AND locked_by IS NOT NULL)
    )
);
CREATE INDEX jobs_org_id_idx ON jobs (org_id);
-- The claim query: due queued jobs, and running jobs whose lock lapsed.
CREATE INDEX jobs_claim_queued_idx ON jobs (run_after) WHERE status = 'queued';
CREATE INDEX jobs_claim_running_idx ON jobs (locked_until) WHERE status = 'running';

-- A job is only ever created in the same transaction as the row that needs it.
-- NOTIFY is delivered at commit and carries nothing: a worker that wakes asks
-- the claim function what is due.
CREATE FUNCTION public.notify_job_queued() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM pg_notify('inspection_jobs', '');
    RETURN NULL;
END
$$;
CREATE TRIGGER jobs_notify AFTER INSERT ON jobs
    FOR EACH STATEMENT EXECUTE FUNCTION public.notify_job_queued();

-- What the API may do. New tables arrive with DML from default privileges; this
-- narrows them to the operations a request has a reason to perform.
REVOKE UPDATE, DELETE ON photos FROM {APP_ROLE};
REVOKE INSERT, UPDATE, DELETE ON tiles FROM {APP_ROLE};
REVOKE SELECT, UPDATE, DELETE ON jobs FROM {APP_ROLE};

ALTER TABLE photos ENABLE ROW LEVEL SECURITY;
ALTER TABLE photos FORCE ROW LEVEL SECURITY;
CREATE POLICY photos_tenant ON photos
    USING (org_id = (SELECT app.org_id())) WITH CHECK (org_id = (SELECT app.org_id()));

ALTER TABLE tiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE tiles FORCE ROW LEVEL SECURITY;
CREATE POLICY tiles_tenant ON tiles
    USING (org_id = (SELECT app.org_id())) WITH CHECK (org_id = (SELECT app.org_id()));

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY jobs_tenant ON jobs
    USING (org_id = (SELECT app.org_id())) WITH CHECK (org_id = (SELECT app.org_id()));
"""

DOWNGRADE = """
DROP TABLE jobs, tiles, photos;
DROP FUNCTION public.notify_job_queued();
ALTER TABLE files DROP CONSTRAINT files_org_id_id_key;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
