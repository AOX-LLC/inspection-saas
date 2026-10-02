"use client";

import { useCallback, useEffect, useEffectEvent, useRef, useState } from "react";
import { api } from "@/lib/api";
import { mergePhotos } from "@/lib/photos";
import type { Photo } from "@/lib/types";

// Photos per request. A batch is at most 100, so one page always reaches all of it.
const PAGE = 100;
// Thumbnail links last 15 minutes; a grid left open is refreshed well before that.
const RENEW_MS = 10 * 60 * 1000;
// A broken thumbnail asks for fresh links, but never more often than this.
const MIN_RENEW_GAP_MS = 10 * 1000;

interface State {
  key: string;
  photos: Photo[];
  next: string | null;
  failed: unknown;
}

export interface PhotosView {
  status: "loading" | "error" | "ready";
  photos: Photo[];
  hasMore: boolean;
  loadingMore: boolean;
  // True when refreshing failed but older photos are still shown.
  stale: boolean;
  error: unknown;
  // `renewLinks` takes fresh thumbnail links instead of keeping the ones on screen.
  refresh: (renewLinks?: boolean) => Promise<void>;
  loadMore: () => Promise<void>;
  // For an <img onError>: the link probably expired.
  renewLinks: () => void;
}

export function usePhotos(orgId: string, projectId: string): PhotosView {
  const key = `${orgId}/${projectId}`;
  const [state, setState] = useState<State | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const lastRenew = useRef(0);

  // Fetches the newest page and folds it into what is shown, so photos already on
  // screen are never lost and older pages that were opened stay open.
  const refresh = useCallback(async (renewLinks = false) => {
    try {
      const page = await api.listPhotos(orgId, projectId, PAGE);
      setState((previous) => {
        const same = previous?.key === key;
        return {
          key,
          photos: same ? mergePhotos(previous.photos, page.items, renewLinks) : page.items,
          next: same ? previous.next : page.next_cursor,
          failed: null,
        };
      });
    } catch (error) {
      setState((previous) =>
        previous?.key === key ? { ...previous, failed: error } : { key, photos: [], next: null, failed: error },
      );
    }
  }, [orgId, projectId, key]);

  const first = useEffectEvent(refresh);
  useEffect(() => {
    void first();
  }, [key, attempt]);

  const renew = useEffectEvent(() => {
    lastRenew.current = Date.now();
    return refresh(true);
  });
  useEffect(() => {
    const timer = setInterval(() => void renew(), RENEW_MS);
    return () => clearInterval(timer);
  }, [key]);

  const current = state?.key === key ? state : null;

  const loadMore = useCallback(async () => {
    if (!current?.next) return;
    setLoadingMore(true);
    try {
      const page = await api.listPhotos(orgId, projectId, PAGE, current.next);
      setState((previous) =>
        previous?.key === key
          ? { ...previous, photos: mergePhotos(previous.photos, page.items), next: page.next_cursor, failed: null }
          : previous,
      );
    } catch (error) {
      setState((previous) => (previous?.key === key ? { ...previous, failed: error } : previous));
    } finally {
      setLoadingMore(false);
    }
  }, [current?.next, orgId, projectId, key]);

  const renewLinks = useCallback(() => {
    if (Date.now() - lastRenew.current < MIN_RENEW_GAP_MS) return;
    lastRenew.current = Date.now();
    void refresh(true);
  }, [refresh]);

  if (!current) return view("loading", [], null, null, loadingMore, refresh, loadMore, renewLinks);
  if (current.failed && current.photos.length === 0) {
    // The retry for a failed first load is a new attempt, not a refresh of nothing.
    return view("error", [], null, current.failed, loadingMore, async () => setAttempt((n) => n + 1), loadMore, renewLinks);
  }
  return view("ready", current.photos, current.next, current.failed, loadingMore, refresh, loadMore, renewLinks);
}

function view(
  status: PhotosView["status"],
  photos: Photo[],
  next: string | null,
  failed: unknown,
  loadingMore: boolean,
  refresh: PhotosView["refresh"],
  loadMore: () => Promise<void>,
  renewLinks: () => void,
): PhotosView {
  return {
    status,
    photos,
    hasMore: next !== null,
    loadingMore,
    stale: status === "ready" && Boolean(failed),
    error: failed,
    refresh,
    loadMore,
    renewLinks,
  };
}
