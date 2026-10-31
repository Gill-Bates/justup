#!/usr/bin/env bash
#
# docker/entrypoint.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

set -e

# Ensure Python output is not lost on crashes/restarts
export PYTHONUNBUFFERED=1
export PYTHONFAULTHANDLER=1
export PYTHONIOENCODING=utf-8

# UTF-8 locale for proper emoji/unicode support in notifications
export LANG=${LANG:-en_US.UTF-8}
export LC_ALL=${LC_ALL:-en_US.UTF-8}

# Reduce risk of OpenBLAS threading issues on small/old hosts
export OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS:-1}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-1}

# Build JAVA_TOOL_OPTIONS from customizable env vars (signal-cli memory tuning)
export JAVA_TOOL_OPTIONS="-Dsignal-cli.home=${SIGNAL_DATA_DIR:-/app/data/signal} -Xms${JAVA_XMS:-64m} -Xmx${JAVA_XMX:-256m}"

# Read version from single source of truth (pyproject.toml)
VERSION=$(python -c "from app.utils.version import get_version; print(get_version())" 2>/dev/null || echo "dev")

echo "=============================================================="
echo "  justUp! v${VERSION}"
echo "  Lightweight Uptime Monitor — done right."
echo "=============================================================="
echo ""

# ------------------------------------------------------------------
# PUID/PGID Support (LinuxServer.io pattern)
# ------------------------------------------------------------------

PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "📋 Running with UID=$PUID, GID=$PGID"

if ! [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]]; then
    echo "Error: PUID and PGID must be numeric" >&2
    exit 1
fi

if [[ "$PUID" =~ ^0+$ || "$PGID" =~ ^0+$ ]]; then
    echo "Error: PUID and PGID must be non-zero" >&2
    exit 1
fi

if ! command -v gosu > /dev/null 2>&1; then
    echo "Error: gosu is required to drop privileges" >&2
    exit 1
fi

if [ "$(id -u)" != 0 ]; then
    echo "Error: entrypoint requires root for UID/GID setup" >&2
    exit 1
fi

if [ "$(id -g justup)" != "$PGID" ]; then
    groupmod -o -g "$PGID" justup
fi
if [ "$(id -u justup)" != "$PUID" ]; then
    usermod -o -u "$PUID" justup
fi

# ------------------------------------------------------------------
# Ensure data directories exist and have correct ownership
# ------------------------------------------------------------------

REQUIRED_DIRS=(/app/data /app/data/sqlite /app/data/tsdb /app/data/pdf_reports /app/data/signal /app/data/geoip)

# Hand the mount over first: with cap_drop ALL, root lacks DAC_OVERRIDE and cannot
# create entries inside a bind mount owned by another UID.
# A changed PUID/PGID must also remap existing files; avoid this scan on normal restarts.
if [ "$(stat -c '%u:%g' /app/data)" != "$PUID:$PGID" ]; then
    chown -R "$PUID:$PGID" /app/data
fi

# Create as the service user so the result does not depend on root's capabilities.
for dir in "${REQUIRED_DIRS[@]}"; do
    if [ ! -d "$dir" ]; then
        echo "📁 Creating directory: $dir"
        gosu justup mkdir -p "$dir"
    fi
done

for dir in "${REQUIRED_DIRS[@]}"; do
    if [ "$(stat -c '%u:%g' "$dir")" != "$PUID:$PGID" ]; then
        chown "$PUID:$PGID" "$dir"
    fi
done

# Ensure cache dirs are writable after UID/GID remap
for dir in /home/justup /home/justup/.matplotlib; do
    if [ -d "$dir" ]; then
        if [ "$(stat -c '%u:%g' "$dir")" != "$PUID:$PGID" ]; then
            chown "$PUID:$PGID" "$dir"
        fi
    fi
done

if ! gosu justup test -w /app/data; then
    echo "Error: /app/data is not writable by UID=$PUID, GID=$PGID" >&2
    exit 1
fi

# Check if database exists (informational only)
if [ ! -f /app/data/sqlite/justup.db ]; then
    echo "📦 First run detected – database will be initialized."
fi

echo ""
echo "🚀 Starting justUp! on ${JUSTUP_HOST:-0.0.0.0}:${JUSTUP_PORT:-8000}"
echo ""

# ------------------------------------------------------------------
# Drop privileges and exec
# ------------------------------------------------------------------

exec gosu justup "$@"
