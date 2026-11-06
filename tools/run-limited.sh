#!/usr/bin/env bash
#
# tools/run-limited.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
# Purpose: Run one heavy local test command under a shared host-wide semaphore, a CPU budget and
#          worker caps, so parallel agent sessions cannot overload the machine.
# Scope:   Local use only. GitHub Actions does not use this wrapper and keeps its own settings.
#
# Environment (all optional):
#   JUSTUP_TEST_SLOTS     concurrent heavy suites per lock directory (default 2; 1 = conservative)
#   JUSTUP_TEST_WAIT      seconds to wait for a free slot (default 3600; 0 = fail at once)
#   JUSTUP_TEST_CPUS      CPU budget per suite in cores (default 4, capped at nproc)
#   JUSTUP_TEST_MAX_WORKERS  cap for pytest-xdist, node --test and Playwright workers (default 4;
#                         Playwright is additionally capped at JUSTUP_TEST_MAX_PW_WORKERS, default 3)
#   JUSTUP_TEST_CGROUP    auto (default) or off; auto uses systemd-run --scope CPUQuota when it works
#   JUSTUP_TEST_LOCK_DIR  lock directory (default $XDG_RUNTIME_DIR/justup-test-slots or
#                         /tmp/justup-test-slots-$UID), mode 0700, owned by the current user
#
# Exit codes: the command's own exit code; 124 = no slot within JUSTUP_TEST_WAIT;
#             125 = wrapper setup error; 130/143 = interrupted by SIGINT/SIGTERM.

DEPENDENCIES=(flock setsid stat mkdir chmod sleep kill nproc id)

SLOTS="${JUSTUP_TEST_SLOTS:-2}"
WAIT_SECONDS="${JUSTUP_TEST_WAIT:-3600}"
CPUS="${JUSTUP_TEST_CPUS:-4}"
MAX_WORKERS="${JUSTUP_TEST_MAX_WORKERS:-4}"
MAX_PW_WORKERS="${JUSTUP_TEST_MAX_PW_WORKERS:-3}"
CGROUP_MODE="${JUSTUP_TEST_CGROUP:-auto}"
STOP_GRACE_SECONDS=10

CMD=()
PREFIX=()
CHILD_PID=""
WATCHDOG_PID=""
RECEIVED_SIGNAL=""

function usage() {
    cat <<'EOF'
Usage: tools/run-limited.sh [--] COMMAND [ARGS...]

Runs COMMAND once a shared test slot is free (default 2 slots per host user), at nice 10 and,
where systemd-run --scope works, inside a cgroup with CPUQuota=400%. Arguments are passed on
unchanged, except that local worker counts are capped (pytest -n, node --test-concurrency,
Playwright workers) and `npm test` becomes `npm run test:local`.

Dependencies: flock, setsid, stat, nproc (optional: nice, ionice, systemd-run)

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

    # A wrapped command that calls the wrapper again must not wait for its own slot.
    if [[ -n "${JUSTUP_RUN_LIMITED:-}" ]]; then
        exec "$@"
    fi

    adjust_command "$@"
    prepare_lock_dir || exit 125
    acquire_slot
    export JUSTUP_RUN_LIMITED=1
    export PYTEST_XDIST_AUTO_NUM_WORKERS="$MAX_WORKERS"
    export JUSTUP_LAYOUT_WORKERS="${LAYOUT_WORKERS}"
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
    local pair name value
    for pair in "JUSTUP_TEST_SLOTS=$SLOTS" "JUSTUP_TEST_CPUS=$CPUS" "JUSTUP_TEST_MAX_WORKERS=$MAX_WORKERS" \
        "JUSTUP_TEST_MAX_PW_WORKERS=$MAX_PW_WORKERS"; do
        name="${pair%%=*}"
        value="${pair#*=}"
        if [[ ! "$value" =~ ^[1-9][0-9]{0,3}$ ]]; then
            printf "Error: %s must be a positive integer, got '%s'\n" "$name" "$value" >&2
            exit 125
        fi
    done
    if [[ ! "$WAIT_SECONDS" =~ ^[0-9]{1,7}$ ]]; then
        printf "Error: JUSTUP_TEST_WAIT must be a non-negative integer, got '%s'\n" "$WAIT_SECONDS" >&2
        exit 125
    fi
    if [[ "$CGROUP_MODE" != "auto" && "$CGROUP_MODE" != "off" ]]; then
        printf "Error: JUSTUP_TEST_CGROUP must be 'auto' or 'off', got '%s'\n" "$CGROUP_MODE" >&2
        exit 125
    fi
    local available
    available="$(nproc 2>/dev/null)" || available=1
    if (( CPUS > available )); then
        CPUS="$available"
    fi
    LAYOUT_WORKERS="${JUSTUP_LAYOUT_WORKERS:-}"
    if [[ -z "$LAYOUT_WORKERS" ]]; then
        LAYOUT_WORKERS="$(( MAX_PW_WORKERS < MAX_WORKERS ? MAX_PW_WORKERS : MAX_WORKERS ))"
    fi
    if [[ "$LAYOUT_WORKERS" =~ ^[0-9]+$ ]] && (( LAYOUT_WORKERS > MAX_PW_WORKERS )); then
        printf "Note: JUSTUP_LAYOUT_WORKERS=%s lowered to %s\n" "$LAYOUT_WORKERS" "$MAX_PW_WORKERS" >&2
        LAYOUT_WORKERS="$MAX_PW_WORKERS"
    fi
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
                CMD+=("$arg" "$(cap_workers "$next" "$MAX_WORKERS")")
                ((i++))
                continue
                ;;
            -n[0-9]*)
                CMD+=("-n$(cap_workers "${arg#-n}" "$MAX_WORKERS")")
                continue
                ;;
            --numprocesses=*)
                CMD+=("--numprocesses=$(cap_workers "${arg#*=}" "$MAX_WORKERS")")
                continue
                ;;
            esac
        fi
        if [[ "$is_playwright" == true ]]; then
            case "$arg" in
            --workers | -j)
                CMD+=("$arg" "$(cap_workers "$next" "$MAX_PW_WORKERS")")
                ((i++))
                continue
                ;;
            --workers=*)
                CMD+=("--workers=$(cap_workers "${arg#*=}" "$MAX_PW_WORKERS")")
                continue
                ;;
            esac
        fi
        if [[ "${in[0]##*/}" == "node" ]]; then
            case "$arg" in
            --test)
                CMD+=("$arg")
                if [[ "${in[*]}" != *--test-concurrency* ]]; then
                    CMD+=("--test-concurrency=$MAX_WORKERS")
                fi
                continue
                ;;
            --test-concurrency=*)
                CMD+=("--test-concurrency=$(cap_workers "${arg#*=}" "$MAX_WORKERS")")
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
        default_dir="/tmp/justup-test-slots-$(id -u)"
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
