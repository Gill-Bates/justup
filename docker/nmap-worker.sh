#!/usr/bin/env bash
#
# docker/nmap-worker.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
# Runs the privileged Nmap worker (the only process holding CAP_NET_RAW) next to the web
# process and keeps it available: readiness handshake on every start, bounded restarts with
# exponential backoff, and a status file the web process can read. A worker failure never
# stops the web server; scans then report "unavailable" with the reason from the status file.
# Started by docker/entrypoint.sh as root, in the background, before it execs the web process.

set -u

WORKER_UID_NAME=justup-nmap
STATUS_FILE=${NMAP_WORKER_STATUS_FILE:-/run/justup-nmap.status}
READY_TIMEOUT=${NMAP_WORKER_READY_TIMEOUT:-20}
MAX_BACKOFF=${NMAP_WORKER_MAX_BACKOFF:-60}
MAX_FAILURES=${NMAP_WORKER_MAX_FAILURES:-8}
STABLE_AFTER=${NMAP_WORKER_STABLE_AFTER:-120}
export JUSTUP_NMAP_SOCKET=${JUSTUP_NMAP_SOCKET:-/run/justup/nmap.sock}

worker_pid=""
restarts=0

log() {
    echo "[nmap-worker] $*"
}

# state: starting | ready | restarting | failed. Written atomically, readable by every user.
write_status() {
    local state=$1 detail=${2:-} tmp="${STATUS_FILE}.$$"
    detail=${detail//[^A-Za-z0-9 ._:,=-]/_}
    printf '{"state":"%s","detail":"%s","restarts":%d,"since":"%s"}\n' \
        "$state" "$detail" "$restarts" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" \
        && chmod 0644 "$tmp" && mv -f "$tmp" "$STATUS_FILE"
}

# Starts the worker in the background ($! is its PID): own UID, only net_raw in the ambient
# set, no_new_privs, scrubbed environment (no SECRET_KEY).
launch_worker() {
    install -d -m 0750 -o "$WORKER_UID_NAME" -g "$WORKER_UID_NAME" "$(dirname "$JUSTUP_NMAP_SOCKET")" || return 1
    setpriv --reuid="$WORKER_UID_NAME" --regid="$WORKER_UID_NAME" --clear-groups \
        --inh-caps=-all,+net_raw --ambient-caps=-all,+net_raw \
        --no-new-privs --pdeathsig=TERM -- \
        env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent LANG="${LANG:-C.UTF-8}" \
            PYTHONPATH=/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
            NMAPDIR="${NMAPDIR:-/usr/share/nmap}" JUSTUP_NMAP_SOCKET="$JUSTUP_NMAP_SOCKET" \
            LOG_LEVEL="${LOG_LEVEL:-INFO}" \
            python -m app.services.nmap_worker &
}

# The socket must accept a connection from the web user and answer a request. The probe is
# rejected by the worker's validation before any Nmap process exists.
handshake() {
    gosu justup python -c '
import asyncio, json, os, sys

async def probe():
    reader, writer = await asyncio.open_unix_connection(os.environ["JUSTUP_NMAP_SOCKET"])
    writer.write(b"{\"host\":\"probe\",\"protocols\":[]}\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    return isinstance(response.get("error"), dict)

sys.exit(0 if asyncio.run(asyncio.wait_for(probe(), 5)) else 1)
' 2> /dev/null
}

# kill -0 cannot be used: with cap_drop ALL the root supervisor lacks CAP_KILL and gets EPERM for
# a process of another UID. /proc tells whether the child still runs (a zombie counts as gone).
worker_alive() {
    local state
    state=$(sed -n 's/^[0-9]* (.*) //p' "/proc/$1/stat" 2> /dev/null)
    [ -n "$state" ] && [ "${state%% *}" != Z ]
}

# Same limitation for signals: send them with the worker's own UID (needs only SETUID/SETGID).
signal_worker() {
    kill -s "$1" "$2" 2> /dev/null && return 0
    # shellcheck disable=SC2016 # sh expands the quoted script, not this shell
    setpriv --reuid="$WORKER_UID_NAME" --regid="$WORKER_UID_NAME" --clear-groups --no-new-privs \
        sh -c 'kill -s "$0" "$1"' "$1" "$2" 2> /dev/null
}

# 0 = ready, 1 = not ready within the timeout, 2 = worker exited during startup
wait_ready() {
    local deadline=$((SECONDS + READY_TIMEOUT))
    while (( SECONDS < deadline )); do
        worker_alive "$worker_pid" || return 2
        # No -S test: root lacks DAC_OVERRIDE and cannot even stat inside the worker's 0750 directory,
        # so readiness is decided by the connection of the web user alone.
        if handshake; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

stop_worker() {
    if [ -n "$worker_pid" ] && worker_alive "$worker_pid"; then
        signal_worker TERM "$worker_pid"
        wait "$worker_pid" 2> /dev/null
    fi
    worker_pid=""
}

supervise() {
    local failures=0 delay=1 started reason code
    trap 'stop_worker; exit 0' TERM INT
    while :; do
        write_status starting
        if launch_worker; then
            worker_pid=$!
            started=$SECONDS
            wait_ready
            code=$?
            if [ "$code" -eq 0 ]; then
                write_status ready
                log "worker ready (pid $worker_pid)"
                wait "$worker_pid"
                reason="worker exited with status $?"
                if (( SECONDS - started >= STABLE_AFTER )); then
                    failures=0
                    delay=1
                fi
            else
                stop_worker
                if [ "$code" -eq 2 ]; then
                    reason="worker exited during startup"
                else
                    reason="worker not ready within ${READY_TIMEOUT}s"
                fi
            fi
        else
            reason="worker could not be launched"
        fi
        worker_pid=""
        failures=$((failures + 1))
        if (( failures >= MAX_FAILURES )); then
            write_status failed "$reason; gave up after $failures consecutive failures"
            log "ERROR: $reason; giving up after $failures consecutive failures, port scans stay unavailable"
            return 1
        fi
        write_status restarting "$reason; retry in ${delay}s"
        log "WARNING: $reason; restarting in ${delay}s"
        sleep "$delay" &
        wait $!
        delay=$((delay * 2 > MAX_BACKOFF ? MAX_BACKOFF : delay * 2))
        restarts=$((restarts + 1))
    done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    supervise
fi
