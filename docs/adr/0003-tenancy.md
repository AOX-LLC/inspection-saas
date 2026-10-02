# ADR 0003: Tenant isolation with Postgres row-level security

- Status: accepted
- Date: 2026-10-02

ADRs 0001 (object store) and 0002 (authentication) arrive with Phase 1b.

## Context

Every org's photos, detections and reports must be invisible to every other
org. Application-level `WHERE org_id = ...` filters are necessary but not
sufficient: one forgotten filter, one raw query, or one background job that
skips the check leaks data. The isolation guarantee should hold even when a
route or job has a bug.

The app runs a single Postgres database shared by all tenants. Per-tenant
databases or schemas would make migrations, pooling and cross-tenant
maintenance much heavier for a product that expects many small orgs.

## Decision

Isolation is enforced by Postgres row-level security (RLS), with the API as a
second, independent check.

**Roles.** The schema and every table are owned by `inspection_owner`, used
only by migrations and the seed. The API connects as `inspection_app`, which
owns nothing, is not a superuser, has no `BYPASSRLS`, inherits no other role,
and has DML grants only. Postgres's own superuser is used only to create these
roles at first start.

**Context.** Policies read two settings, `app.org_id` and `app.user_id`. The
API sets them with `set_config(name, value, true)` at the start of each
request's single transaction (`api/app/db/tenant.py`). The `true` makes them
transaction-local: Postgres reverts them at commit or rollback, so a pooled
connection cannot carry one tenant's context into the next request.
Session-level `SET` is never used.

**Fail loudly.** Tenant-owned tables compare `org_id` with `app.org_id()`,
which raises `insufficient_privilege` when the setting is unset or empty. A
setting reverts to `''`, not NULL, after it has been set once in a session,
so both count as unset. The accessor runs when a row is checked against the
policy, so a bug that forgets the context errors on any query that reaches a
row, instead of returning an empty list that looks like "no data". A query
that matches no rows at all (an empty table, a lookup by a key that does not
exist) may return empty without raising. It never returns rows, and the
tests do not rely on it raising. Phase 1b adds a session-layer guard that
refuses database work not opened through `tenant.py`.

**Identity tables.** `orgs`, `users` and `memberships` must be readable before
an org is chosen (to list a user's orgs) and their policies combine
conditions with OR. Postgres does not guarantee OR short-circuits, so these
use null-safe accessors (`app.org_id_or_null()`, `app.user_id_or_null()`).
An unset context matches no rows, so they fail closed. `memberships` does not
refer to `orgs` or `users` in its policy, which keeps the policies from
recursing.

**Writes match the context.** Every `WITH CHECK` uses the strict accessors.
You can only insert an org whose id is the context org, only insert yourself
as a user, and only write tenant rows for the context org. Who may do those
things (owner, admin, inspector, viewer) is decided in the API; RLS is the
tenancy wall, not the role system.

**FORCE.** Every table has `ENABLE` and `FORCE ROW LEVEL SECURITY`, so the
owner role is bound by the same policies. Migrations and the seed set context
like the API does.

**Composite foreign keys.** Children reference parents by `(org_id, id)`, for
example `files (org_id, project_id) -> projects (org_id, id)`. A row cannot
point at another org's parent even if a policy were wrong.

**Locked tables.** `sessions` has RLS forced, no policy and no app grant.
`audit_events` is append-only for the app: `INSERT` and `SELECT`, no `UPDATE`
or `DELETE`.

**Ids** are random UUIDv4, so they cannot be enumerated and do not reveal
creation time.

**Performance.** Accessors are wrapped in a scalar subquery,
`org_id = (SELECT app.org_id())`, so Postgres evaluates them once per
statement rather than once per row, and indexes on `org_id` stay usable.

## Tests

`api/tests/db/` checks this design against a real Postgres, connecting as the
app role:

- Catalog: role attributes, ownership, exact per-table and no column-level
  privileges, RLS forced on every table outside an explicit global allowlist,
  every permissive policy on an `org_id` table comparing `org_id` with
  `app.org_id()`, every FK between `org_id` tables carrying `org_id`, no
  materialized views, foreign tables or owner-run views, and SECURITY DEFINER
  functions with a pinned `search_path` and no PUBLIC `EXECUTE`. Each check is
  also run against a deliberately unsafe object to prove it catches one, and
  new tables and functions are covered automatically.
- Behaviour: unset context, cross-org reads and writes, upserts onto another
  org's row, rows moved between orgs, identity rows for a member of two orgs,
  the same physical connection reused across transactions (directly and
  through the app's engine), FORCE on the owner, the locked tables, the
  composite FK, and file object keys bound to their own org.

## Consequences

- Every query must run inside `tenant_transaction` or `user_transaction`.
  Code that forgets gets an error in development, not a leak in production.
- RLS does not check membership. The API must confirm that the user belongs to
  the org before it sets `app.org_id`, and it answers 404 for orgs the user is
  not in so their existence is not revealed (Phase 1b).
- Work that has no user, such as background jobs, needs its own path to a
  tenant context. Phase 2 adds a claim function that returns a job's org, and
  the worker then sets context and runs under RLS like the API.
- Logins and session lookups happen before any context exists, and FORCE RLS
  binds the owner too. Phase 1b adds narrow SECURITY DEFINER functions for
  them, owned by a dedicated role, with a pinned `search_path`, no PUBLIC
  `EXECUTE`, and `EXECUTE` granted only to the app role.
- Policies add a small cost to every query. The per-statement accessor and the
  `org_id` indexes keep it low; it is not measured yet.

## Alternatives considered

- **Application filters only.** Simpler, but one missed filter leaks data and
  nothing catches it.
- **Schema or database per tenant.** Strong isolation, but migrations and
  connection pools multiply with the number of orgs.
- **Membership checks inside the policies.** Would also stop an API bug that
  sets the wrong org. Rejected for now: it adds a join to every query and does
  not fit work with no user, such as jobs. The API check and the route tests
  in Phase 1b cover that case instead.
