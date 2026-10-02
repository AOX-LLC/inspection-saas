import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { buildCsp, originOf } from "./csp.ts";

const STORE = "http://127.0.0.1:4703";

function directive(policy: string, name: string): string[] {
  const found = policy.split("; ").find((d) => d.startsWith(`${name} `) || d === name);
  assert.ok(found, `${name} is missing from ${policy}`);
  return found.split(" ").slice(1);
}

describe("buildCsp", () => {
  const policy = buildCsp({ nonce: "abc", storeOrigin: STORE });

  it("allows images only from this origin and the object store", () => {
    assert.deepEqual(directive(policy, "img-src"), ["'self'", STORE]);
  });

  it("allows uploads only to this origin and the object store", () => {
    assert.deepEqual(directive(policy, "connect-src"), ["'self'", STORE]);
  });

  it("forbids framing, plugins and a rewritten base", () => {
    assert.deepEqual(directive(policy, "frame-ancestors"), ["'none'"]);
    assert.deepEqual(directive(policy, "object-src"), ["'none'"]);
    assert.deepEqual(directive(policy, "base-uri"), ["'self'"]);
    assert.deepEqual(directive(policy, "form-action"), ["'self'"]);
  });

  it("runs scripts only with the nonce, never inline or eval in production", () => {
    const scripts = directive(policy, "script-src");
    assert.ok(scripts.includes("'nonce-abc'"));
    assert.ok(!scripts.includes("'unsafe-inline'"));
    assert.ok(!scripts.includes("'unsafe-eval'"));
  });

  it("allows no inline styles other than the nonced ones", () => {
    assert.ok(!directive(policy, "style-src").includes("'unsafe-inline'"));
  });

  it("has no wildcard, data or blob source anywhere", () => {
    assert.doesNotMatch(policy, /(^| )\*( |;|$)|data:|blob:/);
  });

  it("falls back to this origin alone when no store origin is configured", () => {
    const alone = buildCsp({ nonce: "abc", storeOrigin: null });
    assert.deepEqual(directive(alone, "img-src"), ["'self'"]);
    assert.deepEqual(directive(alone, "connect-src"), ["'self'"]);
  });

  it("adds eval only in development", () => {
    const dev = buildCsp({ nonce: "abc", storeOrigin: STORE, development: true });
    assert.ok(directive(dev, "script-src").includes("'unsafe-eval'"));
  });

  it("upgrades insecure requests only when the store is on https", () => {
    assert.doesNotMatch(policy, /upgrade-insecure-requests/);
    assert.match(
      buildCsp({ nonce: "abc", storeOrigin: "https://files.example" }),
      /upgrade-insecure-requests/,
    );
  });
});

describe("originOf", () => {
  it("keeps only the origin of a configured URL", () => {
    assert.equal(originOf("http://127.0.0.1:4703/some/path?x=1"), STORE);
  });

  it("refuses anything that is not an http(s) origin", () => {
    for (const bad of [undefined, "", "not a url", "javascript:alert(1)", "data:text/plain,x", "ftp://x"]) {
      assert.equal(originOf(bad), null);
    }
  });

  it("cannot be turned into a policy injection", () => {
    // A value with a semicolon would otherwise smuggle in a directive.
    assert.equal(originOf("http://a.example; script-src *"), null);
  });
});
