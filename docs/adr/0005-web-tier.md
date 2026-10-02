# ADR 0005: A web tier that holds no logic

- Status: accepted
- Date: 2026-10-02

See also [ADR 0001](0001-object-store.md) (the object store the browser talks
to), [ADR 0002](0002-auth.md) (sessions and the login limit) and
[ADR 0003](0003-tenancy.md) (why the API is the only authority).

## Context

People need a browser interface: sign in, pick a project, drop in a batch of
photos, watch them upload and get processed, and see the finished photos. The
API already enforces tenancy, roles, upload checks and rate limits, and those
must stay in one place. Photo bytes are large and must not pass through
anything but the object store.

The host is a small shared machine, so memory matters at build time and at run
time.

## Decision

**Next.js (TypeScript, App Router), served as a production build, and nothing
more than a front end.** The `web` service rewrites `/api/*` to the API, so the
browser only ever talks to one origin and the `HttpOnly` session cookie stays
same-origin (no CORS on the API, no token in JavaScript). Every decision about
what a person may see or do is the API's: the web tier hides an upload area
from a viewer, but the API refuses the upload either way, and its answers are
shown in plain language, not interpreted.

*Photos never pass through it.* The page asks the API for a presigned POST,
sends the bytes straight to the object store from the browser, then tells the
API the upload is complete. The grid shows thumbnails through short-lived
presigned GETs and never loads an original.

**The page's Content-Security-Policy is built per request** (`web/src/proxy.ts`,
`web/src/lib/csp.ts`). Scripts run only with a per-request nonce; there are no
inline styles of our own; images and connections are allowed from this origin
and the object store's public origin and nowhere else; `frame-ancestors 'none'`;
`object-src 'none'`. The store's origin comes from the environment when a page
is served, so one image works wherever the store is reachable. Every page is
rendered per request, because a nonce cannot be baked into a static page. Other
headers (nosniff, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`,
`Permissions-Policy`, `Cross-Origin-Opener-Policy`) are constant and set in
`next.config.ts`. `upgrade-insecure-requests` is added only when the store is on
https, because on plain `http://127.0.0.1` it would break the store.

**The object store's CORS allows the web origin only** (`ALLOWED_ORIGINS`,
default `http://127.0.0.1:4700`), for `GET` and `POST`, with only a
`content-type` request header. The API's Origin check uses the same list. A
wildcard is never configured.

**`X-Forwarded-For` is trusted from the web container only.** The API's login
limit is per client IP, and behind the web service every request arrives from
the web container's address. Next.js's rewrite passes the header through exactly
as the client sent it, so trusting it as it stands would let a client name any
address. The web container therefore appends the address of the socket each
request arrived on (`web/server/forwarded-for.mjs`, loaded with
`node --import`), and the API trusts that header only for requests whose own
socket address is in `TRUSTED_PROXIES` (the `web` service name, resolved to
addresses and re-resolved every 30 seconds). It reads the header from the right
and takes the first address that is not itself a trusted proxy. A request that
comes straight to the API keeps its socket address, whatever header it sends.
See [ADR 0002](0002-auth.md).

**The seeded demo accounts are listed on the login page in demo mode only.**
`GET /auth/demo-accounts` exists only when `APP_ENV=demo`; in any other mode
the route is not registered and answers 404 like any unknown path, so the web
tier needs no mode of its own. It is built from the seed's own constants and
never reads the database.

**Lists use keyset pagination.** The file and photo lists page by
`(created_at, id)` with an opaque cursor and `(org_id, project_id, created_at,
id)` indexes, so a deep page costs what the first does and rows added during
paging cannot shift a page. The photo grid lists newest first, so a photo just
uploaded is on the first page.

**One extra object per photo, a thumbnail.** The worker writes
`.../photos/{photo_id}/thumb.jpg` (the oriented photo fitted inside 320 pixels,
no metadata) while it tiles, from the same decode, and records the key in
`photos.thumb_key`. A CHECK binds the key to the photo's own org, project and
id. Nothing in the tile table changes, so the detector's inputs are untouched.

**The build happens once, inside the image.** `docker compose build web` runs one
`next build` in a Node image with its heap capped, then copies only the
standalone server and static files into the runtime image, which runs as a
non-root user on a read-only root filesystem with a 256 MB limit. Nothing is
built on the host.

## Consequences

- Sign-in state is the API's answer to `/auth/me`; there is no server-side
  session in the web tier to keep in step, and nothing to secure there beyond
  its headers.
- Pages are client components that fetch through `/api`. Server rendering adds
  little here and would need the session cookie forwarded server to server.
- Behind the published port, Docker presents every local client to the web
  container as the bridge gateway, so the per-IP login limit is still one
  bucket for them. Clients on the Compose network, and clients behind a
  reverse proxy whose address is listed in `TRUSTED_PROXIES`, are told apart.
  The per-email limit remains the real brake.
- A thumbnail link is valid for 15 minutes. A grid left open refreshes its links
  every 10 minutes and again if an image fails to load; a refresh starts again from
  the newest page, so older pages that were opened are closed and fetched again on request.
- The rewrite forwards only `/api/auth`, `/api/orgs` and `/api/health`. The API's
  interactive docs stay on the API's own port, because served from the web origin they
  would load third-party script on an origin the API trusts. The web service answers
  `/healthz` for itself, with the build's revision when `APP_COMMIT` is set.
- Photos tiled before thumbnails existed have no `thumb_key` and show a
  "Preview unavailable" tile. Nothing backfills them yet.
- Thumbnail presigns are not written to the audit log (they are many, small and
  carry no original); original downloads still are.
