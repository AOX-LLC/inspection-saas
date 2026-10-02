#!/bin/bash
# Creates the database roles and databases. Runs once, as the superuser, when
# the data volume is first initialised. Nothing else uses the superuser.
#
#   inspection_owner  owns the schema and every table. Used by migrate and seed.
#                     Tenant tables FORCE row-level security, so it is bound too.
#   inspection_app    what the API connects as. Owns nothing, cannot bypass RLS,
#                     and gets DML grants only (set up by the first migration).
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

CREATE ROLE inspection_owner
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'owner_password';

CREATE ROLE inspection_app
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'app_password';

-- A request that leaves a transaction open must not hold locks or a pooled
-- connection forever.
ALTER ROLE inspection_app SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE inspection_app SET statement_timeout = '15s';

CREATE DATABASE inspection OWNER inspection_owner;
CREATE DATABASE inspection_test OWNER inspection_owner;
SQL

for database in inspection inspection_test; do
  psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$database" <<SQL
REVOKE ALL ON DATABASE $database FROM PUBLIC;
GRANT CONNECT ON DATABASE $database TO inspection_owner, inspection_app;

-- PUBLIC keeps no rights in the schema; the app may resolve names but not create.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO inspection_app;

-- Alembic's version table lives apart from app tables. The app has no access.
CREATE SCHEMA migrations AUTHORIZATION inspection_owner;
SQL
done
