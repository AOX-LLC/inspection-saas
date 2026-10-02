"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";

export type Loaded<T> =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; data: T };

// Loads once for each `key` and again when `reload` is called. The result is
// stored with the key it answers, so a stale answer for a previous key shows as
// "loading" instead of flashing the wrong data.
export function useLoad<T>(load: () => Promise<T>, key: string): Loaded<T> & { reload: () => void } {
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState<{ id: string; data?: T; error?: unknown } | null>(null);
  const id = `${key}#${attempt}`;
  const run = useEffectEvent(load);

  useEffect(() => {
    let current = true;
    run().then(
      (data) => current && setSettled({ id, data }),
      (error: unknown) => current && setSettled({ id, error }),
    );
    return () => {
      current = false;
    };
  }, [id]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  if (settled?.id !== id) return { status: "loading", reload };
  if (settled.error !== undefined) return { status: "error", error: settled.error, reload };
  return { status: "ready", data: settled.data as T, reload };
}
