# ADR 0004: A Postgres job queue and a tenant-scoped worker

- Status: accepted
- Date: 2026-10-02

See also [ADR 0001](0001-object-store.md) (object store),
[ADR 0002](0002-auth.md) (the auth functions this reuses as a pattern) and
[ADR 0003](0003-tenancy.md) (row-level security).

## Context

Uploaded photos must be processed in the background: cut into tiles now, run
through a detector, a model and a report generator in later phases. That work
belongs to one org, but a worker serves all of them, so it is the one place
outside a request where a process must reach across tenants to find its next
job. Whatever finds the job must not become a way to read every org's data.

Batches are tens of photos, and the host has little memory to spare.

## Decision

**A queue in Postgres, written in-house.** The `jobs` table is tenant-owned like
any other: `org_id NOT NULL`, forced row-level security, one policy comparing
`org_id` with `app.org_id()`.

- *Enqueue is part of the write.* The API inserts the photo and its job in the
  same transaction as the file's status change (`app/photos/service.py`). Both
  commit or neither does, so there is never a photo nobody will tile.
- *Payloads are ids.* A CHECK on `jobs.payload` allows only a small object (500
  characters at most) whose keys are short snake_case names and whose values are
  canonical UUID strings. Nothing else can be stored, so nothing else can be
  returned by the cross-tenant claim.
- *Wake-up.* An `AFTER INSERT` trigger sends `NOTIFY inspection_jobs` with no
  payload; it is delivered at commit. Workers `LISTEN` and also poll every few
  seconds, so a lost notification or a retry that has become due costs one poll
  interval.
- *No Redis or Celery.* One less container, and the outbox property above comes
  free. procrastinate and pgqueuer would need cross-tenant reads on their own
  tables; the tenant-aware claim is the part that has to be ours.

**Claiming is the one cross-tenant path, and it is narrow.** It follows the
pattern of ADR 0002. Five `SECURITY DEFINER` functions in schema `queue` are
owned by `inspection_dispatcher`: `NOLOGIN`, `BYPASSRLS`, owning nothing else,
holding `SELECT, UPDATE` on `jobs` and `SELECT` on `files` and `photos`. Each pins
`search_path`, has `EXECUTE` revoked from PUBLIC, and is granted to
`inspection_worker` alone.

- `jobs_claim` returns four columns: job id, org id, kind, payload.
- `jobs_complete` and `jobs_fail` act only on a job the caller still holds
  (`locked_by`), and return whether they did.
- `abandoned_uploads` returns org and file ids of uploads to clean up.
- `stuck_photos` returns org and photo ids of photos whose job has failed.

`inspection_worker` is `NOSUPERUSER NOBYPASSRLS`, owns nothing, and has **no
grant on `jobs`**. Its other grants are listed in migration 0004 and pinned by
a test; it inherits nothing from the default privileges the app role gets. It
may update only a photo's status, size, error and timestamp, and a restrictive
policy limits its deletes on `files` to uploads that never finished.

**The worker does everything else under row-level security.** After a claim it
opens `tenant_transaction(org_id=job.org_id)` and reads and writes through the
same policies as the API. A payload that names another org's photo finds
nothing: the rows are invisible, and every query also says `org_id = :org_id`
explicitly. The job then fails with a fixed code and touches nothing.

**Locks, attempts and failure.** A claim sets `locked_until` and counts as an
attempt. A running job whose lock has lapsed belongs to a worker that died, and
the next claim takes it again. A failed attempt is released with exponential
backoff (base doubling, capped at an hour). A job that is out of attempts, or
whose error is not retryable, becomes `failed`; a lapsed lock on a job with no
attempts left also becomes `failed`, so a file that kills its worker cannot loop
forever. That sweep happens in SQL, where the photo is out of reach, so the
worker's housekeeping finds photos whose job has failed (`stuck_photos`) and
marks them failed under their own org's context; a project's progress can
therefore always finish. A handler is also given up on after 90% of `JOB_LOCK_SECONDS` (at most an hour,
what the claim allows). The thread decoding a pathological file cannot be
interrupted, but the loop stops waiting for it and the job is retried; the
thread's decode slot is held until it really ends, so a second decode never
starts beside it in a container sized for one. A lock holder's name includes a
random part, so another worker cannot guess it. Errors stored on a job or photo are fixed codes, never messages.
Handlers are written to be repeated; processing is at-least-once.

**Tiling.**

- Decode with Pillow after checking the pixel count from the header
  (`MAX_IMAGE_PIXELS`, default 50 megapixels), so a decompression bomb costs a
  few kilobytes. Only JPEG, PNG and WebP are tried. The EXIF orientation is
  applied, so every coordinate is in the photo as a person sees it.
- Tiles are cut at the detector's input size (`TILE_SIZE`, default 640) with a
  fixed overlap (`TILE_OVERLAP`, default 128), the last tile on each axis
  aligned to the edge. A photo larger than one tile also gets a single
  downscaled overview, so a defect larger than a tile can be seen whole.
- Each tile row records the region it covers in the original (`x`, `y`,
  `src_width`, `src_height`) and its `scale`, so a detected box maps back:
  `original = origin + tile_px / scale`.
- A photo that would need more than `MAX_TILES_PER_PHOTO` tiles is refused
  before any are cut, which bounds a long thin image.
