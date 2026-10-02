"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { api, ApiError } from "@/lib/api";
import { describeLoadFailure } from "@/lib/messages";
import type { Me } from "@/lib/types";
import { RetryAlert } from "./ui";
import { useLoad } from "./use-load";

const SessionContext = createContext<Me | null>(null);

export function useMe(): Me {
  const me = useContext(SessionContext);
  if (!me) throw new Error("useMe is used outside a signed-in page");
  return me;
}

// Renders its children only for a signed-in person. Whether someone is signed in is
// the API's answer to /auth/me; this component only reacts to it.
export function SessionGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const session = useLoad(api.me, "me");
  const signedOut = session.status === "error" && session.error instanceof ApiError && session.error.status === 401;

  useEffect(() => {
    if (signedOut) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [signedOut, router, pathname]);

  if (session.status === "ready") {
    return <SessionContext.Provider value={session.data}>{children}</SessionContext.Provider>;
  }
  if (session.status === "error" && !signedOut) {
    return (
      <div className="container">
        <RetryAlert message={describeLoadFailure(session.error, "session")} onRetry={session.reload} />
      </div>
    );
  }
  return (
    <div className="container" aria-busy="true">
      <p className="visually-hidden" role="status">
        Checking your session
      </p>
      <div className="skeleton skeleton-line" />
    </div>
  );
}
