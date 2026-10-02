"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { BrandMark } from "./icons";
import { useMe } from "./session";

export function Shell({ children }: { children: ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const [leaving, setLeaving] = useState(false);

  async function signOut() {
    setLeaving(true);
    try {
      await api.logout();
    } finally {
      // Signed out as far as this browser is concerned, whatever the server said.
      router.replace("/login");
    }
  }

  return (
    <>
      <header className="app-header">
        <div className="app-header-inner">
          <Link href="/orgs" className="brand">
            <BrandMark />
            Inspection
          </Link>
          <div className="header-user">
            <span className="header-name" title={me.email}>
              {me.display_name}
            </span>
            <button type="button" className="btn btn-small" onClick={signOut} disabled={leaving}>
              {leaving ? "Signing out…" : "Sign out"}
            </button>
          </div>
        </div>
      </header>
      <main className="container">{children}</main>
    </>
  );
}
