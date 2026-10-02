import type { PresignedPost } from "./types.ts";
import { describeStoreFailure, MAX_UPLOAD_BYTES } from "./messages.ts";
import { formatBytes } from "./format.ts";

// The browser's side of an upload. The API decides what is allowed; these checks
// only spare a round trip, and the API repeats every one of them.

export const ACCEPTED_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;
// The API refuses more than this many unfinished uploads at once.
export const MAX_FILES_PER_BATCH = 100;
// Files sent at the same time. Photos are large; a few in flight saturate a link.
export const UPLOAD_CONCURRENCY = 3;

export interface FileLike {
  name: string;
  size: number;
  type: string;
}

// A sentence saying why the file cannot be uploaded, or null if it can.
export function checkFile(file: FileLike): string | null {
  if (file.size === 0) return "This file is empty.";
  if (!(ACCEPTED_TYPES as readonly string[]).includes(file.type)) {
    const what = file.type ? `a ${file.type.split("/").pop()?.toUpperCase()} file` : "an unknown file type";
    return `Only JPEG, PNG and WebP photos can be uploaded. This is ${what}.`;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return `This file is ${formatBytes(file.size)}. The limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`;
  }
  return null;
}

// Runs `work` over `items`, at most `limit` at a time, and resolves when all are done.
// `work` must not throw; each item's outcome is its own business.
export async function runPool<T>(items: T[], limit: number, work: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const lane = async () => {
    while (next < items.length) {
      const item = items[next++] as T;
      await work(item);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
}

export class StoreError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(describeStoreFailure(status));
    this.name = "StoreError";
    this.status = status;
  }
}

// Sends the file to the object store with the API's presigned POST. Uses
// XMLHttpRequest because fetch cannot report upload progress.
export function postToStore(
  post: PresignedPost,
  file: File,
  onProgress: (sent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const [name, value] of Object.entries(post.fields)) form.append(name, value);
    // The store requires the file to be the last field.
    form.append("file", file);

    const request = new XMLHttpRequest();
    request.open("POST", post.url);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.min(event.loaded, file.size));
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) resolve();
      else reject(new StoreError(request.status));
    };
    request.onerror = () => reject(new StoreError(0));
    request.ontimeout = () => reject(new StoreError(0));
    request.send(form);
  });
}
