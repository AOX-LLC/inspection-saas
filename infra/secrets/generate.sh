#!/bin/sh
# Writes random database credentials into the `secrets` volume on first run.
# The repo holds no secrets and `docker compose up` works without a .env file.
#
# Each consumer gets its own directory, readable only by its group:
#   db/     gid 999   postgres: superuser, owner and app passwords (for initdb)
#   app/    gid 10001 api: the app role's password
#   owner/  gid 10002 migrate and seed: the owner role's password
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

lock_down() {
  dir=$1
  gid=$2
  chown -R "0:$gid" "$dir"
  chmod 0750 "$dir"
  chmod 0440 "$dir"/*
}

mkdir -p "$SECRETS_DIR/db" "$SECRETS_DIR/app" "$SECRETS_DIR/owner"
chmod 0755 "$SECRETS_DIR"

write_once "$SECRETS_DIR/db/postgres_password"
write_once "$SECRETS_DIR/db/owner_password"
write_once "$SECRETS_DIR/db/app_password"
copy_into "$SECRETS_DIR/db/app_password" "$SECRETS_DIR/app/app_password"
copy_into "$SECRETS_DIR/db/owner_password" "$SECRETS_DIR/owner/owner_password"

lock_down "$SECRETS_DIR/db" 999
lock_down "$SECRETS_DIR/app" 10001
lock_down "$SECRETS_DIR/owner" 10002

echo "secrets: ready"
