#!/bin/sh
# Immich Album Sync — container entrypoint
# Runs before Python to ensure directories exist and print basic diagnostics.
set -e

echo "========================================"
echo " Immich Album Sync — Starting"
echo "========================================"
echo "  User     : $(id)"
echo "  Workdir  : $(pwd)"
echo "  Python   : $(python --version 2>&1)"
echo "  immich-go: $(immich-go --version 2>&1 | head -1 || echo 'not found')"
echo ""

# Pre-create all required data directories.
# These will also be created by init_db() but doing it here means
# any permission error is printed to the Docker log before Python starts.
echo "Creating appdata directories..."
mkdir -p /app/appdata/cache /app/appdata/logs

echo "Appdata contents:"
ls -la /app/appdata/ 2>&1 || echo "  (empty or not mounted)"
echo ""
echo "Starting uvicorn..."
echo "========================================"

exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --log-level info