- Tiles are JPEGs written to `.../photos/{photo_id}/tiles/{level}_{x}_{y}.jpg`
  with none of the original's metadata (no EXIF, so no camera, time or
  location, and no JPEG comment). A tile row's key is bound by a CHECK to its
  own org and photo.

**Uploads go to a staging key.** The presigned POST targets
`.../files/{id}/upload`, never the final key. `complete` first claims the
row (`pending` to `completing`, atomically, so two concurrent completions cannot
both copy), checks the staged object's size and magic bytes, copies it
server-side to `.../original` pinned to the etag it checked, re-checks the copy,
then deletes the staged object. A failure before publishing releases the row back
to `pending`; one that is never released is removed by the cleanup like any
abandoned upload. An upload within ten minutes of that age can no longer be
completed (`complete` answers 409), so cleanup never removes a row that is being
completed. A cancelled request is not released either, because its copy may still
be running; the cleanup removes the row later. The
final key is never a POST target, so a finished upload cannot be overwritten
inside the POST's expiry window.

**Housekeeping.** Every worker runs a periodic cleanup, safe to repeat:

- A `pending` or `completing` upload older than `ABANDONED_UPLOAD_SECONDS` (never less than 15
  minutes, past a presigned POST's lifetime) loses its row and both objects. The
  objects go first inside the transaction that deletes the row, so a storage
  failure rolls the delete back and nothing is orphaned.
- A finished upload's staging key is deleted once after the POST has expired
  (a replay can recreate it) and `files.staging_swept_at` records that.
- Photos whose job failed are marked failed (`queue.stuck_photos`, served by
  partial indexes on unfinished photos and failed jobs). Each cleanup step runs
  even if another fails.
- Dead sessions are removed by `auth.purge_sessions`, a fifth `SECURITY DEFINER`
  function owned by the auth role and executable by the worker alone. The worker
  has no grant on `sessions`.

## Tests

`api/tests/db/` pins the roles, the grants, and the owner and executing roles of
every definer function, and proves: a worker holding org A's job cannot read,
change or write org B's rows; a claimed job whose worker dies is reclaimed after
`locked_until`; the claim function exposes exactly its four columns and the
payload cannot hold anything but ids. `api/tests/worker/` runs the real worker
against Postgres and the object store: tiling geometry, orientation, bombs,
retries, crash recovery, wake-up and polling, cleanup. `api/tests/api/test_batch.py`
uploads 50 synthetic photos through the API, runs a worker and reads the counts
back through the progress endpoint.

## Consequences

- **A compromised worker process can still choose its org.** Row-level security
  keeps a worker's *queries* inside the org in its context, and the context is
  set by the worker process, as the API sets it. The claim narrows which jobs
  and org ids it is handed; it does not stop code running in the worker from
  setting a different `app.org_id`. The same is true of the API, and the
  defence is the same: no `BYPASSRLS`, narrow grants, a read-only container with
  no capabilities. Tying the context to a held job inside the database is
  possible and not built.
- **The worker shares the API's object-store key.** Both can read and write the
  uploads bucket. A separate key for the worker is a possible hardening.
- **No lock heartbeat.** A job that runs longer than `JOB_LOCK_SECONDS` can be
  claimed by a second worker; because handlers repeat safely the result is
  duplicate work, not corruption, and the first worker's `complete` is refused.
- **Cleanup runs in every worker.** There is no leader election. Each step is
  idempotent; the cost is some duplicate work with several workers.
- **Photos over the pixel limit are refused**, including a large real photograph.
  The limit is a setting sized to the worker container's memory.
- **Throughput under load, and memory with large photos at higher concurrency,
  are not measured.** The default is one job at a time.
- **There is no erasure path for objects yet.** Deleting an org cascades its
  rows, but every original and tile (up to 513 derived copies of each image)
  stays in the bucket under `orgs/{org_id}/`. Removing that prefix is the
  intended mechanism and is not built; it must exist before this holds personal
  data.
- **No fairness between orgs.** Claims are first come, first served. Each org is
  limited to about 2000 photos waiting or running (checked when an upload starts,
  by count then insert, so it can be overshot by the uploads already in flight),
  which bounds how long one org can hold the others up; round-robin claiming is not built.
- **Finished jobs are never pruned.**
- **The API role can insert a job with any column set** (status, attempts, lock),
  not only the three it needs, and the dispatcher can update any job column.
  Column grants would narrow both, but the catalog tests assert that the app has
  no column-level grants, so it is left as a known gap.
- Peak memory while decoding a 50-megapixel photo was measured at about 415 MB
  (RGB) and 500 MB (with an alpha channel) in the worker's 768 MB container, so
  one job at a time fits and two do not. The worker is restarted automatically if
  it dies, which is the only protection against an out-of-memory kill.

## Alternatives considered

- **Redis or Celery.** Another service to run and no transactional enqueue.
- **A library on top of Postgres.** Needs cross-tenant reads on its own tables.
- **A worker role with `BYPASSRLS`.** Simplest, and gives one process every
  org's data. The narrow claim exists to avoid it.
- **Putting the org in the payload and trusting it.** The org comes from the
  claimed row, which was written under a policy that matched an authorised
  request; a payload is only ever ids to look up under that org.
