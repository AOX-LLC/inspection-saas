"""Authentication: password credentials and the functions that reach sessions.

Login and session lookup happen before any tenant context exists, and forced
row-level security binds even the table owner, so a function owned by
`inspection_owner` would see no rows. These functions are owned instead by
`inspection_auth`: a NOLOGIN role with BYPASSRLS that owns nothing else and
holds only the grants below. Each function pins `search_path`, has EXECUTE
revoked from PUBLIC, and is granted to `inspection_app` alone.

* `credentials` holds password hashes apart from `users`, so a policy or grant
  that lets the app read user rows can never expose a hash. Like `sessions` it
  has forced RLS, no policy and no app grant.
* The app role never sees a session row. It passes a token's SHA-256 to
  `auth.resolve_session` and gets back a session and user id, or nothing.
* Lifetimes are clamped inside the functions, so a bug (or a compromised app
  role) cannot mint a session longer than the maximums.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "inspection_app"
AUTH_ROLE = "inspection_auth"

UPGRADE = f"""
CREATE TABLE credentials (
    user_id        uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    password_hash  text NOT NULL CHECK (length(password_hash) BETWEEN 20 AND 500),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON credentials FROM {APP_ROLE};
ALTER TABLE credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE credentials FORCE ROW LEVEL SECURITY;

-- Only what the auth functions (and the seed, acting as the auth role) need.
GRANT SELECT ON users TO {AUTH_ROLE};
GRANT SELECT, INSERT, UPDATE ON credentials TO {AUTH_ROLE};
GRANT SELECT, INSERT, UPDATE ON sessions TO {AUTH_ROLE};

CREATE SCHEMA auth;
REVOKE ALL ON SCHEMA auth FROM PUBLIC;
GRANT USAGE ON SCHEMA auth TO {APP_ROLE};
GRANT USAGE, CREATE ON SCHEMA auth TO {AUTH_ROLE};

-- Create the functions as the auth role so it owns them. The owner may SET
-- ROLE to it but does not inherit its privileges.
SET LOCAL ROLE {AUTH_ROLE};

CREATE FUNCTION auth.verify_login(p_email text)
    RETURNS TABLE (user_id uuid, password_hash text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT u.id, c.password_hash
    FROM public.users u
    JOIN public.credentials c ON c.user_id = u.id
    WHERE u.email = lower(p_email)
$$;

CREATE FUNCTION auth.create_session(
    p_user_id uuid, p_token_sha256 bytea, p_absolute_seconds integer
) RETURNS uuid
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    INSERT INTO public.sessions (user_id, token_sha256, expires_at)
    VALUES (
        p_user_id,
        p_token_sha256,
        now() + make_interval(secs => least(greatest(p_absolute_seconds, 1), 604800))
    )
    RETURNING id
$$;

-- Returns the session only while it is unrevoked, inside its absolute limit
-- and inside its idle window. last_seen_at moves at most once a minute, so a
-- busy session is not a write on every request.
CREATE FUNCTION auth.resolve_session(p_token_sha256 bytea, p_idle_seconds integer)
    RETURNS TABLE (session_id uuid, user_id uuid)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    session_row public.sessions%ROWTYPE;
BEGIN
    SELECT * INTO session_row FROM public.sessions s
    WHERE s.token_sha256 = p_token_sha256
      AND s.revoked_at IS NULL
      AND s.expires_at > now()
      AND s.last_seen_at > now() - make_interval(secs => least(greatest(p_idle_seconds, 1), 43200));
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF session_row.last_seen_at < now() - interval '1 minute' THEN
        UPDATE public.sessions s SET last_seen_at = now() WHERE s.id = session_row.id;
    END IF;
    RETURN QUERY SELECT session_row.id, session_row.user_id;
END
$$;

CREATE FUNCTION auth.revoke_session(p_token_sha256 bytea) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, pg_temp
AS $$
    UPDATE public.sessions SET revoked_at = now()
    WHERE token_sha256 = p_token_sha256 AND revoked_at IS NULL
$$;

REVOKE ALL ON FUNCTION
    auth.verify_login(text),
    auth.create_session(uuid, bytea, integer),
    auth.resolve_session(bytea, integer),
    auth.revoke_session(bytea)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    auth.verify_login(text),
    auth.create_session(uuid, bytea, integer),
    auth.resolve_session(bytea, integer),
    auth.revoke_session(bytea)
TO {APP_ROLE};

RESET ROLE;
"""

DOWNGRADE = f"""
DROP SCHEMA auth CASCADE;
DROP TABLE credentials;
REVOKE ALL ON users, sessions FROM {AUTH_ROLE};
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
