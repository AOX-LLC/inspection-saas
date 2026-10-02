"use client";

import { describePhotoError } from "@/lib/messages";
import type { Photo } from "@/lib/types";
import { AlertIcon, ClockIcon, ImageIcon, SpinnerIcon } from "./icons";
import { EmptyState } from "./ui";

export function PhotoGrid({ photos, onThumbnailError }: { photos: Photo[]; onThumbnailError: () => void }) {
  if (photos.length === 0) {
    return (
      <div className="card">
        <EmptyState icon={<ImageIcon />} title="No photos yet">
          Drop photos above to upload them. They appear here once they have been processed.
        </EmptyState>
      </div>
    );
  }
  return (
    <ul className="grid" aria-label="Photos">
      {photos.map((photo) => (
        <li key={photo.id} className="photo">
          <div className="photo-frame">
            {photo.status === "tiled" && photo.thumbnail_url ? (
              // The grid shows the small thumbnail only; an original is never loaded here.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={photo.thumbnail_url}
                alt=""
                width={photo.width ?? undefined}
                height={photo.height ?? undefined}
                loading="lazy"
                decoding="async"
                onError={onThumbnailError}
              />
            ) : (
              <PhotoState photo={photo} />
            )}
          </div>
          <div className="photo-caption">
            <span className="photo-name" title={photo.original_filename ?? undefined}>
              {photo.original_filename ?? "Untitled photo"}
            </span>
            {photo.width && photo.height ? (
              <span className="tabular">
                {photo.width} × {photo.height}
              </span>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  );
}

function PhotoState({ photo }: { photo: Photo }) {
  if (photo.status === "failed") {
    return (
      <div className="photo-state photo-state-failed">
        <AlertIcon />
        <span>{describePhotoError(photo.error)}</span>
      </div>
    );
  }
  if (photo.status === "tiled") {
    // Tiled before thumbnails existed, so there is nothing to show but the photo is fine.
    return (
      <div className="photo-state">
        <ImageIcon />
        <span>Preview unavailable</span>
      </div>
    );
  }
  return (
    <div className="photo-state skeleton">
      {photo.status === "processing" ? <SpinnerIcon /> : <ClockIcon />}
      <span>{photo.status === "processing" ? "Processing" : "Waiting"}</span>
    </div>
  );
}
