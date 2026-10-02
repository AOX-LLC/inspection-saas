// Where to send someone after signing in. Only a path on this site is allowed, so a
// crafted `?next=` link cannot bounce a person to another origin.
export function safeNext(value: string | null | undefined, fallback = "/orgs"): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) {
    return fallback;
  }
  // Control characters are how a browser can be talked into reading "/\t/evil.example".
  if (/[\u0000-\u001f\u007f]/.test(value)) return fallback;
  return value;
}
