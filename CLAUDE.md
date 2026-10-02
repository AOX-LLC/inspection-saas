# CLAUDE.md

Multi-tenant visual defect inspection API. Tenant isolation is enforced in Postgres, not only in application code.

Stack: Python 3.12, FastAPI, SQLAlchemy 2 async with psycopg 3, Alembic with plain-SQL migrations, Postgres 18 with pgvector, Docker Compose. Python deps are managed with uv.

## Commands

- Start: `docker compose up -d --wait`
- Health: `curl http://127.0.0.1:4701/health`
- Tests: `docker compose --profile test run --rm --build test`
- Stop: `docker compose down` (`-v` also deletes data and generated credentials)
- Lint: `cd api && uv run ruff check . && uv run ruff format --check .`
- All hooks: `pre-commit run --all-files`

## Tenancy rules

These are invariants. Do not relax them.

- Every tenant-owned table has `org_id NOT NULL`, `ENABLE` and `FORCE ROW LEVEL SECURITY`, and a policy comparing `org_id` with `app.org_id()`.
- Child tables reference parents with a composite FK `(org_id, parent_id) -> parent(org_id, id)`.
- Set tenant context only through `app/db/tenant.py` (`tenant_transaction`, `user_transaction`). It uses transaction-local `set_config`. Never use session-level `SET` for `app.*` settings.
- The API connects as `inspection_app`, which owns nothing and has no BYPASSRLS. Migrations and seed run as `inspection_owner`. Never grant the app role ownership, TRUNCATE or BYPASSRLS.
- A new table must pass the catalog tests in `api/tests/db/`: RLS is forced on every table unless it is on the explicit global allowlist.
- Never weaken a policy or grant to make a test pass. Fix the test or the code.

## Data and secrets

- Use synthetic or openly licensed data only.
- Never commit `.env` or credentials. gitleaks runs in pre-commit and CI.
- Mock mode (`MODEL_MODE=mock`) must keep working with no API key.

## Conventions

- Bind every port to 127.0.0.1, in the 4700-4799 range.
- One concern per commit.
- Run the test suite and pre-commit before opening a PR.
