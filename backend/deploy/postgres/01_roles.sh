#!/bin/sh
# Runs once, when the postgres container creates its data directory (docker-entrypoint-initdb.d), as the superuser POSTGRES_USER.
# It only hands the passwords to 01_roles.sql, which holds the real work (and can be run by hand against any other server).
set -eu
: "${BOOKING_OWNER_PASSWORD:?set BOOKING_OWNER_PASSWORD}"
: "${BOOKING_APP_PASSWORD:?set BOOKING_APP_PASSWORD}"
: "${BOOKING_BACKUP_PASSWORD:?set BOOKING_BACKUP_PASSWORD}"
psql -v ON_ERROR_STOP=1 \
  -v owner_password="$BOOKING_OWNER_PASSWORD" -v app_password="$BOOKING_APP_PASSWORD" -v backup_password="$BOOKING_BACKUP_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /deploy/01_roles.sql
