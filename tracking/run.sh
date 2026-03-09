#!/usr/bin/env bash
#
# tracking/run.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# JustUp Tracking Server Launcher

set -e

cd "$(dirname "$0")"

# Create venv if missing
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Created virtual environment"
fi

source venv/bin/activate

pip install -q -r requirements.txt

# Default settings
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-9000}"

# Enable dev mode for local runs (uses default credentials)
export TRACKING_DEV_MODE="${TRACKING_DEV_MODE:-true}"

echo "Starting tracking server on ${HOST}:${PORT}"
echo "Dashboard: http://${HOST}:${PORT}/admin"
echo ""
echo "DEV MODE: ${TRACKING_DEV_MODE}"
echo "  Default user: admin"
echo "  Default pass: devpass123"
echo ""

uvicorn main:app --host "$HOST" --port "$PORT" --reload
