#!/usr/bin/env bash
#
# docker/docker-entrypoint.sh
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

# Read version from single source of truth
VERSION=$(cat /app/VERSION 2>/dev/null || echo "dev")

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

# Update justup user/group to match PUID/PGID
if [ "$(id -u justup 2>/dev/null)" != "$PUID" ]; then
    echo "🔧 Adjusting user 'justup' to UID=$PUID, GID=$PGID"
    
    # Modify group first
    if getent group justup > /dev/null 2>&1; then
        groupmod -o -g "$PGID" justup 2>/dev/null || true
    else
        groupadd -o -g "$PGID" justup 2>/dev/null || true
    fi
    
    # Modify user
    if getent passwd justup > /dev/null 2>&1; then
        usermod -o -u "$PUID" -g "$PGID" justup 2>/dev/null || true
    else
        useradd -o -u "$PUID" -g "$PGID" -s /bin/sh justup 2>/dev/null || true
    fi
fi

# ------------------------------------------------------------------
# Ensure data directories exist and have correct ownership
# ------------------------------------------------------------------

REQUIRED_DIRS="/app/data /app/data/sqlite /app/data/tsdb /app/data/signal"

for dir in $REQUIRED_DIRS; do
    if [ ! -d "$dir" ]; then
        echo "📁 Creating directory: $dir"
        mkdir -p "$dir"
    fi
done

# Seed default files from /opt/defaults/ if not present in data volume
# This handles the case where /app/data is a mounted volume hiding baked-in files
GEOIP_TARGET="${JUSTUP_DATA_DIR:-/app/data}/GeoLite2-City.mmdb"
if [ ! -f "$GEOIP_TARGET" ] && [ -f /opt/defaults/GeoLite2-City.mmdb ]; then
    echo "📦 Seeding GeoLite2-City.mmdb to $GEOIP_TARGET"
    cp /opt/defaults/GeoLite2-City.mmdb "$GEOIP_TARGET"
fi

# Fix ownership
echo "🔐 Setting ownership on /app/data"
chown -R "$PUID:$PGID" /app/data 2>/dev/null || true

# Ensure cache dirs are writable after UID/GID remap
for dir in /home/justup /home/justup/.matplotlib /app/.cartopy; do
    if [ -d "$dir" ]; then
        chown -R "$PUID:$PGID" "$dir" 2>/dev/null || true
    fi
done

# Verify write access (consistent with exec order: gosu > su-exec)
if command -v gosu > /dev/null 2>&1; then
    gosu justup test -w /app/data 2>/dev/null || echo "⚠️  Warning: /app/data may not be writable by user $PUID"
elif command -v su-exec > /dev/null 2>&1; then
    su-exec justup test -w /app/data 2>/dev/null || echo "⚠️  Warning: /app/data may not be writable by user $PUID"
fi

# Check if database exists (informational only)
if [ ! -f /app/data/sqlite/app.sqlite3 ]; then
    echo "📦 First run detected – database will be initialized."
fi

echo ""
echo "🚀 Starting justUp! on ${JUSTUP_HOST:-0.0.0.0}:${JUSTUP_PORT:-8080}"
echo "   Command: $@"
echo ""

# ------------------------------------------------------------------
# Drop privileges and exec
# ------------------------------------------------------------------

# Try gosu first (preferred), then su-exec, then exec directly
if command -v gosu > /dev/null 2>&1; then
    exec gosu justup "$@"
elif command -v su-exec > /dev/null 2>&1; then
    exec su-exec justup "$@"
else
    # Fallback: run as current user (root) - not ideal but works
    echo "⚠️  Warning: gosu/su-exec not found, running as root"
    exec "$@"
fi
