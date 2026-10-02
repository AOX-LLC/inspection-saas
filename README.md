# Visual Defect Inspection SaaS

[![CI](https://github.com/AOX-LLC/inspection-saas/actions/workflows/ci.yml/badge.svg)](https://github.com/AOX-LLC/inspection-saas/actions/workflows/ci.yml)

Multi-tenant building inspection: defect detection on site photos, engineer review and PDF site reports.

Built by [AOX](https://automatedoperationsexperts.com).

## Status

Phase 1a: tenancy and database isolation. Login, object storage and the web app come in later phases.

## Run it

Prerequisite: Docker Engine with Compose v2.

```sh
docker compose up -d --wait
```

The first run builds the API image, generates credentials, creates the databases, migrates and seeds.

```sh
curl http://127.0.0.1:4701/health
# {"status":"ok"}
```

Run the database isolation suite. It uses its own `inspection_test` database, never the seeded one:

```sh
docker compose --profile test run --rm --build test
```

Stop with `docker compose down`. Use `docker compose down -v` to also delete the data and the generated credentials.

## Ports

All ports are bound to 127.0.0.1.

| Port | Service | Phase |
| --- | --- | --- |
| 4701 | API | now |
| 4702 | Postgres | now |
| 4700 | Web app | later |
| 4703 | Object store | later |

## Demo data

All demo data is synthetic. The seed refuses to run unless `APP_ENV=demo`. There are no passwords yet; login arrives in Phase 1b.

| Org | User | Role |
| --- | --- | --- |
| Demo Org Alpha | `alpha.owner@alpha.example` | owner |
| Demo Org Alpha | `alpha.inspector@alpha.example` | inspector |
| Demo Org Alpha | `alpha.viewer@alpha.example` | viewer |
| Demo Org Beta | `beta.owner@beta.example` | owner |
| Both orgs | `consultant@shared.example` | inspector |

Each org has two projects whose names start with "Synthetic".

## Mock mode

`MODEL_MODE=mock` is the default. Recorded model responses are replayed and no API key is needed. See `.env.example`.

## How tenancy works

Every table uses Postgres row-level security, with both ENABLE and FORCE. The API connects as a role that owns nothing and cannot bypass RLS. Tenant context is set per transaction with `set_config(..., true)`, so it cannot leak across pooled connections. If the context is missing, the query raises an error instead of returning an empty list.

More detail: [docs/architecture.md](docs/architecture.md) and [docs/adr/0003-tenancy.md](docs/adr/0003-tenancy.md).

## Repository layout

```
api/                  FastAPI app, Alembic migrations, tests
infra/postgres/init/  roles and databases
infra/secrets/        credential generation
docs/                 architecture notes and ADRs
compose.yaml          local stack
```

## Licence

MIT. See [LICENSE](LICENSE).
