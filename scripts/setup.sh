#!/bin/sh
set -eu
umask 077
export LC_ALL=C

if [ "$#" -ne 1 ]; then
    printf 'Usage: sh scripts/setup.sh <OBS-reachable-hostname-or-IPv4>\n' >&2
    exit 1
fi

host=$1
case "$host" in
    ''|*[!A-Za-z0-9.-]*|.*|*.|*..*|-*|*-|*.-*|*-.*)
        printf 'Invalid host: use a hostname or IPv4 address, without scheme, port, path, or credentials.\n' >&2
        exit 1
        ;;
esac
if [ "${#host}" -gt 253 ]; then
    printf 'Invalid host: maximum length is 253 characters.\n' >&2
    exit 1
fi
old_ifs=$IFS
IFS=.
set -- $host
IFS=$old_ifs
for label do
    if [ "${#label}" -gt 63 ]; then
        printf 'Invalid host: each hostname label must be at most 63 characters.\n' >&2
        exit 1
    fi
done

if ! command -v openssl >/dev/null 2>&1; then
    printf 'OpenSSL is required. Install it with your OS package manager and rerun.\n' >&2
    exit 1
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -f "$root/compose.yaml" ] || [ ! -f "$root/.env.example" ]; then
    printf 'Run the setup script from an intact KUNAS/Labs checkout.\n' >&2
    exit 1
fi
if [ -e "$root/.env" ] || [ -L "$root/.env" ]; then
    printf '.env already exists; refusing to replace credentials.\n' >&2
    exit 1
fi

admin_password=$(openssl rand -hex 24)
internal_token=$(openssl rand -hex 32)
if [ "${#admin_password}" -ne 48 ] || [ "${#internal_token}" -ne 64 ]; then
    printf 'OpenSSL did not generate the expected secret lengths.\n' >&2
    exit 1
fi

# Noclobber also protects an existing file created after the check above.
(
    set -C
    {
        printf 'ADMIN_PASSWORD=%s\nINTERNAL_TOKEN=%s\n' "$admin_password" "$internal_token"
        printf 'PUBLIC_HOST=%s\n' "$host"
        printf '%s\n' \
            'RTMP_PORT=1935' 'WEB_PORT=8080' \
            'WEB_BIND=127.0.0.1' 'RTMP_BIND=127.0.0.1' \
            'COOKIE_SECURE=false' 'AUTO_RECORD=true' 'MIN_FREE_GB=2' 'FACE_RETENTION_DAYS=7' \
            'MAX_FACES=2000' 'MATCH_THRESHOLD=0.5' \
            'DETECTION_THRESHOLD=0.85' 'ANALYSIS_FPS=2'
    } > "$root/.env"
)
printf '%s\n' 'Created .env with owner-only permissions. Secrets were not printed.' \
    'Review bind addresses and COOKIE_SECURE before starting Docker Compose.' \
    'The hostname is advertised to OBS; it does not change the loopback-only bindings.'
