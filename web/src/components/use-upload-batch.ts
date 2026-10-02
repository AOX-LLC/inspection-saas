"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import { api, ApiError } from "@/lib/api";
import { describeUploadFailure } from "@/lib/messages";
import { checkFile, MAX_FILES_PER_BATCH, postToStore, runPool, StoreError, UPLOAD_CONCURRENCY } from "@/lib/uploader";

// What this browser knows about one file. After "processing" the worker's progress
// is read from the photo list, not stored here.
export type UploadPhase = "queued" | "uploading" | "saving" | "processing" | "failed";

export interface BatchItem {
  key: string;
  file: File;
  phase: UploadPhase;
  // Bytes sent so far while uploading.
  sent: number;
  fileId: string | null;
  error: string | null;
  // Trying again is pointless for a file the checks refuse.
  retryable: boolean;
}

type Action =
  | { type: "add"; items: BatchItem[] }
  | { type: "patch"; key: string; patch: Partial<BatchItem> }
  | { type: "clear" };

function reduce(items: BatchItem[], action: Action): BatchItem[] {
  switch (action.type) {
    case "add":
      return [...items, ...action.items];
    case "patch":
      return items.map((item) => (item.key === action.key ? { ...item, ...action.patch } : item));
    case "clear":
      return [];
  }
}

// The API turned the file itself down (wrong type, too big, not a photo); the same file
// would be turned down again.
function isFinalRefusal(error: unknown): boolean {
  return error instanceof ApiError && [413, 415, 422].includes(error.status);
}

export interface UploadBatch {
  items: BatchItem[];
  // Files left out because a batch holds at most MAX_FILES_PER_BATCH.
  add: (files: File[]) => number;
  retry: (key: string) => void;
  clear: () => void;
}

// Uploads photos to a project a few at a time: ask the API for a presigned POST,
// send the bytes straight to the object store, then tell the API it is complete.
// `onCompleted` runs after each file the API accepted.
export function useUploadBatch(orgId: string, projectId: string, onCompleted: () => void): UploadBatch {
  const [items, dispatch] = useReducer(reduce, []);
  const counter = useRef(0);
  const completed = useRef(onCompleted);
  useEffect(() => {
    completed.current = onCompleted;
  });

  const send = useCallback(
    async (item: BatchItem) => {
      const patch = (changes: Partial<BatchItem>) => dispatch({ type: "patch", key: item.key, patch: changes });
      patch({ phase: "uploading", sent: 0, error: null });
      try {
        const start = await api.startUpload(orgId, projectId, item.file);
        await postToStore(start.upload, item.file, (sent) => patch({ sent }));
        patch({ phase: "saving", sent: item.file.size });
        await api.completeUpload(orgId, projectId, start.file_id);
        patch({ phase: "processing", fileId: start.file_id });
        completed.current();
      } catch (error) {
        patch({
          phase: "failed",
          error: error instanceof StoreError ? error.message : describeUploadFailure(error),
          retryable: !isFinalRefusal(error),
        });
      }
    },
    [orgId, projectId],
  );

  const add = useCallback(
    (files: File[]) => {
      const accepted = files.slice(0, MAX_FILES_PER_BATCH);
      const made = accepted.map((file): BatchItem => {
        const refusal = checkFile(file);
        return {
          key: `${(counter.current += 1)}`,
          file,
          phase: refusal ? "failed" : "queued",
          sent: 0,
          fileId: null,
          error: refusal,
          retryable: refusal === null,
        };
      });
      dispatch({ type: "add", items: made });
      void runPool(
        made.filter((item) => item.phase === "queued"),
        UPLOAD_CONCURRENCY,
        send,
      );
      return files.length - accepted.length;
    },
    [send],
  );

  const retry = useCallback(
    (key: string) => {
      const item = items.find((candidate) => candidate.key === key);
      if (item && item.phase === "failed" && item.retryable) void send(item);
    },
    [items, send],
  );

  const clear = useCallback(() => dispatch({ type: "clear" }), []);
  return { items, add, retry, clear };
}
