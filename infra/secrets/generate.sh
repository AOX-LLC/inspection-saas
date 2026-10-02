#!/bin/sh
# Writes random database credentials into the `secrets` volume on first run.
# The repo holds no secrets and `docker compose up` works without a .env file.
#
# Each consumer gets its own directory, readable only by its group:
#   db/     gid 999   postgres: superuser, owner and app passwords (for initdb)
#   app/    gid 10001 api: the app role's password
#   worker/ gid 10006 worker: the worker role's password
#   owner/  gid 10002 migrate and seed: the owner role's password
#   objectstore/ uid 10004 object store and its init: cluster RPC secret, admin token
#   storage/     gid 10005 api, seed, init, tests: the S3 key the API signs with
# Existing files are kept, so credentials survive restarts. `down -v` resets them.
set -eu
# Nothing this script creates is readable by others, even before lock_down.
umask 077

SECRETS_DIR=/secrets

new_password() {
  od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
}

write_once() {
  file=$1
  if [ ! -s "$file" ]; then
    new_password > "$file"
  fi
}

# Mirrors one file into a consumer directory. Both copies hold the same value.
copy_into() {
  source_file=$1
  target_file=$2
  if [ ! -s "$target_file" ]; then
    cp "$source_file" "$target_file"
  fi
}

# Garage access key ids are "GK" plus 24 hex characters.
write_once_key_id() {
  file=$1
  if [ ! -s "$file" ]; then
    printf 'GK%s' "$(od -An -tx1 -N12 /dev/urandom | tr -d ' \n')" > "$file"
  fi
}

lock_down() {
  dir=$1
  gid=$2
  chown -R "0:$gid" "$dir"
  chmod 0750 "$dir"
  chmod 0440 "$dir"/*
}

# Garage refuses secret files that any group or other can read, so its directory
# is owned by its own user instead of shared by group.
lock_down_to_user() {
  dir=$1
  uid=$2
  # Already handed over on an earlier run; root can no longer enter the directory.
  if [ "$(stat -c %u "$dir")" = "$uid" ]; then
    return
  fi
  chmod 0400 "$dir"/*
  chmod 0700 "$dir"
  chown -R "$uid:$uid" "$dir"
}

mkdir -p "$SECRETS_DIR/db" "$SECRETS_DIR/app" "$SECRETS_DIR/owner" "$SECRETS_DIR/worker" \
  "$SECRETS_DIR/objectstore" "$SECRETS_DIR/storage"
chmod 0755 "$SECRETS_DIR"

write_once "$SECRETS_DIR/db/postgres_password"
write_once "$SECRETS_DIR/db/owner_password"
write_once "$SECRETS_DIR/db/app_password"
write_once "$SECRETS_DIR/db/worker_password"
copy_into "$SECRETS_DIR/db/app_password" "$SECRETS_DIR/app/app_password"
copy_into "$SECRETS_DIR/db/owner_password" "$SECRETS_DIR/owner/owner_password"
copy_into "$SECRETS_DIR/db/worker_password" "$SECRETS_DIR/worker/worker_password"

# After the first run root can no longer enter the directory, so it is skipped.
if [ "$(stat -c %u "$SECRETS_DIR/objectstore")" != 10004 ]; then
  write_once "$SECRETS_DIR/objectstore/rpc_secret"
  write_once "$SECRETS_DIR/objectstore/admin_token"
fi
write_once_key_id "$SECRETS_DIR/storage/s3_access_key_id"
write_once "$SECRETS_DIR/storage/s3_secret_access_key"

lock_down "$SECRETS_DIR/db" 999
lock_down "$SECRETS_DIR/app" 10001
lock_down "$SECRETS_DIR/owner" 10002
lock_down "$SECRETS_DIR/worker" 10006
lock_down_to_user "$SECRETS_DIR/objectstore" 10004
lock_down "$SECRETS_DIR/storage" 10005

# The object store runs as gid/uid 10004 and writes its data here. A new named
# volume is root-owned, so hand it over once.
if [ "$(stat -c %u /objectdata)" != 10004 ]; then
  chmod 0700 /objectdata
  chown 10004:10004 /objectdata
fi

echo "secrets: ready"
