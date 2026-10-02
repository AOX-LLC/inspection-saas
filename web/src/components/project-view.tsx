"use client";

import { useEffect, useEffectEvent, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { describeLoadFailure } from "@/lib/messages";
import { plural } from "@/lib/format";
import { isUnfinished } from "@/lib/photos";
import { MAX_FILES_PER_BATCH } from "@/lib/uploader";
import type { Photo, Progress } from "@/lib/types";
import { BatchPanel } from "./batch-panel";
import { Dropzone } from "./dropzone";
import { PhotoGrid } from "./photo-grid";
import { Alert, Busy, RetryAlert } from "./ui";
import { useMe } from "./session";
import { usePhotos } from "./use-photos";
import { useUploadBatch } from "./use-upload-batch";

const POLL_MS = 1500;
const POLL_MAX_MS = 8000;
const WRITER_ROLES = new Set(["owner", "admin", "inspector"]);

// Upload area, this upload's progress, and the grid of finished photos. Whether the
// person may upload is the API's call; the role only decides whether to offer the control.
export function ProjectView({ orgId, projectId }: { orgId: string; projectId: string }) {
  const role = useMe().orgs.find((org) => org.id === orgId)?.role;
  const canUpload = role !== undefined && WRITER_ROLES.has(role);

  const photos = usePhotos(orgId, projectId);
  const batch = useUploadBatch(orgId, projectId, () => void photos.refresh());
  const [progress, setProgress] = useState<Progress | null>(null);
  const [skipped, setSkipped] = useState(0);
  // Set once the project's queue is empty and one more look at the photos has been taken.
  const [settledAt, setSettledAt] = useState(0);

  const photosByFile = useMemo(() => new Map(photos.photos.map((photo) => [photo.file_id, photo])), [photos.photos]);
  const waiting = batch.items.some((item) => {
    if (item.phase !== "processing") return false;
    const photo = item.fileId ? photosByFile.get(item.fileId) : undefined;
    return !photo || isUnfinished(photo);
  });
  const settled = settledAt === batch.items.length && batch.items.length > 0;
  const polling = waiting && !settled;

  // While the worker is busy with this upload, ask the API how far it got, and
  // refresh the grid whenever the numbers move.
  const refreshPhotos = useEffectEvent(() => photos.refresh());
  const lastDone = useRef(-1);
  useEffect(() => {
    if (!polling) return;
    let stopped = false;
    let delay = POLL_MS;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const count = batch.items.length;

    async function tick() {
      try {
        const latest = await api.progress(orgId, projectId);
        if (stopped) return;
        setProgress(latest);
        const done = latest.counts.tiled + latest.counts.failed;
        if (done !== lastDone.current || latest.finished) {
          lastDone.current = done;
          await refreshPhotos();
        }
        if (stopped) return;
        if (latest.finished) {
          setSettledAt(count);
          return;
        }
        delay = POLL_MS;
      } catch {
        delay = Math.min(delay * 2, POLL_MAX_MS);
      }
      if (!stopped) timer = setTimeout(tick, delay);
    }
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
    // The count is read once per run: a newly added file restarts polling by changing `polling`'s inputs.
  }, [polling, orgId, projectId, batch.items.length]);

  function addFiles(files: File[]) {
    setSkipped(batch.add(files));
  }

  function clearBatch() {
    batch.clear();
    setSkipped(0);
    setProgress(null);
    setSettledAt(0);
  }

  return (
    <>
      {canUpload ? (
        <section aria-label="Upload photos" className="page-head">
          <Dropzone onFiles={addFiles} />
          {skipped > 0 ? (
            <Alert tone="warn">
              A batch holds up to {MAX_FILES_PER_BATCH} photos, so {plural(skipped, "photo")} {skipped === 1 ? "was" : "were"} left
              out. Add {skipped === 1 ? "it" : "them"} again once this upload has finished.
            </Alert>
          ) : null}
        </section>
      ) : (
        <Alert tone="warn">
          Your role in this organization lets you view photos but not upload them.
        </Alert>
      )}

      {batch.items.length > 0 ? (
        <BatchPanel
          items={batch.items}
          photosByFile={photosByFile}
          settled={settled}
          progress={progress}
          onRetry={batch.retry}
          onClear={clearBatch}
        />
      ) : null}

      <section aria-labelledby="photos-title" className="page-head">
        <div className="section-head">
          <h2 id="photos-title">Photos</h2>
          {photos.status === "ready" ? (
            <span className="muted small tabular">
              {plural(photos.photos.length, "photo")}
              {photos.hasMore ? " so far" : ""}
            </span>
          ) : null}
        </div>
        <PhotosBody photos={photos} />
      </section>
    </>
  );
}

function PhotosBody({ photos }: { photos: ReturnType<typeof usePhotos> }) {
  if (photos.status === "loading") return <PhotoSkeletons />;
  if (photos.status === "error") {
    return <RetryAlert message={describeLoadFailure(photos.error, "project")} onRetry={() => void photos.refresh()} />;
  }
  return (
    <>
      {photos.stale ? (
        <RetryAlert message="Couldn't refresh the photos. What's shown may be out of date." onRetry={() => void photos.refresh()} />
      ) : null}
      <PhotoGrid photos={photos.photos} onThumbnailError={photos.renewLinks} />
      {photos.hasMore ? (
        <div className="center">
          <button type="button" className="btn" onClick={() => void photos.loadMore()} disabled={photos.loadingMore}>
            {photos.loadingMore ? "Loading…" : "Show older photos"}
          </button>
        </div>
      ) : null}
    </>
  );
}

function PhotoSkeletons() {
  return (
    <div aria-busy="true">
      <Busy>Loading photos…</Busy>
      <ul className="grid" aria-hidden="true">
        {Array.from({ length: 8 }, (_, index) => (
          <li key={index} className="photo">
            <div className="photo-frame skeleton" />
          </li>
        ))}
      </ul>
    </div>
  );
}

export type { Photo };
