#!/usr/bin/env bash
#
# tools/run-limited.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
# Purpose: Run one heavy local test command under a shared host-wide semaphore, a CPU budget and
#          worker caps, so parallel agent sessions cannot overload the machine.
# Scope:   Local use only. GitHub Actions does not use this wrapper and keeps its own settings.
#
# All sizes come from tools/test-budget.mjs (CPU affinity, cgroup limits, free RAM, load); nothing is
# hard-coded here. Overrides (all optional, validated there): JUSTUP_TEST_SLOTS,
# JUSTUP_TEST_CPU_SHARE, JUSTUP_TEST_RUN_SHARE, JUSTUP_TEST_CORES_PER_RUN, JUSTUP_TEST_RAM_SHARE,
# JUSTUP_TEST_RAM_PER_WORKER_MB, JUSTUP_TEST_LOAD_THRESHOLD, JUSTUP_TEST_LOAD_AWARE.
#
# Environment owned by this wrapper:
#   JUSTUP_TEST_WAIT        seconds to wait for a free slot (default 3600; 0 = fail at once)
#   JUSTUP_TEST_LOAD_WAIT   seconds to wait for a busy machine to calm down after a slot was
#                           acquired (default 60; the run then starts with the reduced budget)
#   JUSTUP_TEST_CGROUP      auto (default) or off; auto uses systemd-run --scope CPUQuota when it works
#   JUSTUP_TEST_LOCK_DIR    lock directory (default $XDG_RUNTIME_DIR/justup-test-slots or
#                           ${TMPDIR:-/tmp}/justup-test-slots-$UID), mode 0700, owned by the current user
#
# Exit codes: the command's own exit code; 124 = no slot within JUSTUP_TEST_WAIT;
#             125 = wrapper setup error; 130/143 = interrupted by SIGINT/SIGTERM.

DEPENDENCIES=(flock setsid stat mkdir chmod sleep kill id node)

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WAIT_SECONDS="${JUSTUP_TEST_WAIT:-3600}"
LOAD_WAIT_SECONDS="${JUSTUP_TEST_LOAD_WAIT:-60}"
CGROUP_MODE="${JUSTUP_TEST_CGROUP:-auto}"
STOP_GRACE_SECONDS=10
SLOTS=1 CPUS=1 PY_WORKERS=1 NODE_WORKERS=1 PW_WORKERS=1 BUSY=false

CMD=()
PREFIX=()
CHILD_PID=""
WATCHDOG_PID=""
RECEIVED_SIGNAL=""

function usage() {
    cat <<'EOF'
Usage: tools/run-limited.sh [--] COMMAND [ARGS...]

Runs COMMAND once a shared test slot is free, at nice 10 and, where systemd-run --scope works,
inside a cgroup with a CPUQuota. Slot count, cores per run and worker counts are derived from
this machine by tools/test-budget.mjs (run `node tools/test-budget.mjs` to see them). Arguments
are passed on unchanged, except that worker counts are capped to the budget (pytest -n,
node --test-concurrency, Playwright workers) and `npm test` becomes `npm run test:local`.

Dependencies: flock, setsid, stat, node (optional: nice, ionice, systemd-run)

Examples:
  tools/run-limited.sh pytest -n auto
  tools/run-limited.sh node tests/layout/run.mjs --profile quick --browsers chromium
  JUSTUP_TEST_SLOTS=1 tools/run-limited.sh npm test
EOF
    exit 1
}

