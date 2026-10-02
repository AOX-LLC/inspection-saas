"""The queue's functions, and what the worker role may do.

Handing a job to a worker is the one place the system works across tenants:
the next due job may belong to any org. It follows the pattern of the auth
functions. SECURITY DEFINER functions in schema `queue` are owned by
`inspection_dispatcher`: NOLOGIN, BYPASSRLS, owning only these functions and
holding only the table grants below. Each pins `search_path`, has EXECUTE
revoked from PUBLIC, and is granted to `inspection_worker` alone.

* `jobs_claim` returns four columns: job id, org id, kind, and a payload that
  the table's CHECK limits to ids. The worker then opens a tenant transaction
  for that org and does everything else under row-level security.
* `jobs_complete` and `jobs_fail` act only on a job the calling worker still
  holds (`locked_by`). The worker has no grant on `jobs` at all.
* A claim sets `locked_until`. A running job whose lock has lapsed belongs to a
  worker that died, and the next claim takes it again. Each claim counts as an
  attempt; a job out of attempts becomes `failed` instead of looping forever.
* `abandoned_uploads` finds, across orgs, uploads that were started and never
  finished, and finished uploads whose staging key has not been swept. It
  returns org and file ids only; the worker acts on them under that org's
  context.

The worker's own table grants are listed explicitly (it inherits none from the
default privileges the app role gets), and every one is subject to the same
forced row-level security as the API's.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER_ROLE = "inspection_worker"
DISPATCHER_ROLE = "inspection_dispatcher"

UPGRADE = f"""
-- Set when the worker has deleted a finished upload's staging object, which a
-- replayed presigned POST can recreate until it expires.
ALTER TABLE files ADD COLUMN staging_swept_at timestamptz;

-- The worker -------------------------------------------------------------------

GRANT USAGE ON SCHEMA app TO {WORKER_ROLE};
-- The only accessor the tables it reaches use. The identity accessors stay out.
GRANT EXECUTE ON FUNCTION app.org_id() TO {WORKER_ROLE};

GRANT SELECT, DELETE ON files TO {WORKER_ROLE};
GRANT UPDATE (staging_swept_at) ON files TO {WORKER_ROLE};
GRANT SELECT, UPDATE ON photos TO {WORKER_ROLE};
GRANT SELECT, INSERT, DELETE ON tiles TO {WORKER_ROLE};
GRANT INSERT ON audit_events TO {WORKER_ROLE};

-- The dispatcher ---------------------------------------------------------------

GRANT SELECT, UPDATE ON jobs TO {DISPATCHER_ROLE};
GRANT SELECT ON files TO {DISPATCHER_ROLE};

CREATE SCHEMA queue;
REVOKE ALL ON SCHEMA queue FROM PUBLIC;
GRANT USAGE ON SCHEMA queue TO {WORKER_ROLE};
GRANT USAGE, CREATE ON SCHEMA queue TO {DISPATCHER_ROLE};

-- Create the functions as the dispatcher so it owns them. The owner may SET
-- ROLE to it but does not inherit its privileges.
SET LOCAL ROLE {DISPATCHER_ROLE};

