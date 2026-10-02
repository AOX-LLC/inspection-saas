import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { checkFile, runPool } from "./uploader.ts";
import { formatBytes, formatWait, plural } from "./format.ts";
import { isUnfinished, mergePhotos } from "./photos.ts";
import type { Photo } from "./types.ts";

const MB = 1024 * 1024;

describe("checkFile", () => {
  it("accepts the three photo types", () => {
    for (const type of ["image/jpeg", "image/png", "image/webp"]) {
      assert.equal(checkFile({ name: "a", size: MB, type }), null);
    }
  });

  it("refuses other types in plain language", () => {
    assert.match(checkFile({ name: "a.gif", size: MB, type: "image/gif" }) ?? "", /This is a GIF file/);
    assert.match(checkFile({ name: "a.pdf", size: MB, type: "application/pdf" }) ?? "", /PDF/);
    assert.match(checkFile({ name: "a", size: MB, type: "" }) ?? "", /unknown file type/);
  });

  it("refuses an empty file and one over the limit, saying by how much", () => {
    assert.match(checkFile({ name: "a", size: 0, type: "image/png" }) ?? "", /empty/);
    assert.equal(checkFile({ name: "a", size: 50 * MB, type: "image/png" }), null);
    assert.match(checkFile({ name: "a", size: 51 * MB, type: "image/png" }) ?? "", /51 MB.*limit is 50 MB/);
  });
});

describe("runPool", () => {
  it("never runs more than the limit at once, and runs everything", async () => {
    let running = 0;
    let peak = 0;
    const done: number[] = [];
    await runPool([1, 2, 3, 4, 5, 6, 7], 3, async (n) => {
      running += 1;
      peak = Math.max(peak, running);
      await new Promise((resolve) => setTimeout(resolve, 5));
      running -= 1;
      done.push(n);
    });
    assert.equal(peak, 3);
    assert.deepEqual(done.sort(), [1, 2, 3, 4, 5, 6, 7]);
  });

  it("resolves at once for no items", async () => {
    await runPool([], 3, async () => assert.fail("no work expected"));
  });
});

function photo(id: string, created_at: string, status: Photo["status"] = "tiled"): Photo {
  return {
    id, file_id: `f-${id}`, original_filename: `${id}.jpg`, status, width: null, height: null,
    error: null, thumbnail_url: status === "tiled" ? `http://store/${id}` : null, created_at,
  };
}

describe("mergePhotos", () => {
  it("keeps newest first and takes the fresh state of a photo seen again", () => {
    const shown = [photo("b", "2026-01-02T00:00:00Z", "queued"), photo("a", "2026-01-01T00:00:00Z")];
    const merged = mergePhotos(shown, [photo("c", "2026-01-03T00:00:00Z", "queued"), photo("b", "2026-01-02T00:00:00Z")]);
    assert.deepEqual(merged.map((p) => p.id), ["c", "b", "a"]);
    assert.equal(merged.find((p) => p.id === "b")?.status, "tiled");
  });

  it("keeps a thumbnail link it already has, so the browser does not fetch the picture again", () => {
    const before = photo("a", "2026-01-01T00:00:00Z");
    const resigned = { ...before, thumbnail_url: "http://store/a?signature=new" };
    const [kept] = mergePhotos([before], [resigned]);
    assert.equal(kept?.thumbnail_url, "http://store/a");
  });

  it("gives a photo that just finished its first link", () => {
    const waiting = { ...photo("a", "2026-01-01T00:00:00Z", "queued") };
    const [done] = mergePhotos([waiting], [photo("a", "2026-01-01T00:00:00Z", "tiled")]);
    assert.equal(done?.status, "tiled");
    assert.equal(done?.thumbnail_url, "http://store/a");
  });

  it("keeps older pages that the fresh page does not reach", () => {
    const merged = mergePhotos([photo("old", "2025-01-01T00:00:00Z")], [photo("new", "2026-01-01T00:00:00Z")]);
    assert.deepEqual(merged.map((p) => p.id), ["new", "old"]);
  });

  it("orders photos made at the same instant by id, as the API does", () => {
    const merged = mergePhotos([], [photo("a", "2026-01-01T00:00:00Z"), photo("c", "2026-01-01T00:00:00Z"), photo("b", "2026-01-01T00:00:00Z")]);
    assert.deepEqual(merged.map((p) => p.id), ["c", "b", "a"]);
  });
});

describe("isUnfinished", () => {
  it("is true while the worker has not finished", () => {
    assert.equal(isUnfinished(photo("a", "2026-01-01T00:00:00Z", "queued")), true);
    assert.equal(isUnfinished(photo("a", "2026-01-01T00:00:00Z", "processing")), true);
    assert.equal(isUnfinished(photo("a", "2026-01-01T00:00:00Z", "tiled")), false);
    assert.equal(isUnfinished(photo("a", "2026-01-01T00:00:00Z", "failed")), false);
  });
});

describe("formatting", () => {
  it("formats sizes and counts", () => {
    assert.equal(formatBytes(0), "0 B");
    assert.equal(formatBytes(1536), "1.5 KB");
    assert.equal(formatBytes(50 * MB), "50 MB");
    assert.equal(plural(1, "photo"), "1 photo");
    assert.equal(plural(10, "photo"), "10 photos");
    assert.equal(formatWait(45), "45 seconds");
    assert.equal(formatWait(600), "10 minutes");
  });
});