function main() {
    if (( $# > 0 )) && [[ "$1" == "--" ]]; then
        shift
    fi
    case "${1:-}" in
    "" | -h | --help)
        usage
        ;;
    esac

    exit_on_missing_tools "${DEPENDENCIES[@]}"
    validate_settings
    load_budget

    # A wrapped command that calls the wrapper again must not wait for its own slot.
    if [[ -n "${JUSTUP_RUN_LIMITED:-}" ]]; then
        exec "$@"
    fi

    prepare_lock_dir || exit 125
    acquire_slot
    wait_for_calm_machine
    adjust_command "$@"
    export JUSTUP_RUN_LIMITED=1
    export PYTEST_XDIST_AUTO_NUM_WORKERS="$PY_WORKERS"
    export JUSTUP_LAYOUT_WORKERS="${JUSTUP_LAYOUT_WORKERS:-$PW_WORKERS}"
    build_prefix
    run_command
}

function exit_on_missing_tools() {
    local cmd
    for cmd in "$@"; do
        if command -v "$cmd" &>/dev/null; then
            continue
        fi
        printf "Error: Required tool '%s' is not installed or not in PATH\n" "$cmd" >&2
        exit 125
    done
}

function validate_settings() {
    local name value
    for name in JUSTUP_TEST_WAIT JUSTUP_TEST_LOAD_WAIT; do
        value="${!name:-0}"
        if [[ ! "$value" =~ ^[0-9]{1,7}$ ]]; then
            printf "Error: %s must be a non-negative integer, got '%s'\n" "$name" "$value" >&2
            exit 125
        fi
    done
    if [[ "$CGROUP_MODE" != "auto" && "$CGROUP_MODE" != "off" ]]; then
        printf "Error: JUSTUP_TEST_CGROUP must be 'auto' or 'off', got '%s'\n" "$CGROUP_MODE" >&2
        exit 125
    fi
}

# Reads the computed budget; the key=value lines are parsed, never evaluated.
function load_budget() {
    local output key value
    if ! output="$(node "$SCRIPT_DIR/test-budget.mjs")"; then
        exit 125
    fi
    while IFS='=' read -r key value; do
        if [[ ! "$value" =~ ^[0-9]+$ && "$key" != "busy" && "$key" != "ramMb" ]]; then
            continue
        fi
        case "$key" in
        slots) SLOTS="$value" ;;
        runCores) CPUS="$value" ;;
        pytestWorkers) PY_WORKERS="$value" ;;
        nodeConcurrency) NODE_WORKERS="$value" ;;
        playwrightWorkers) PW_WORKERS="$value" ;;
        busy) BUSY="$value" ;;
        esac
    done <<<"$output"
}

# Holding a slot while the machine is still busy keeps further suites away; the run then starts
# with the load-reduced budget instead of waiting forever.
function wait_for_calm_machine() {
    local waited=0
    while [[ "$BUSY" == "true" ]] && (( waited < LOAD_WAIT_SECONDS )) && [[ -z "$RECEIVED_SIGNAL" ]]; do
        if (( waited % 30 == 0 )); then
            printf "run-limited: machine is busy, waiting up to %s s before starting with %s core(s)\n" \
                "$LOAD_WAIT_SECONDS" "$CPUS" >&2
        fi
        sleep 1
        waited=$((waited + 1))
        if (( waited % 5 == 0 )); then
            load_budget
        fi
    done
    if [[ -n "$RECEIVED_SIGNAL" ]]; then
        exit "$([[ "$RECEIVED_SIGNAL" == INT ]] && echo 130 || echo 143)"
    fi
    printf "run-limited: budget: %s core(s), pytest/node workers %s/%s, Playwright workers %s\n" \
        "$CPUS" "$PY_WORKERS" "$NODE_WORKERS" "$PW_WORKERS" >&2
}

