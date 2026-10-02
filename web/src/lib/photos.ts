import type { Photo } from "./types.ts";

// Newest first, the order the API lists in. Ids break ties, as the API's cursor does.
function newestFirst(a: Photo, b: Photo): number {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? 1 : -1;
  return a.id < b.id ? 1 : -1;
}

// Folds a freshly fetched page into what is already shown: a photo seen again takes
// its new state, a new one is added, and older pages stay.
//
// A thumbnail link is signed afresh on every fetch, so a new link is a new address to
// the browser, which would download the same picture again. A photo that already has
// a link keeps it, until `renewLinks` says the old ones are about to expire.
export function mergePhotos(shown: Photo[], fresh: Photo[], renewLinks = false): Photo[] {
  const byId = new Map(shown.map((photo) => [photo.id, photo]));
  for (const photo of fresh) {
    const known = byId.get(photo.id)?.thumbnail_url;
    byId.set(photo.id, !renewLinks && known && photo.thumbnail_url ? { ...photo, thumbnail_url: known } : photo);
  }
  return [...byId.values()].sort(newestFirst);
}

// Photos still waiting for the worker.
export function isUnfinished(photo: Photo): boolean {
  return photo.status === "queued" || photo.status === "processing";
}
