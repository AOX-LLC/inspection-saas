"use client";

import { describePhotoError } from "@/lib/messages";
import { formatBytes, plural } from "@/lib/format";
import type { Photo, Progress } from "@/lib/types";
import type { BatchItem } from "./use-upload-batch";
import { AlertIcon, CheckIcon, ClockIcon, RetryIcon, SpinnerIcon } from "./icons";

// What a row shows. After the bytes are in, the photo list says how far the worker got.
type Shown =
  | { kind: "waiting" }
  | { kind: "uploading"; percent: number }
  | { kind: "checking" }
  | { kind: "processing" }
  | { kind: "done" }
  | { kind: "failed"; message: string; retryable: boolean };

export function showItem(item: BatchItem, photo: Photo | undefined, settled: boolean): Shown {
  switch (item.phase) {
    case "queued":
      return { kind: "waiting" };
    case "uploading":
      return { kind: "uploading", percent: Math.round((item.sent / Math.max(item.file.size, 1)) * 100) };
    case "saving":
      return { kind: "checking" };
    case "failed":
      return { kind: "failed", message: item.error ?? "The upload failed.", retryable: item.retryable };
    case "processing":
      if (photo?.status === "tiled") return { kind: "done" };
      if (photo?.status === "failed") {
        return { kind: "failed", message: describePhotoError(photo.error), retryable: true };
      }
      // The project's queue is empty and a fresh look found nothing wrong: it is finished.
      return settled ? { kind: "done" } : { kind: "processing" };
  }
}

export function BatchPanel({
  items,
  photosByFile,
  settled,
  progress,
  onRetry,
  onClear,
}: {
  items: BatchItem[];
  photosByFile: Map<string, Photo>;
  settled: boolean;
  progress: Progress | null;
  onRetry: (key: string) => void;
  onClear: () => void;
}) {
  const rows = items.map((item) => ({
    item,
    shown: showItem(item, item.fileId ? photosByFile.get(item.fileId) : undefined, settled),
  }));
  const finished = rows.filter(({ shown }) => shown.kind === "done" || shown.kind === "failed").length;
  const failed = rows.filter(({ shown }) => shown.kind === "failed").length;
  const uploaded = rows.filter(({ item }) => item.phase === "processing").length;
  const allFinished = finished === rows.length;

  const summary = allFinished
    ? failed === 0
      ? `All ${plural(rows.length, "photo")} uploaded and processed.`
      : `${plural(rows.length - failed, "photo")} done, ${failed} could not be added.`
    : `${uploaded + failed} of ${rows.length} uploaded · ${finished - failed} of ${rows.length} processed`;

  return (
    <section className="card batch" aria-labelledby="batch-title">
      <div className="batch-head">
        <div>
          <h2 id="batch-title">This upload</h2>
          <p className="muted small tabular">{summary}</p>
          {/* Announced once, when the whole batch is finished, not on every file. */}
          <p className="visually-hidden" role="status">
            {allFinished ? summary : ""}
          </p>
        </div>
        {allFinished ? (
          <button type="button" className="btn btn-small" onClick={onClear}>
            Dismiss
          </button>
        ) : null}
      </div>
      <progress
        className="progress"
        max={rows.length * 2}
        value={uploaded + failed + finished}
        aria-label="Progress of this upload"
        aria-valuetext={`${finished} of ${rows.length} finished`}
      />
      {progress && !progress.finished ? (
        <p className="muted small tabular">
          {plural(progress.counts.queued + progress.counts.processing, "photo")} in this project still waiting to be
          processed.
        </p>
      ) : null}
      <ul className="list batch-items" aria-label="Files in this upload">
        {rows.map(({ item, shown }) => (
          <li key={item.key} className="batch-item">
            <span className="batch-item-name" title={item.file.name}>
              {item.file.name}
            </span>
            <StatusChip shown={shown} />
            <div className="batch-item-detail">
              <span className="tabular">{formatBytes(item.file.size)}</span>
              {shown.kind === "uploading" ? (
                <progress
                  className="progress progress-thin"
                  max={100}
                  value={shown.percent}
                  aria-label={`Uploading ${item.file.name}`}
                />
              ) : null}
              {shown.kind === "failed" ? <span className="batch-item-error">{shown.message}</span> : null}
              {shown.kind === "failed" && shown.retryable ? (
                <button type="button" className="btn btn-quiet btn-small" onClick={() => onRetry(item.key)}>
                  <RetryIcon /> Try again
                </button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function StatusChip({ shown }: { shown: Shown }) {
  switch (shown.kind) {
    case "waiting":
      return (
        <span className="chip">
          <ClockIcon /> Waiting
        </span>
      );
    case "uploading":
      return <span className="chip chip-accent tabular">Uploading {shown.percent}%</span>;
    case "checking":
      return (
        <span className="chip chip-accent">
          <SpinnerIcon /> Checking
        </span>
      );
    case "processing":
      return (
        <span className="chip chip-accent">
          <SpinnerIcon /> Processing
        </span>
      );
    case "done":
      return (
        <span className="chip chip-ok">
          <CheckIcon /> Done
        </span>
      );
    case "failed":
      return (
        <span className="chip chip-danger">
          <AlertIcon /> Failed
        </span>
      );
  }
}