# Caps worker counts that would bypass the budget; everything else is copied unchanged.
function adjust_command() {
    local -a in=("$@")
    local is_pytest=false is_playwright=false
    local i arg next
    for ((i = 0; i < ${#in[@]} && i < 4; i++)); do
        case "${in[i]##*/}" in
        pytest | py.test) is_pytest=true ;;
        playwright) is_playwright=true ;;
        esac
        if [[ "${in[i]}" == "-m" && "${in[i + 1]:-}" == "pytest" ]]; then
            is_pytest=true
        fi
    done
    for arg in "${in[@]}"; do
        if [[ "$arg" == */@playwright/test/cli.js || "$arg" == "tests/layout/run.mjs" ]]; then
            is_playwright=true
        fi
    done

    if [[ "${in[0]##*/}" == "npm" ]] && [[ "${in[*]}" == "npm test" || "${in[*]}" == "npm run test" ]]; then
        CMD=(npm run test:local)
        return 0
    fi

    CMD=()
    for ((i = 0; i < ${#in[@]}; i++)); do
        arg="${in[i]}"
        next="${in[i + 1]:-}"
        if [[ "$is_pytest" == true ]]; then
            case "$arg" in
            -n | --numprocesses)
                CMD+=("$arg" "$(cap_workers "$next" "$PY_WORKERS")")
                ((i++))
                continue
                ;;
            -n[0-9]*)
                CMD+=("-n$(cap_workers "${arg#-n}" "$PY_WORKERS")")
                continue
                ;;
            --numprocesses=*)
                CMD+=("--numprocesses=$(cap_workers "${arg#*=}" "$PY_WORKERS")")
                continue
                ;;
            esac
        fi
        if [[ "$is_playwright" == true ]]; then
            case "$arg" in
            --workers | -j)
                CMD+=("$arg" "$(cap_workers "$next" "$PW_WORKERS")")
                ((i++))
                continue
                ;;
            --workers=*)
                CMD+=("--workers=$(cap_workers "${arg#*=}" "$PW_WORKERS")")
                continue
                ;;
            esac
        fi
        if [[ "${in[0]##*/}" == "node" ]]; then
            case "$arg" in
            --test)
                CMD+=("$arg")
                if [[ "${in[*]}" != *--test-concurrency* ]]; then
                    CMD+=("--test-concurrency=$NODE_WORKERS")
                fi
                continue
                ;;
            --test-concurrency=*)
                CMD+=("--test-concurrency=$(cap_workers "${arg#*=}" "$NODE_WORKERS")")
                continue
                ;;
            esac
        fi
        CMD+=("$arg")
    done
}

# Numbers and percentages above the cap (or any non-number except pytest's "auto") become the cap.
function cap_workers() {
    local value="$1" cap="$2"
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        if (( value > cap )); then
            printf "Note: worker count %s lowered to %s\n" "$value" "$cap" >&2
            printf '%s' "$cap"
        else
            printf '%s' "$value"
        fi
    elif [[ "$value" == "auto" || "$value" == "logical" ]]; then
        printf '%s' "$value"
    else
        printf "Note: worker setting '%s' replaced by %s\n" "$value" "$cap" >&2
        printf '%s' "$cap"
    fi
}

function prepare_lock_dir() {
    local default_dir
    if [[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" && -O "$XDG_RUNTIME_DIR" ]]; then
        default_dir="$XDG_RUNTIME_DIR/justup-test-slots"
    else
        default_dir="${TMPDIR:-/tmp}/justup-test-slots-$(id -u)"
    fi
    LOCK_DIR="${JUSTUP_TEST_LOCK_DIR:-$default_dir}"
    if [[ -L "$LOCK_DIR" ]]; then
        printf "Error: lock directory '%s' is a symlink\n" "$LOCK_DIR" >&2
        return 1
    fi
    if [[ ! -d "$LOCK_DIR" ]]; then
        # shellcheck disable=SC2174 # only the deepest directory needs 0700, chmod below enforces it
        mkdir -p -m 0700 -- "$LOCK_DIR" 2>/dev/null || [[ -d "$LOCK_DIR" ]] || {
            printf "Error: cannot create lock directory '%s'\n" "$LOCK_DIR" >&2
            return 1
        }
    fi
    if [[ "$(stat -c %u -- "$LOCK_DIR")" != "$(id -u)" ]]; then
        printf "Error: lock directory '%s' is not owned by the current user\n" "$LOCK_DIR" >&2
        return 1
    fi
    chmod 0700 -- "$LOCK_DIR" || {
        printf "Error: cannot set mode 0700 on '%s'\n" "$LOCK_DIR" >&2
        return 1
    }
}

# Holds one flock on fd 9 until this wrapper exits; the kernel releases it on any exit path.
function acquire_slot() {
    local waited=0 announced=-1 index file
    trap 'RECEIVED_SIGNAL=INT' INT
    trap 'RECEIVED_SIGNAL=TERM' TERM
    while true; do
        for ((index = 0; index < SLOTS; index++)); do
            file="$LOCK_DIR/slot-$index.lock"
            if [[ -L "$file" ]]; then
                printf "Error: lock file '%s' is a symlink\n" "$file" >&2
                exit 125
            fi
            exec 9>>"$file" || {
                printf "Error: cannot open lock file '%s'\n" "$file" >&2
                exit 125
            }
            if flock -n 9; then
                if ! slot_allowed "$index"; then
                    exec 9>&-
                    continue
                fi
                printf '%s\n' "$SLOTS" >"$LOCK_DIR/slot-$index.n"
                trap - INT TERM
                if [[ -n "$RECEIVED_SIGNAL" ]]; then
                    exit "$([[ "$RECEIVED_SIGNAL" == INT ]] && echo 130 || echo 143)"
                fi
                printf "run-limited: acquired test slot %s of %s\n" "$((index + 1))" "$SLOTS" >&2
                return 0
            fi
            exec 9>&-
        done
        if [[ -n "$RECEIVED_SIGNAL" ]]; then
            printf "run-limited: interrupted while waiting for a test slot\n" >&2
            exit "$([[ "$RECEIVED_SIGNAL" == INT ]] && echo 130 || echo 143)"
        fi
        if (( waited >= WAIT_SECONDS )); then
            printf "Error: no free test slot within %s s (all %s busy, lock dir %s)\n" \
                "$WAIT_SECONDS" "$SLOTS" "$LOCK_DIR" >&2
            exit 124
        fi
        if (( waited / 30 != announced )); then
            announced=$((waited / 30))
            printf "run-limited: all %s test slots are busy, waiting (%s s of max %s s)...\n" \
                "$SLOTS" "$waited" "$WAIT_SECONDS" >&2
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

# Sessions may compute different slot counts (cgroup, load, overrides). The smallest count of any
# current holder wins: a slot index is only usable if it is below every holder's count.
function slot_allowed() {
    local index="$1" other recorded
    (( index < SLOTS )) || return 1
    for ((other = 0; other < SLOTS || other < index; other++)); do
        [[ "$other" -eq "$index" ]] && continue
        [[ -f "$LOCK_DIR/slot-$other.lock" ]] || continue
        if ( flock -n 8 ) 8<"$LOCK_DIR/slot-$other.lock" 2>/dev/null; then
            continue
        fi
        recorded="$(cat -- "$LOCK_DIR/slot-$other.n" 2>/dev/null)"
        if [[ "$recorded" =~ ^[0-9]+$ ]] && (( index >= recorded )); then
            return 1
        fi
    done
    return 0
}

function build_prefix() {
    PREFIX=()
    if command -v nice &>/dev/null; then
        PREFIX+=(nice -n 10)
    else
        printf "run-limited: nice not available, running at normal priority\n" >&2
    fi
    if command -v ionice &>/dev/null && ionice -c 2 -n 7 true &>/dev/null; then
        PREFIX+=(ionice -c 2 -n 7)
    fi
    CGROUP_PREFIX=()
    if [[ "$CGROUP_MODE" == "off" ]]; then
        printf "run-limited: cgroup CPU limit disabled; only slots and worker caps are active\n" >&2
    else
        detect_cgroup_scope
    fi
}

# Only claims a CPU limit after reading the quota back from the new scope's cgroup.
function detect_cgroup_scope() {
    local -a scope
    local expected observed
    if ! command -v systemd-run &>/dev/null || [[ ! -d /run/systemd/system ]]; then
        printf "run-limited: systemd-run --scope unavailable; no CPU limit (only slots %s and worker caps)\n" "$SLOTS" >&2
        return 0
    fi
    scope=(systemd-run --scope --quiet --collect -p "CPUQuota=$((CPUS * 100))%")
    if [[ "$(id -u)" != "0" ]]; then
        scope=(systemd-run --user --scope --quiet --collect -p "CPUQuota=$((CPUS * 100))%")
    fi
    expected="$((CPUS * 100000)) 100000"
    # shellcheck disable=SC2016 # expansion is meant to happen in the inner shell
    observed="$("${scope[@]}" sh -c 'cat "/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/cpu.max"' 2>/dev/null)"
    if [[ "$observed" == "$expected" ]]; then
        CGROUP_PREFIX=("${scope[@]}")
        printf "run-limited: CPU limit active (CPUQuota=%s%%, %s slots)\n" "$((CPUS * 100))" "$SLOTS" >&2
    else
        printf "run-limited: CPUQuota could not be verified (read '%s'); no CPU limit, only slots %s and worker caps\n" \
            "${observed:-none}" "$SLOTS" >&2
    fi
}

function stop_group() {
    local signal="$1"
    kill "-$signal" -- "-$CHILD_PID" 2>/dev/null
}

# SIGTERM is forwarded for both signals: background jobs of a non-interactive shell start with
# SIGINT ignored, so a forwarded SIGINT would not reach every command.
function on_signal() {
    RECEIVED_SIGNAL="$1"
    stop_group TERM
    if [[ -z "$WATCHDOG_PID" ]]; then
        ( sleep "$STOP_GRACE_SECONDS"; kill -KILL -- "-$CHILD_PID" 2>/dev/null ) 9>&- &
        WATCHDOG_PID=$!
    fi
}

function run_command() {
    local status
    trap 'on_signal TERM' TERM
    trap 'on_signal INT' INT
    # Own session = own process group, so cleanup reaches browsers and stub apps but nothing else.
    # fd 9 is closed for the child: a leaked daemon must not hold the slot after the wrapper ends.
    setsid "${CGROUP_PREFIX[@]}" "${PREFIX[@]}" "${CMD[@]}" <&0 9>&- &
    CHILD_PID=$!
    wait "$CHILD_PID"
    status=$?
    while kill -0 "$CHILD_PID" 2>/dev/null; do
        wait "$CHILD_PID"
        status=$?
    done
    trap - INT TERM
    if [[ -n "$WATCHDOG_PID" ]]; then
        kill "$WATCHDOG_PID" 2>/dev/null
    fi
    # Stragglers of the command's own process group (e.g. a browser that outlived its runner).
    if kill -0 -- "-$CHILD_PID" 2>/dev/null; then
        stop_group TERM
        sleep 1
        stop_group KILL
    fi
    if [[ -n "$RECEIVED_SIGNAL" && "$status" -ge 128 ]]; then
        status="$([[ "$RECEIVED_SIGNAL" == INT ]] && echo 130 || echo 143)"
    fi
    exit "$status"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
    # shellcheck disable=SC2317 # main always exits; kept for the house guard-clause pattern
    exit 0
fi
