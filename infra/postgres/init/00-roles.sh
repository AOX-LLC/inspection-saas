#!/bin/bash
# Creates the database roles and databases. Runs once, as the superuser, when
# the data volume is first initialised. Nothing else uses the superuser.
#
#   inspection_owner  owns the schema and every table. Used by migrate and seed.
#                     Tenant tables FORCE row-level security, so it is bound too.
#   inspection_app    what the API connects as. Owns nothing, cannot bypass RLS,
#                     and gets DML grants only (set up by the first migration).
#   inspection_worker what the background worker connects as. Same limits as the app
#                     role: owns nothing, cannot bypass RLS, and works under a tenant
#                     context like the API. It may execute the queue functions below.
#   inspection_dispatcher
#                     NOLOGIN, BYPASSRLS. Owns only the SECURITY DEFINER functions in
#                     schema `queue`, which must see every org's jobs to hand one to a
#                     worker. Same arrangement as inspection_auth: the owner may SET
#                     ROLE to it (not inherit it) so migrations can create the functions.
#   inspection_auth   NOLOGIN, BYPASSRLS. Owns only the SECURITY DEFINER functions
#                     in schema `auth`, which must read sessions and credentials
#                     before any tenant context exists. Forced RLS binds the owner,
#                     so these functions cannot run as inspection_owner. Nobody
#                     can log in as this role; inspection_owner may SET ROLE to it
#                     (not inherit it) so migrations can create the functions as it.
#
# `inspection` holds the seeded demo data; `inspection_test` is the test suite's,
# so tests never touch seeded rows.
set -euo pipefail

SECRETS_DIR=/run/inspection-secrets/db

psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname postgres <<SQL
-- CREATE ROLE carries the passwords; if it fails, keep the statement out of the log.
SET log_min_error_statement = panic;
\set owner_password \`cat $SECRETS_DIR/owner_password\`
\set app_password \`cat $SECRETS_DIR/app_password\`
\set worker_password \`cat $SECRETS_DIR/worker_password\`

CREATE ROLE inspection_owner
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'owner_password';

CREATE ROLE inspection_app
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'app_password';

CREATE ROLE inspection_worker
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'worker_password';

CREATE ROLE inspection_auth
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS;
GRANT inspection_auth TO inspection_owner WITH INHERIT FALSE, SET TRUE;

CREATE ROLE inspection_dispatcher
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS;
GRANT inspection_dispatcher TO inspection_owner WITH INHERIT FALSE, SET TRUE;

-- A request that leaves a transaction open must not hold locks or a pooled
-- connection forever.
ALTER ROLE inspection_app SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE inspection_app SET statement_timeout = '15s';
ALTER ROLE inspection_worker SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE inspection_worker SET statement_timeout = '30s';

CREATE DATABASE inspection OWNER inspection_owner;
CREATE DATABASE inspection_test OWNER inspection_owner;
SQL

for database in inspection inspection_test; do
  psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$database" <<SQL
REVOKE ALL ON DATABASE $database FROM PUBLIC;
GRANT CONNECT ON DATABASE $database TO inspection_owner, inspection_app, inspection_worker;

-- PUBLIC keeps no rights in the schema; the app may resolve names but not create.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO inspection_app, inspection_worker, inspection_auth,
    inspection_dispatcher;

-- Alembic's version table lives apart from app tables. The app has no access.
CREATE SCHEMA migrations AUTHORIZATION inspection_owner;
SQL
done
