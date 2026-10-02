"""Tenancy: identity tables, projects, files, audit events, and row-level security.

The isolation rules, in brief:

* Tenant-owned tables (projects, files, audit_events) carry `org_id NOT NULL`.
  Their policies compare it with `app.org_id()`, which raises when no org
  context is set, so a missing context fails loudly instead of looking empty.
* Identity tables (orgs, users, memberships) need OR-composed policies, and
  Postgres does not promise to short-circuit OR. They use the null-safe
  accessors, so an unset context matches nothing.
* Writes must match the context too: you can only create an org whose id is
  the context org, and only insert yourself as a user. Who may do either is
  the API's decision; RLS keeps writes inside the context.
* Every table has ENABLE and FORCE row-level security, so the owner role is
  bound by the same policies as the app.
* Children reference parents by (org_id, id), so a row cannot point at another
  org's parent even if a policy were wrong.
* `sessions` has no policy and no app grant. Phase 1b reaches it through
  narrow SECURITY DEFINER functions.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "inspection_app"

UPGRADE = f"""
-- Context accessors -------------------------------------------------------

CREATE SCHEMA app;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO {APP_ROLE};

-- A setting that was set earlier in the session reverts to '' rather than
-- NULL, so both mean "unset".
CREATE FUNCTION app.org_id() RETURNS uuid
    LANGUAGE plpgsql STABLE PARALLEL SAFE
    SET search_path = pg_catalog
AS $$
DECLARE
    setting text := current_setting('app.org_id', true);
BEGIN
    IF setting IS NULL OR setting = '' THEN
        RAISE EXCEPTION 'tenant context app.org_id is not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN setting::uuid;
END
$$;

CREATE FUNCTION app.user_id() RETURNS uuid
    LANGUAGE plpgsql STABLE PARALLEL SAFE
    SET search_path = pg_catalog
AS $$
DECLARE
    setting text := current_setting('app.user_id', true);
BEGIN
    IF setting IS NULL OR setting = '' THEN
        RAISE EXCEPTION 'tenant context app.user_id is not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN setting::uuid;
END
$$;

CREATE FUNCTION app.org_id_or_null() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    SET search_path = pg_catalog
AS $$ SELECT nullif(current_setting('app.org_id', true), '')::uuid $$;

CREATE FUNCTION app.user_id_or_null() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    SET search_path = pg_catalog
AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO {APP_ROLE};

-- The app gets DML, never TRUNCATE, REFERENCES or TRIGGER, on tables the
-- owner creates in public. A new table without RLS fails the catalog tests.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};

-- Identity ---------------------------------------------------------------

CREATE TABLE orgs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 320),
    display_name  text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_email_key UNIQUE (email)
);

CREATE TABLE memberships (
    org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role        text NOT NULL CHECK (role IN ('owner', 'admin', 'inspector', 'viewer')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);
CREATE INDEX memberships_user_id_idx ON memberships (user_id);

CREATE TABLE sessions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_sha256  bytea NOT NULL CHECK (length(token_sha256) = 32),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    CONSTRAINT sessions_token_sha256_key UNIQUE (token_sha256)
);
CREATE INDEX sessions_user_id_idx ON sessions (user_id);

-- Tenant-owned ------------------------------------------------------------

CREATE TABLE projects (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT projects_org_id_id_key UNIQUE (org_id, id)
);

CREATE TABLE files (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             uuid NOT NULL,
    project_id         uuid NOT NULL,
    object_key         text NOT NULL,
    content_type       text NOT NULL CHECK (length(content_type) BETWEEN 1 AND 255),
    size_bytes         bigint CHECK (size_bytes >= 0),
    status             text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'ready', 'failed')),
    original_filename  text CHECK (length(original_filename) <= 255),
    uploaded_by        uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT files_object_key_key UNIQUE (object_key),
    -- The storage key is a pointer into another system. Binding it to the
    -- row's own ids stops a row from pointing at, or squatting, another
    -- org's objects.
    CONSTRAINT files_object_key_in_org CHECK (
        object_key LIKE 'orgs/' || org_id::text || '/projects/' || project_id::text
            || '/files/' || id::text || '/%'
    ),
    CONSTRAINT files_project_fkey FOREIGN KEY (org_id, project_id)
        REFERENCES projects (org_id, id) ON DELETE CASCADE
);
CREATE INDEX files_org_id_project_id_idx ON files (org_id, project_id);
CREATE INDEX files_uploaded_by_idx ON files (uploaded_by);

