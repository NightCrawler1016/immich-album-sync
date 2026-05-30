#!/bin/sh
# Immich Album Sync — container entrypoint
# Runs as root only to prepare directories, then drops to an unprivileged
# user (PUID:PGID) for the actual application process.
set -e

# Runtime user/group. Defaults match Unraid's "nobody:users" (99:100) so that
# existing host-mounted appdata — typically owned 99:100 on Unraid — stays
# writable with no extra configuration. Override via the PUID/PGID env vars.
PUID=${PUID:-99}
PGID=${PGID:-100}

echo "========================================"
echo " Immich Album Sync — Starting"
echo "========================================"
echo "  Boot user : $(id)"
echo "  Run as    : ${PUID}:${PGID} (PUID:PGID)"
echo "  Workdir   : $(pwd)"
echo "  Python    : $(python --version 2>&1)"
echo "  immich-go : $(immich-go --version 2>&1 | head -1 || echo 'not found')"
echo ""

# Pre-create all required data directories.
# These will also be created by init_db() but doing it here means
# any permission error is printed to the Docker log before Python starts.
echo "Creating appdata directories..."
mkdir -p /app/appdata/cache /app/appdata/logs

# Hand ownership of the writable paths to the runtime user. Best-effort:
# network shares (SMB/CIFS) enforce their own ownership via mount options and
# will reject chown — that is expected and not fatal.
echo "Setting ownership of /app/appdata to ${PUID}:${PGID}..."
chown -R "${PUID}:${PGID}" /app/appdata 2>/dev/null \
    || echo "  (could not chown /app/appdata — continuing; check share permissions if writes fail)"

echo "Appdata contents:"
ls -la /app/appdata/ 2>&1 || echo "  (empty or not mounted)"
echo ""
echo "Starting uvicorn as ${PUID}:${PGID}..."
echo "========================================"

# Drop root and run the app as the unprivileged user.
exec gosu "${PUID}:${PGID}" python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --log-level info