-- Takes the next due job, or returns nothing. Qualified names throughout: the
-- output columns are also variables in this function.
CREATE FUNCTION queue.jobs_claim(p_worker_id text, p_kinds text[], p_lock_seconds integer)
    RETURNS TABLE (job_id uuid, org_id uuid, kind text, payload jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    claimed public.jobs%ROWTYPE;
BEGIN
    IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'worker id must be 1 to 100 characters' USING ERRCODE = '22023';
    END IF;
    IF p_kinds IS NULL OR cardinality(p_kinds) = 0 THEN
        RAISE EXCEPTION 'at least one job kind is required' USING ERRCODE = '22023';
    END IF;

    -- A lapsed lock on a job with no attempts left is a job that kills its
    -- worker. It fails here rather than being handed out again.
    UPDATE public.jobs j
    SET status = 'failed', locked_by = NULL, locked_until = NULL, finished_at = now(),
        last_error = 'worker lost after the last attempt'
    WHERE j.status = 'running' AND j.locked_until < now() AND j.attempts >= j.max_attempts;

    SELECT * INTO claimed
    FROM public.jobs j
    WHERE j.kind = ANY (p_kinds)
      AND (
          (j.status = 'queued' AND j.run_after <= now())
          OR (j.status = 'running' AND j.locked_until < now())
      )
    ORDER BY j.run_after, j.created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE public.jobs j
    SET status = 'running',
        attempts = j.attempts + 1,
        locked_by = p_worker_id,
        locked_until = now() + make_interval(secs => least(greatest(p_lock_seconds, 1), 3600)),
        last_error = CASE WHEN claimed.status = 'running'
                          THEN 'worker lost; retried' ELSE j.last_error END
    WHERE j.id = claimed.id;

    RETURN QUERY SELECT claimed.id, claimed.org_id, claimed.kind, claimed.payload;
END
$$;

-- True if the caller still held the job.
CREATE FUNCTION queue.jobs_complete(p_job_id uuid, p_worker_id text) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    UPDATE public.jobs j
    SET status = 'succeeded', locked_by = NULL, locked_until = NULL, finished_at = now(),
        last_error = NULL
    WHERE j.id = p_job_id AND j.status = 'running' AND j.locked_by = p_worker_id;
    RETURN FOUND;
END
$$;

-- Releases a held job for retry with exponential backoff, or fails it when it
-- is out of attempts or the error is not retryable. Returns the new status, or
-- NULL if the caller no longer held the job. `p_error` is a short code, never
-- a message that could carry data.
CREATE FUNCTION queue.jobs_fail(
    p_job_id uuid, p_worker_id text, p_error text, p_retryable boolean,
    p_backoff_base_seconds integer
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    held public.jobs%ROWTYPE;
    is_final boolean;
BEGIN
    SELECT * INTO held FROM public.jobs j
    WHERE j.id = p_job_id AND j.status = 'running' AND j.locked_by = p_worker_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    is_final := NOT p_retryable OR held.attempts >= held.max_attempts;
    IF is_final THEN
        UPDATE public.jobs j
        SET status = 'failed', locked_by = NULL, locked_until = NULL, finished_at = now(),
            last_error = left(p_error, 200)
        WHERE j.id = held.id;
        RETURN 'failed';
    END IF;

    UPDATE public.jobs j
    SET status = 'queued', locked_by = NULL, locked_until = NULL, last_error = left(p_error, 200),
        run_after = now() + make_interval(
            secs => least(greatest(p_backoff_base_seconds, 1)::double precision
                          * power(2, held.attempts - 1), 3600)
        )
    WHERE j.id = held.id;
    RETURN 'queued';
END
$$;

-- Uploads for the worker to clean up, across orgs: started and never finished,
-- or finished with a staging key not yet swept. Ids only. Nothing younger than
-- 15 minutes qualifies, which is past the longest a presigned POST can live.
CREATE FUNCTION queue.abandoned_uploads(p_older_than_seconds integer, p_limit integer)
    RETURNS TABLE (org_id uuid, file_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT f.org_id, f.id
    FROM public.files f
    WHERE f.created_at < now() - make_interval(secs => greatest(p_older_than_seconds, 900))
      AND (f.status = 'pending' OR (f.status IN ('ready', 'failed') AND f.staging_swept_at IS NULL))
    ORDER BY f.created_at
    LIMIT least(greatest(p_limit, 1), 500)
$$;

REVOKE ALL ON FUNCTION
    queue.jobs_claim(text, text[], integer),
    queue.jobs_complete(uuid, text),
    queue.jobs_fail(uuid, text, text, boolean, integer),
    queue.abandoned_uploads(integer, integer)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    queue.jobs_claim(text, text[], integer),
    queue.jobs_complete(uuid, text),
    queue.jobs_fail(uuid, text, text, boolean, integer),
    queue.abandoned_uploads(integer, integer)
TO {WORKER_ROLE};

RESET ROLE;

-- The functions exist; nothing needs to create more objects as the dispatcher.
REVOKE CREATE ON SCHEMA queue FROM {DISPATCHER_ROLE};
"""

DOWNGRADE = f"""
DROP SCHEMA queue CASCADE;
REVOKE ALL ON jobs, files FROM {DISPATCHER_ROLE};
REVOKE ALL ON files, photos, tiles, audit_events FROM {WORKER_ROLE};
REVOKE EXECUTE ON FUNCTION app.org_id() FROM {WORKER_ROLE};
REVOKE USAGE ON SCHEMA app FROM {WORKER_ROLE};
ALTER TABLE files DROP COLUMN staging_swept_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
