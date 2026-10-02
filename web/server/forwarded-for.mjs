// Gives the API the address this server saw.
//
// Next.js forwards /api/* to the API but passes `X-Forwarded-For` through exactly as
// the client sent it, so a client could name any address it liked. The API believes
// this server's header, so this server must not pass a claim through unchecked: it
// appends the address of the socket the request arrived on. Whatever the client wrote
// stays to the left, where the API never looks; it reads from the right and counts
// only the proxies it is told to trust (TRUSTED_PROXIES).
//
// Behind another proxy, that proxy's address is the rightmost entry; list it in the
// API's TRUSTED_PROXIES and the API reads the client address that proxy appended.

// An IPv4 peer on a dual-stack socket arrives as "::ffff:172.20.0.1".
export function normalizePeer(address) {
  if (typeof address !== "string" || address === "") return null;
  return address.startsWith("::ffff:") && address.includes(".") ? address.slice(7) : address;
}

// The header value to forward: what arrived, then the peer. With no peer address
// there is nothing trustworthy to add, and an unchecked claim is dropped.
export function forwardedFor(inbound, peer) {
  const address = normalizePeer(peer);
  if (address === null) return null;
  const earlier = Array.isArray(inbound) ? inbound.join(", ") : inbound;
  return earlier ? `${earlier}, ${address}` : address;
}

// Rewrites the header of every request an http.Server receives, before any handler
// (Next.js's included) sees it.
export function install(http) {
  const emit = http.Server.prototype.emit;
  http.Server.prototype.emit = function patchedEmit(event, request, ...rest) {
    if (event === "request" && request?.headers) {
      const value = forwardedFor(request.headers["x-forwarded-for"], request.socket?.remoteAddress);
      if (value === null) delete request.headers["x-forwarded-for"];
      else request.headers["x-forwarded-for"] = value;
    }
    return emit.call(this, event, request, ...rest);
  };
}
