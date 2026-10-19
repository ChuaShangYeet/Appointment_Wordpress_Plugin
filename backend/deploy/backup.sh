#!/bin/sh
# Backs the booking database up to a compressed, restorable archive and deletes archives older than KEEP_DAYS.
#
#   BACKUP_DIR=/backups PGHOST=db PGUSER=booking_backup PGPASSWORD=... PGDATABASE=booking ./deploy/backup.sh
#
# Use the booking_backup role (deploy/postgres/01_roles.sql). pg_dump refuses to run as any role the tenant policies apply to - which,
# because they are forced, includes the table owner - since it would silently dump only part of the data.
#
# Run it from cron or a scheduler (daily at least). The archive is checked before it is kept; a failed or partial dump never
# replaces a good one. What it does NOT contain, and must be backed up separately and kept safe:
#   * PDPA_ENCRYPTION_KEY - health notes are stored encrypted with it; without the key a restored backup cannot read them
#   * JWT_SECRET, PLATFORM_ADMIN_TOKEN, the WhatsApp / SMS / SMTP credentials
#   * Redis (it holds only short-lived codes and caches)
# Copy the archives off the machine (another region / provider) too: a backup on the same disk is not a backup.
set -eu
: "${BACKUP_DIR:?set BACKUP_DIR}"
: "${PGDATABASE:?set PGDATABASE}"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$BACKUP_DIR/booking-$stamp.dump"
partial="$file.partial"
trap 'rm -f "$partial"' EXIT

pg_dump --format=custom --no-password --file "$partial"
pg_restore --list "$partial" > /dev/null   # unreadable archive: stop here, keep the older ones
mv "$partial" "$file"
trap - EXIT

find "$BACKUP_DIR" -maxdepth 1 -name 'booking-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "$file"
