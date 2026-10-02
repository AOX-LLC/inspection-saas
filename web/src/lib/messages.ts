import { ApiError } from "./api.ts";
import { formatBytes, formatWait } from "./format.ts";

// Plain-language text for everything that can go wrong. The API's wording is
// never shown raw except where it already is a sentence meant for people.

export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export function describeLoginFailure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "That email and password don't match an account.";
    if (error.status === 429) {
      const wait = error.retryAfter ? ` Try again in ${formatWait(error.retryAfter)}.` : " Try again later.";
      return `Too many sign-in attempts.${wait}`;
    }
    if (error.status === 503) return "The server is busy. Try again in a few seconds.";
    if (error.status === 0) return unreachable();
    if (error.status === 403) return "Sign-in was blocked. Open this app at the address it was set up for (http://127.0.0.1:4700 unless your administrator says otherwise) and try again.";
  }
  return "Something went wrong signing in. Try again.";
}

export function describeLoadFailure(error: unknown, what: string): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return unreachable();
    if (error.status === 404) return `This ${what} doesn't exist, or you don't have access to it.`;
    if (error.status >= 500) return `The server had a problem loading this ${what}. Try again.`;
  }
  return `Couldn't load this ${what}. Try again.`;
}

function unreachable(): string {
  return "Can't reach the server. Check your connection and try again.";
}

// Why a file was refused before or during upload, from the API's status and text.
export function describeUploadFailure(error: unknown): string {
  if (!(error instanceof ApiError)) return "The upload failed. Try again.";
  const detail = error.message.toLowerCase();
  switch (error.status) {
    case 0:
      return unreachable();
    case 403:
      return "Your role in this organization can't upload photos.";
    case 404:
      return "This project no longer exists, or you no longer have access to it.";
    case 409:
      return detail.includes("expired")
        ? "The upload took too long to finish. Try it again."
        : "This upload was already finished or is being finished.";
    case 413:
      return `This file is over the ${formatBytes(MAX_UPLOAD_BYTES)} limit.`;
    case 415:
      return "Only JPEG, PNG and WebP photos can be uploaded.";
    case 422:
      if (detail.includes("does not match")) {
        return "The file's contents don't match its type, so it was rejected. Is it really a photo?";
      }
      if (detail.includes("larger than declared")) {
        return "The file changed size while it was uploading. Try again.";
      }
      return "The file was rejected by the upload check.";
    case 429:
      // The API's sentence for this case already says what to do.
      return error.message || "Too many uploads at once. Wait a moment and try again.";
    default:
      return error.status >= 500
        ? "The server had a problem. Try again in a moment."
        : "The upload failed. Try again.";
  }
}

// Why the object store refused the bytes.
export function describeStoreFailure(status: number): string {
  if (status === 0) return "Couldn't reach the photo storage. Check your connection and try again.";
  if (status === 400 || status === 403) {
    return "The photo storage refused the file. It may be larger than declared, or the upload link expired.";
  }
  return "The photo storage had a problem. Try again in a moment.";
}

// Why the worker could not process a photo, from the short code the API stores.
export function describePhotoError(code: string | null): string {
  switch (code) {
    case "too_many_pixels":
      return "This photo is too large to process (over 50 megapixels).";
    case "unreadable_image":
      return "The image file is damaged or can't be read.";
    case "too_many_tiles":
      return "This photo's shape is too extreme to process.";
    case "object_too_large":
      return "The stored file is larger than expected.";
    case "worker_lost":
      return "Processing was interrupted. Upload the photo again.";
    case "storage_error":
      return "Photo storage had a problem. Upload the photo again.";
    default:
      return "This photo couldn't be processed.";
  }
}
