import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { describe, it } from "node:test";
import { forwardedFor, install, normalizePeer } from "./forwarded-for.mjs";

describe("forwardedFor", () => {
  it("is the peer alone when the client sent no header", () => {
    assert.equal(forwardedFor(undefined, "172.20.0.1"), "172.20.0.1");
  });

  it("appends the peer after whatever arrived, so a forged address stays on the left", () => {
    assert.equal(forwardedFor("6.6.6.6", "172.20.0.1"), "6.6.6.6, 172.20.0.1");
    assert.equal(forwardedFor("6.6.6.6, 7.7.7.7", "172.20.0.1"), "6.6.6.6, 7.7.7.7, 172.20.0.1");
  });

  it("joins repeated header lines", () => {
    assert.equal(forwardedFor(["1.1.1.1", "2.2.2.2"], "172.20.0.1"), "1.1.1.1, 2.2.2.2, 172.20.0.1");
  });

  it("drops a claim it cannot vouch for when the peer is unknown", () => {
    assert.equal(forwardedFor("6.6.6.6", undefined), null);
    assert.equal(forwardedFor("6.6.6.6", ""), null);
  });

  it("writes an IPv4 peer on a dual-stack socket as plain IPv4", () => {
    assert.equal(normalizePeer("::ffff:172.20.0.1"), "172.20.0.1");
    assert.equal(normalizePeer("2001:db8::7"), "2001:db8::7");
    assert.equal(normalizePeer("::1"), "::1");
  });
});

describe("install", () => {
  // A stand-in for http: a Server whose emit is observable.
  function fakeHttp() {
    class Server extends EventEmitter {}
    return { Server };
  }

  function send(server, headers, remoteAddress) {
    const request = { headers, socket: { remoteAddress } };
    let seen;
    server.on("request", (incoming) => {
      seen = incoming.headers["x-forwarded-for"];
    });
    server.emit("request", request, {});
    return seen;
  }

  it("rewrites the header before a handler sees it", () => {
    const http = fakeHttp();
    install(http);
    assert.equal(send(new http.Server(), { "x-forwarded-for": "6.6.6.6" }, "::ffff:172.20.0.1"), "6.6.6.6, 172.20.0.1");
  });

  it("removes an unchecked claim when the peer is unknown", () => {
    const http = fakeHttp();
    install(http);
    assert.equal(send(new http.Server(), { "x-forwarded-for": "6.6.6.6" }, undefined), undefined);
  });

  it("rewrites the header of an upgrade request too", () => {
    const http = fakeHttp();
    install(http);
    const server = new http.Server();
    let seen;
    server.on("upgrade", (incoming) => {
      seen = incoming.headers["x-forwarded-for"];
    });
    server.emit("upgrade", { headers: { "x-forwarded-for": "6.6.6.6" }, socket: { remoteAddress: "172.20.0.1" } }, {}, Buffer.alloc(0));
    assert.equal(seen, "6.6.6.6, 172.20.0.1");
  });

  it("leaves every other event alone", () => {
    const http = fakeHttp();
    install(http);
    const server = new http.Server();
    let heard = false;
    server.on("connection", () => {
      heard = true;
    });
    server.emit("connection", {});
    assert.equal(heard, true);
  });
});
