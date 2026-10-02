// The Content-Security-Policy, built per request.
//
// Images and uploads go to two places only: this origin and the object store's
// public origin. Scripts run only with the request's nonce, and nothing may frame
// the app. There are no inline styles or scripts of our own, so the nonce is for
// the ones Next.js itself writes.

export interface CspOptions {
  nonce: string;
  // The object store's public origin, as the browser reaches it. Without one the
  // policy allows this origin alone, and uploads and thumbnails fail visibly.
  storeOrigin: string | null;
  development?: boolean;
}

// Reduces a configured value to a bare origin, or null if it is not a usable one.
export function originOf(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.origin;
  } catch {
    return null;
  }
}

export function buildCsp({ nonce, storeOrigin, development = false }: CspOptions): string {
  const store = storeOrigin ? ` ${storeOrigin}` : "";
  const directives = [
    "default-src 'self'",
    // React reads source maps through eval in development only.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    `img-src 'self'${store}`,
    `connect-src 'self'${store}`,
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ];
  // Upgrading only makes sense over TLS; on plain http://127.0.0.1 it would break the store.
  if (storeOrigin?.startsWith("https:")) directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}
