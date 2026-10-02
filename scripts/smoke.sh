#!/usr/bin/env bash
# End-to-end check against a running stack (`docker compose up -d --wait`):
# log in, list projects, upload an image through a presigned POST, complete it,
# wait for the worker to tile it, fetch it back through a presigned GET, and
# confirm another org is a 404.
# Uses only the demo seed, which is synthetic.
set -euo pipefail

API="${API:-http://127.0.0.1:4701}"
ORIGIN="${ORIGIN:-http://127.0.0.1:4700}"
ALPHA_ORG="8e35f604-8a87-4fba-b70e-e87a8e22efd1"
BETA_ORG="d16b9455-89e1-4a14-8535-6f581ac38d7b"
BETA_PROJECT="b70e9fc6-b294-4ea6-abce-a3ba75ff66e6"
# The published demo password (see README); it protects nothing.
DEMO_PASSWORD="synthetic-demo-password"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
jar="$work/cookies"

fail() { echo "smoke: FAIL: $*" >&2; exit 1; }
step() { echo "smoke: $*"; }

api() { # api METHOD PATH [curl args...]; prints the body, fails on HTTP >= 400
  local method="$1" path="$2"
  shift 2
  curl --silent --show-error --fail-with-body --max-time 20 \
    -X "$method" -b "$jar" -c "$jar" -H "Origin: $ORIGIN" "$@" "$API$path"
}

status_of() { # status_of METHOD PATH: only the HTTP status code
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 20 \
    -X "$1" -b "$jar" -H "Origin: $ORIGIN" "$API$2"
}

# A 1x1 PNG.
printf 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==' \
  | base64 -d > "$work/upload.png"
size="$(stat -c %s "$work/upload.png")"

step "health"
[ "$(curl -fsS "$API/health")" = '{"status":"ok"}' ] || fail "/health is not exactly status-only"

step "unauthenticated request is refused"
[ "$(status_of GET "/orgs/$ALPHA_ORG/projects")" = 401 ] || fail "expected 401 without a session"

step "log in as a seeded Alpha inspector"
api POST /auth/login -H 'Content-Type: application/json' \
  -d "{\"email\":\"alpha.inspector@alpha.example\",\"password\":\"$DEMO_PASSWORD\"}" \
  | jq -e '.orgs[0].role == "inspector"' >/dev/null || fail "login did not return the inspector"

step "list projects"
project="$(api GET "/orgs/$ALPHA_ORG/projects" | jq -er '.items[0].id')"

step "start an upload"
upload="$(api POST "/orgs/$ALPHA_ORG/projects/$project/files" -H 'Content-Type: application/json' \
  -d "{\"filename\":\"smoke.png\",\"content_type\":\"image/png\",\"size_bytes\":$size}")"
file_id="$(jq -er '.file_id' <<<"$upload")"
post_url="$(jq -er '.upload.url' <<<"$upload")"

step "upload straight to the object store"
form=()
while IFS= read -r line; do form+=(-F "$line"); done \
  < <(jq -r '.upload.fields | to_entries[] | "\(.key)=\(.value)"' <<<"$upload")
# The file part must come last in a presigned POST.
curl --silent --show-error --fail-with-body --max-time 20 "${form[@]}" -F "file=@$work/upload.png" "$post_url" \
  || fail "object store rejected the upload"

step "complete the upload"
api POST "/orgs/$ALPHA_ORG/projects/$project/files/$file_id/complete" \
  | jq -e '.status == "ready"' >/dev/null || fail "file is not ready"

step "the worker tiles the photo, and the progress shows it"
progress_path="/orgs/$ALPHA_ORG/projects/$project/photos/progress"
tiled="$(api GET "$progress_path" | jq -r '.counts.tiled')"
for _ in $(seq 1 60); do
  progress="$(api GET "$progress_path")"
  if [ "$(jq -r '.finished' <<<"$progress")" = true ]; then break; fi
  sleep 1
done
[ "$(jq -r '.finished' <<<"$progress")" = true ] || fail "the worker did not finish: $progress"
[ "$(jq -r '.counts.failed' <<<"$progress")" = 0 ] || fail "a photo failed to tile: $progress"
[ "$(jq -r '.counts.tiled' <<<"$progress")" -ge 1 ] || fail "no photo was tiled: $progress"

step "download through a presigned GET and compare bytes"
download_url="$(api GET "/orgs/$ALPHA_ORG/projects/$project/files/$file_id/download" | jq -er '.url')"
curl --silent --show-error --fail --max-time 20 -o "$work/fetched.png" -D "$work/headers" "$download_url"
cmp "$work/upload.png" "$work/fetched.png" || fail "downloaded bytes differ"
grep -qi '^content-type: image/png' "$work/headers" || fail "content type not forced"
grep -qi '^content-disposition: attachment' "$work/headers" || fail "no content-disposition"

step "another org is a 404"
[ "$(status_of GET "/orgs/$BETA_ORG/projects")" = 404 ] || fail "Beta org list was not 404"
[ "$(status_of GET "/orgs/$BETA_ORG/projects/$BETA_PROJECT/files")" = 404 ] || fail "Beta files were not 404"

step "log out and confirm the session is revoked server-side"
# Saved first: curl drops the cookie on logout, so only a replay proves revocation.
token="$(awk '$6 == "session" {print $7}' "$jar")"
[ -n "$token" ] || fail "no session cookie to replay"
api POST /auth/logout >/dev/null
code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 20 \
  -H "Cookie: session=$token" "$API/orgs/$ALPHA_ORG/projects")"
[ "$code" = 401 ] || fail "old session token still works after logout ($code)"

echo "smoke: ok"
