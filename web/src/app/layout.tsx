import type { Metadata } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Inspection", template: "%s · Inspection" },
  description: "Upload site photos and review them.",
  robots: { index: false },
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Every page renders per request, because the Content-Security-Policy carries a
  // fresh nonce for each one (see src/proxy.ts).
  await connection();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
