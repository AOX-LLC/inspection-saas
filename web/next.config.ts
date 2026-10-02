import type { NextConfig } from "next";

// Where the API is, as the web container reaches it. Read at build time: Next
// bakes rewrites into the build, so the Dockerfile passes it as a build argument.
const apiOrigin = process.env.API_ORIGIN ?? "http://127.0.0.1:4701";

// Headers that never vary by request. The Content-Security-Policy does (it carries
// a per-request nonce and the object store's origin), so src/proxy.ts sets it.
const staticSecurityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
];

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  // Thumbnails come straight from the object store; nothing here resizes images.
  images: { unoptimized: true },
  async rewrites() {
    // The browser only ever talks to this origin, so the session cookie stays
    // same-origin. The API stays the one authority; this tier adds no logic.
    // Only the API's real routes. Its interactive docs (/docs, /openapi.json) stay on its
    // own port: served here they would load third-party script on the origin the API trusts.
    return ["auth/:path*", "orgs/:path*", "health"].map((path) => ({
      source: `/api/${path}`,
      destination: `${apiOrigin}/${path}`,
    }));
  },
  async headers() {
    return [{ source: "/:path*", headers: staticSecurityHeaders }];
  },
};

export default config;
