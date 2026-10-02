import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ApiError } from "./api.ts";
import {
  describeLoadFailure,
  describeLoginFailure,
  describePhotoError,
  describeStoreFailure,
  describeUploadFailure,
} from "./messages.ts";

describe("describeUploadFailure", () => {
  it("explains a wrong type in plain language", () => {
    assert.match(describeUploadFailure(new ApiError(415, "Only JPEG, PNG and WebP images are accepted")), /JPEG, PNG and WebP/);
  });

  it("explains a file that is too large", () => {
    assert.match(describeUploadFailure(new ApiError(413, "File is too large")), /over the 50 MB limit/);
  });

  it("explains a rejection by the content check", () => {
    const message = describeUploadFailure(new ApiError(422, "File content does not match the declared type"));
    assert.match(message, /contents don't match its type/);
  });

  it("explains an unreachable server and a forbidden role", () => {
    assert.match(describeUploadFailure(new ApiError(0, "unreachable")), /Can't reach the server/);
    assert.match(describeUploadFailure(new ApiError(403, "Forbidden")), /role/);
  });

  it("never shows raw server text for a server error", () => {
    const message = describeUploadFailure(new ApiError(500, "Traceback: secret detail"));
    assert.doesNotMatch(message, /Traceback|secret/);
  });

  it("falls back for errors that are not API errors", () => {
    assert.match(describeUploadFailure(new Error("boom")), /upload failed/);
  });
});

describe("describeStoreFailure", () => {
  it("separates unreachable, refused and broken", () => {
    assert.match(describeStoreFailure(0), /reach the photo storage/);
    assert.match(describeStoreFailure(403), /refused/);
    assert.match(describeStoreFailure(500), /problem/);
  });
});

describe("describePhotoError", () => {
  it("knows each code the worker records", () => {
    for (const code of ["too_many_pixels", "unreadable_image", "too_many_tiles", "object_too_large", "worker_lost", "storage_error"]) {
      assert.notEqual(describePhotoError(code), describePhotoError(null), code);
    }
  });

  it("has a generic message for a code it does not know", () => {
    assert.equal(describePhotoError("something_new"), describePhotoError(null));
  });
});

describe("describeLoginFailure", () => {
  it("does not say which of email or password was wrong", () => {
    assert.match(describeLoginFailure(new ApiError(401, "Invalid email or password")), /don't match an account/);
  });

  it("says how long to wait when throttled", () => {
    assert.match(describeLoginFailure(new ApiError(429, "x", 600)), /10 minutes/);
    assert.match(describeLoginFailure(new ApiError(429, "x")), /Try again later/);
  });
});

describe("describeLoadFailure", () => {
  it("does not reveal whether a missing thing exists or is just not yours", () => {
    assert.match(describeLoadFailure(new ApiError(404, "Not found"), "project"), /doesn't exist, or you don't have access/);
  });
});

import { safeNext } from "./redirect.ts";

describe("safeNext", () => {
  it("keeps a path on this site", () => {
    assert.equal(safeNext("/orgs/1/projects/2"), "/orgs/1/projects/2");
    assert.equal(safeNext("/orgs?x=1"), "/orgs?x=1");
  });

  it("refuses anything that could leave the site", () => {
    for (const bad of ["//evil.example", "https://evil.example", "/\\evil.example", "/\t/evil.example", "javascript:alert(1)", "evil", "", null, undefined]) {
      assert.equal(safeNext(bad), "/orgs", String(bad));
    }
  });
});
