#!/bin/sh
# Restores a backup made by backup.sh into a NEW, empty database and checks that tenant isolation came back with it.
#
#   PGHOST=db PGUSER=postgres PGPASSWORD=... ./deploy/restore.sh /backups/booking-20261005T020000Z.dump booking_restored
#
# Needs a superuser (the dump re-creates the btree_gist / postgis extensions) and the roles from deploy/postgres/01_roles.sql to
# exist on the server already (the dump records who owns what). Restore into a fresh name first, look at it, then point the
# application at it (or rename databases) - never restore over the live one.
set -eu
dump="${1:?usage: restore.sh <dump file> <new database name>}"
target="${2:?usage: restore.sh <dump file> <new database name>}"

createdb "$target"
if ! pg_restore --exit-on-error --no-password --dbname "$target" "$dump"; then
  echo "restore failed; dropping the half-restored database $target" >&2
  dropdb "$target"
  exit 1
fi

# every table that carries tenant_id must still have Row-Level Security switched on AND forced
unprotected="$(psql --no-password --tuples-only --no-align --dbname "$target" -c "
  SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname NOT IN ('tenants', 'sites', 'api_keys')
    AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
    AND NOT (c.relrowsecurity AND c.relforcerowsecurity)")"
tables="$(psql --no-password --tuples-only --no-align --dbname "$target" -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
echo "restored $dump into $target: $tables tables, $unprotected without enforced row-level security"
[ "$unprotected" = "0" ] || { echo "ROW-LEVEL SECURITY IS MISSING AFTER THE RESTORE - do not use this database" >&2; exit 1; }
