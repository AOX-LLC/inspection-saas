import type { ReactNode } from "react";
import { SessionGate } from "@/components/session";
import { Shell } from "@/components/shell";

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <SessionGate>
      <Shell>{children}</Shell>
    </SessionGate>
  );
}
