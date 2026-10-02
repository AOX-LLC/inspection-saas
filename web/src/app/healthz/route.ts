import { NextResponse } from "next/server";
import pkg from "../../../package.json" with { type: "json" };

// Which build is running, for whoever is looking after it. Outside /api, so it answers
// for this service and not for the API behind it.
export const dynamic = "force-dynamic";

const startedAt = Date.now();

export function GET() {
  return NextResponse.json({
    status: "ok",
    // Set by whoever builds or deploys the image; unknown otherwise.
    commit: process.env.APP_COMMIT ?? null,
    commit_source: "process_start",
    branch: process.env.APP_BRANCH ?? null,
    version: pkg.version,
    schema_version: null,
    uptime_s: Math.round((Date.now() - startedAt) / 1000),
  });
}
