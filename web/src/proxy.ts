import { NextResponse, type NextRequest } from "next/server";
import { buildCsp, originOf } from "@/lib/csp";

// Runs on every page request. It gives each response its own nonce and the
// Content-Security-Policy that names it, so a page can only run scripts Next.js
// itself wrote. The object store's origin is read here, at request time, so one
// image works wherever the store is reachable.
export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const policy = buildCsp({
    nonce,
    storeOrigin: originOf(process.env.S3_PUBLIC_ORIGIN),
    development: process.env.NODE_ENV === "development",
  });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", policy);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: the API has its own headers, and static files need no policy.
      source: "/((?!api/|api$|_next/static|_next/image|icon.svg).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
