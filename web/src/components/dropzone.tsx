"use client";

import { useState, type DragEvent } from "react";
import { formatBytes } from "@/lib/format";
import { MAX_UPLOAD_BYTES } from "@/lib/messages";
import { ACCEPTED_TYPES, MAX_FILES_PER_BATCH } from "@/lib/uploader";
import { UploadIcon } from "./icons";

export function Dropzone({ onFiles }: { onFiles: (files: File[]) => void }) {
  const [dragging, setDragging] = useState(false);

  function drop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    const files = Array.from(event.dataTransfer.files);
    if (files.length > 0) onFiles(files);
  }

  return (
    <label
      className="dropzone"
      data-active={dragging}
      onDragEnter={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => {
        // Moving over a child element also fires leave; only the zone's own edge counts.
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={drop}
    >
      <input
        className="visually-hidden"
        type="file"
        multiple
        accept={ACCEPTED_TYPES.join(",")}
        aria-label="Choose photos to upload"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          // Cleared so choosing the same files again still fires a change.
          event.target.value = "";
          if (files.length > 0) onFiles(files);
        }}
      />
      <UploadIcon />
      <span className="dropzone-title">{dragging ? "Drop to upload" : "Drop photos here, or choose files"}</span>
      <span className="muted small">
        JPEG, PNG or WebP, up to {formatBytes(MAX_UPLOAD_BYTES)} each, {MAX_FILES_PER_BATCH} at a time
      </span>
    </label>
  );
}
