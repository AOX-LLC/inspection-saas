#!/bin/bash
# The superuser may only connect over the local socket, as the postgres OS
# user (`docker compose exec -u postgres db psql`). Over TCP it is rejected,
# so the published port only ever admits the owner and app roles.
set -euo pipefail

sed -i '1i host all postgres all reject' "$PGDATA/pg_hba.conf"
