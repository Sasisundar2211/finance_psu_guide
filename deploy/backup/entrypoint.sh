#!/bin/sh
# Backup service entrypoint (DEPLOYMENT.md §5, DECISIONS.md D11.3, REQ-OPS-02).
#
#   backup                    dump -> encrypt -> upload to R2 -> clean up
#   restore <r2-object-key>   download -> decrypt -> pg_restore into an EMPTY database
#
# Secrets policy: DB_PASSWORD and BACKUP_AES_PASSPHRASE are read from the
# container environment only. They are never printed, never written to a file,
# and never placed on a command line (no `set -x`, OpenSSL reads the passphrase
# via `-pass env:`).
set -eu
umask 077

PREFIX="backups/postgres/"
R2_HELPER="/opt/backup/r2_transfer.py"

die() {
    echo "backup-service: $*" >&2
    exit 1
}

usage() {
    echo "usage: entrypoint.sh backup" >&2
    echo "       entrypoint.sh restore <r2-object-key>" >&2
    exit 2
}

# Names only are ever reported, never values.
require_env() {
    for name in "$@"; do
        eval "value=\${$name:-}"
        [ -n "$value" ] || die "required environment variable $name is not set"
    done
}

[ "$#" -ge 1 ] || usage
command="$1"
shift

require_env DB_NAME DB_USER DB_PASSWORD DB_HOST \
    R2_BUCKET_NAME R2_ENDPOINT_URL R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY \
    BACKUP_AES_PASSPHRASE
DB_PORT="${DB_PORT:-5432}"

# libpq does not read DB_PASSWORD; PGPASSWORD is scoped to this ephemeral container.
export PGPASSWORD="$DB_PASSWORD"
export PGCONNECT_TIMEOUT=15

# Every temp file lives in one private directory that is removed on success,
# failure, and termination signals. The exit status of the failing step is kept.
WORKDIR="$(mktemp -d /tmp/psu-backup.XXXXXX)"
cleanup() {
    rm -rf "$WORKDIR"
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP

DUMP="$WORKDIR/dump.custom"
ENCRYPTED="$WORKDIR/dump.custom.enc"

do_backup() {
    [ "$#" -eq 0 ] || usage

    # UTC timestamp plus 8 random hex chars: sortable, collision-safe, and
    # distinct from chapter PDF keys (chapters/<id>.pdf) by prefix.
    safe_name="$(printf '%s' "$DB_NAME" | tr -c 'A-Za-z0-9_-' '_')"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    suffix="$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    key="${PREFIX}${safe_name}-${stamp}-${suffix}.dump.enc"

    echo "backup-service: dumping database (custom format)"
    pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        --format=custom --no-owner --no-acl \
        -f "$DUMP" || die "pg_dump failed"
    pg_restore --list "$DUMP" >/dev/null || die "dump is not a readable archive"

    echo "backup-service: encrypting (AES-256-CBC, PBKDF2)"
    openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_AES_PASSPHRASE \
        -in "$DUMP" -out "$ENCRYPTED" || die "encryption failed"
    # Only the encrypted artifact is ever uploaded; drop the plaintext now.
    rm -f "$DUMP"
    [ -s "$ENCRYPTED" ] || die "encrypted artifact is empty"

    echo "backup-service: uploading to R2"
    python "$R2_HELPER" upload "$ENCRYPTED" "$key" || die "upload failed"

    rm -f "$ENCRYPTED"
    echo "backup-service: uploaded $key"
}

validate_key() {
    case "$1" in
        "$PREFIX"*) ;;
        *) die "object key must start with $PREFIX" ;;
    esac
    case "$1" in
        *[!A-Za-z0-9._/-]*) die "object key contains unsupported characters" ;;
    esac
    case "$1" in
        *..*) die "object key must not contain '..'" ;;
    esac
    case "$1" in
        *.dump.enc) ;;
        *) die "object key must end with .dump.enc" ;;
    esac
}

# Restore never drops or overwrites live data: any relation or function outside
# the system schemas means the target is not fresh, and the restore is refused.
assert_target_empty() {
    objects="$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        -X -A -t -v ON_ERROR_STOP=1 -c "
        SELECT
          (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%')
          +
          (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema'))
        ")" || die "cannot query the target database (does it exist and is DB_NAME correct?)"
    case "$objects" in
        '' | *[!0-9]*) die "unexpected emptiness-check result" ;;
    esac
    [ "$objects" -eq 0 ] ||
        die "target database '$DB_NAME' is not empty ($objects objects); refusing to restore. Restore only into a fresh, empty database."
}

do_restore() {
    [ "$#" -eq 1 ] || die "restore requires exactly one argument: <r2-object-key>"
    key="$1"
    [ -n "$key" ] || die "restore requires exactly one argument: <r2-object-key>"
    validate_key "$key"

    echo "backup-service: checking that target database is empty"
    assert_target_empty

    echo "backup-service: downloading $key"
    python "$R2_HELPER" download "$key" "$ENCRYPTED" || die "download failed"

    echo "backup-service: decrypting"
    openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_AES_PASSPHRASE \
        -in "$ENCRYPTED" -out "$DUMP" ||
        die "decryption failed (wrong passphrase or corrupt artifact)"
    rm -f "$ENCRYPTED"
    pg_restore --list "$DUMP" >/dev/null || die "decrypted file is not a readable archive"

    echo "backup-service: restoring into $DB_NAME"
    pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        --no-owner --no-acl --single-transaction \
        "$DUMP" || die "pg_restore failed (single transaction rolled back)"
    echo "backup-service: restore complete"
}

case "$command" in
    backup) do_backup "$@" ;;
    restore) do_restore "$@" ;;
    *) usage ;;
esac
