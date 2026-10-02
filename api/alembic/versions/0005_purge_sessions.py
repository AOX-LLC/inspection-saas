"""A narrow auth function for deleting dead sessions.

Sessions are reachable only through the auth role, so the worker that cleans
them up gets one function, not a grant on the table. `auth.purge_sessions`
deletes sessions that can never resolve again: revoked, past their absolute
limit, or idle for longer than the 12-hour maximum, each by at least the grace
period. It is owned by `inspection_auth` like the other auth functions, pins
`search_path`, has EXECUTE revoked from PUBLIC, and is granted to
`inspection_worker` alone. It deletes at most `p_limit` rows per call and
returns how many.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTH_ROLE = "inspection_auth"
WORKER_ROLE = "inspection_worker"

UPGRADE = f"""
GRANT DELETE ON sessions TO {AUTH_ROLE};
GRANT USAGE ON SCHEMA auth TO {WORKER_ROLE};
GRANT CREATE ON SCHEMA auth TO {AUTH_ROLE};

SET LOCAL ROLE {AUTH_ROLE};

-- 43200 seconds is the idle maximum that auth.resolve_session clamps to, so a
-- session idle for longer than that plus the grace cannot resolve under any
-- configuration.
CREATE FUNCTION auth.purge_sessions(p_grace_seconds integer, p_limit integer) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    grace interval := make_interval(secs => greatest(p_grace_seconds, 60));
    removed integer;
BEGIN
    WITH doomed AS (
        SELECT s.id
        FROM public.sessions s
        WHERE s.revoked_at < now() - grace
           OR s.expires_at < now() - grace
           OR s.last_seen_at < now() - (interval '43200 seconds' + grace)
        ORDER BY s.created_at
        LIMIT least(greatest(p_limit, 1), 1000)
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM public.sessions s USING doomed WHERE s.id = doomed.id;
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END
$$;

REVOKE ALL ON FUNCTION auth.purge_sessions(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth.purge_sessions(integer, integer) TO {WORKER_ROLE};

RESET ROLE;

REVOKE CREATE ON SCHEMA auth FROM {AUTH_ROLE};
"""

DOWNGRADE = f"""
DROP FUNCTION auth.purge_sessions(integer, integer);
REVOKE USAGE ON SCHEMA auth FROM {WORKER_ROLE};
REVOKE DELETE ON sessions FROM {AUTH_ROLE};
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