-- Append-only: the app may insert and read, never change or remove.
CREATE TABLE audit_events (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
    actor_user_id  uuid REFERENCES users (id) ON DELETE SET NULL,
    action         text NOT NULL CHECK (length(action) BETWEEN 1 AND 100),
    target_type    text CHECK (length(target_type) <= 100),
    target_id      uuid,
    detail         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    occurred_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_org_id_occurred_at_idx ON audit_events (org_id, occurred_at DESC);
CREATE INDEX audit_events_actor_user_id_idx ON audit_events (actor_user_id);

REVOKE ALL ON sessions FROM {APP_ROLE};
REVOKE UPDATE, DELETE ON audit_events FROM {APP_ROLE};

-- Row-level security -------------------------------------------------------
-- Accessors are wrapped in a scalar subquery so they run once per statement,
-- not once per row.

ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;
ALTER TABLE orgs FORCE ROW LEVEL SECURITY;
CREATE POLICY orgs_select ON orgs FOR SELECT USING (
    id = (SELECT app.org_id_or_null())
    OR EXISTS (
        SELECT 1 FROM memberships m
        WHERE m.org_id = orgs.id AND m.user_id = (SELECT app.user_id_or_null())
    )
);
CREATE POLICY orgs_insert ON orgs FOR INSERT WITH CHECK (id = (SELECT app.org_id()));
CREATE POLICY orgs_update ON orgs FOR UPDATE
    USING (id = (SELECT app.org_id())) WITH CHECK (id = (SELECT app.org_id()));

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_select ON users FOR SELECT USING (
    id = (SELECT app.user_id_or_null())
    OR EXISTS (
        SELECT 1 FROM memberships m
        WHERE m.user_id = users.id AND m.org_id = (SELECT app.org_id_or_null())
    )
);
CREATE POLICY users_insert ON users FOR INSERT WITH CHECK (id = (SELECT app.user_id()));
CREATE POLICY users_update ON users FOR UPDATE
    USING (id = (SELECT app.user_id())) WITH CHECK (id = (SELECT app.user_id()));

-- memberships must not refer back to orgs or users, or the policies recurse.
ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY memberships_select ON memberships FOR SELECT USING (
    org_id = (SELECT app.org_id_or_null())
    OR user_id = (SELECT app.user_id_or_null())
);
CREATE POLICY memberships_insert ON memberships FOR INSERT
    WITH CHECK (org_id = (SELECT app.org_id()));
CREATE POLICY memberships_update ON memberships FOR UPDATE
    USING (org_id = (SELECT app.org_id())) WITH CHECK (org_id = (SELECT app.org_id()));
CREATE POLICY memberships_delete ON memberships FOR DELETE
    USING (org_id = (SELECT app.org_id()));

-- No policy: nobody bound by RLS reads or writes sessions directly.
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;
CREATE POLICY projects_tenant ON projects
    USING (org_id = (SELECT app.org_id())) WITH CHECK (org_id = (SELECT app.org_id()));

ALTER TABLE files ENABLE ROW LEVEL SECURITY;
ALTER TABLE files FORCE ROW LEVEL SECURITY;
CREATE POLICY files_tenant ON files
    USING (org_id = (SELECT app.org_id())) WITH CHECK (org_id = (SELECT app.org_id()));

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_events_select ON audit_events FOR SELECT
    USING (org_id = (SELECT app.org_id()));
CREATE POLICY audit_events_insert ON audit_events FOR INSERT
    WITH CHECK (org_id = (SELECT app.org_id()));
"""

DOWNGRADE = f"""
DROP TABLE audit_events, files, projects, sessions, memberships, users, orgs;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE};
DROP SCHEMA app CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
