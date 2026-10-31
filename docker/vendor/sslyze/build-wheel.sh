#!/usr/bin/env bash
#
# docker/vendor/sslyze/build-wheel.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
# Purpose: Rebuild or verify the pinned, vendored justUp SSLyze wheel.
# Output: docker/vendor/wheels/sslyze-6.3.1+justup.1-py3-none-any.whl.

DEPENDENCIES=(flock git sha256sum unshare)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
UPSTREAM_URL="https://github.com/nabla-c0d3/sslyze.git"
UPSTREAM_COMMIT="e064c2c8e7d251e5bc5db1a7beb21add5e12a0bc"
EXPECTED_WHEEL="sslyze-6.3.1+justup.1-py3-none-any.whl"
EXPECTED_VERSION="6.3.1+justup.1"
BUILD_PYTHON_VERSION="3.13.5"
WHEEL_DIR="${REPO_ROOT}/docker/vendor/wheels"
WORKDIR=""
STAGED_WHEEL=""

function usage() {
    local exit_code="${1:-1}"
    cat <<USAGE
Usage: build-wheel.sh [--check] [-h|--help]

Fetches pinned upstream commit ${UPSTREAM_COMMIT}, applies the local patches, installs
the hash-pinned build toolchain, then builds the wheel with network access disabled.
The builder must be Python ${BUILD_PYTHON_VERSION}; the patch version is pinned because
the vendored artifact is expected to be reproducible.

Options:
  --check    Rebuild temporarily and verify byte identity plus lock/provenance hashes.
  -h, --help Show this help (exit 0).

Dependencies: ${DEPENDENCIES[*]}, Python ${BUILD_PYTHON_VERSION}, and network access to
GitHub/PyPI for fetching the pinned source and installing the hash-pinned build toolchain.
The wheel build itself runs in a network namespace.
USAGE
    exit "$exit_code"
}

function fail() {
    local exit_code="$1"
    shift
    printf 'Error: %s\n' "$*" >&2
    exit "$exit_code"
}

function main() {
    set -Eeuo pipefail
    # Preserve the vendored artifact's existing 0664 source-file modes in the wheel.
    umask 0002

    local check=false
    while (( $# > 0 )); do
        case "$1" in
        --check)
            check=true
            ;;
        -h | --help)
            usage 0
            ;;
        *)
            printf "Error: Unknown option '%s'\n" "$1" >&2
            usage 2
            ;;
        esac
        shift
    done

    exit_on_missing_tools "${DEPENDENCIES[@]}"
    local python_bin="${PYTHON:-python3.13}"
    exit_on_missing_tools "$python_bin"
    check_python_version "$python_bin"
    acquire_build_lock

    WORKDIR="$(mktemp -d)" || fail 1 "cannot create temporary build directory"
    trap cleanup EXIT

    fetch_source
    apply_patches
    create_build_env "$python_bin"
    build_wheel "$check" "$python_bin"
}

function check_python_version() {
    local python_bin="$1"
    local actual
    actual="$("$python_bin" -c 'import platform; print(platform.python_version())')" \
        || fail 1 "cannot read Python version from ${python_bin}"
    if [[ "$actual" != "$BUILD_PYTHON_VERSION" ]]; then
        fail 1 "expected Python ${BUILD_PYTHON_VERSION}, found ${actual}"
    fi
}

function acquire_build_lock() {
    local lock_file="${TMPDIR:-/tmp}/justup-sslyze-wheel-build.lock"
    exec {BUILD_LOCK_FD}>"$lock_file" || fail 1 "cannot open build lock ${lock_file}"
    flock -x "$BUILD_LOCK_FD" || fail 1 "cannot acquire build lock ${lock_file}"
}

function fetch_source() {
    local actual
    git init -q "${WORKDIR}/src" || fail 1 "git init failed"
    git -C "${WORKDIR}/src" fetch -q --depth=1 "$UPSTREAM_URL" "$UPSTREAM_COMMIT" \
        || fail 1 "cannot fetch pinned upstream commit ${UPSTREAM_COMMIT}"
    git -C "${WORKDIR}/src" checkout -q --detach FETCH_HEAD || fail 1 "cannot check out upstream source"
    actual="$(git -C "${WORKDIR}/src" rev-parse HEAD)" || fail 1 "cannot read checked-out commit"
    [[ "$actual" == "$UPSTREAM_COMMIT" ]] || fail 1 "fetched commit ${actual} is not ${UPSTREAM_COMMIT}"

    SOURCE_DATE_EPOCH="$(git -C "${WORKDIR}/src" show -s --format=%ct HEAD)" \
        || fail 1 "cannot read upstream commit time"
    [[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] \
        || fail 1 "upstream SOURCE_DATE_EPOCH is empty or not numeric"
    export SOURCE_DATE_EPOCH
}

function apply_patches() {
    local patch
    shopt -s nullglob
    local patches=("${SCRIPT_DIR}"/patches/*.patch)
    shopt -u nullglob
    (( ${#patches[@]} > 0 )) || fail 1 "no local patch files found"
    for patch in "${patches[@]}"; do
        git -C "${WORKDIR}/src" apply --check "$patch" \
            || fail 1 "patch does not apply: ${patch}"
        git -C "${WORKDIR}/src" apply "$patch" \
            || fail 1 "applying patch failed: ${patch}"
    done
}

function create_build_env() {
    local python_bin="$1"
    "$python_bin" -m venv "${WORKDIR}/venv" || fail 1 "cannot create build virtualenv"
    "${WORKDIR}/venv/bin/python" -m pip install --quiet --no-cache-dir --require-hashes \
        --only-binary=:all: -r "${SCRIPT_DIR}/build-requirements.lock" \
        || fail 1 "hash-pinned build toolchain install failed"
}

function select_wheel() {
    local out_dir="$1"
    shopt -s nullglob
    local wheels=("${out_dir}"/sslyze-*.whl)
    shopt -u nullglob
    (( ${#wheels[@]} == 1 )) \
        || fail 1 "expected exactly one SSLyze wheel, found ${#wheels[@]}"
    [[ "$(basename "${wheels[0]}")" == "$EXPECTED_WHEEL" ]] \
        || fail 1 "unexpected wheel filename: $(basename "${wheels[0]}")"
    printf '%s' "${wheels[0]}"
}

function sha256_file() {
    local path="$1" output digest
    output="$(sha256sum -- "$path")" || fail 1 "cannot calculate SHA-256 for ${path}"
    digest="${output%% *}"
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || fail 1 "invalid SHA-256 output for ${path}"
    printf '%s' "$digest"
}

function validate_wheel_metadata() {
    local wheel="$1" python_bin="$2"
    "$python_bin" - "$wheel" "$EXPECTED_WHEEL" "$EXPECTED_VERSION" <<'PY'
import email
import sys
import zipfile

wheel_path, expected_name, expected_version = sys.argv[1:]
if wheel_path.rsplit("/", 1)[-1] != expected_name:
    raise SystemExit(f"unexpected wheel filename: {wheel_path.rsplit('/', 1)[-1]}")
with zipfile.ZipFile(wheel_path) as archive:
    metadata_paths = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    wheel_paths = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
    if len(metadata_paths) != 1 or len(wheel_paths) != 1:
        raise SystemExit("wheel must contain exactly one METADATA and one WHEEL file")
    metadata = email.message_from_bytes(archive.read(metadata_paths[0]))
    wheel_metadata = archive.read(wheel_paths[0]).decode("utf-8")
if metadata.get("Name", "").lower() != "sslyze" or metadata.get("Version") != expected_version:
    raise SystemExit(f"wheel metadata mismatch: {metadata.get('Name')} {metadata.get('Version')}")
tags = [line.removeprefix("Tag: ") for line in wheel_metadata.splitlines() if line.startswith("Tag: ")]
if tags != ["py3-none-any"]:
    raise SystemExit(f"unexpected wheel tags: {tags}")
PY
    local status=$?
    (( status == 0 )) || fail 1 "wheel metadata validation failed for ${wheel}"
}

function verify_wheel_triplet() {
    local wheel="$1" lock_path="$2" provenance_path="$3" python_bin="$4"
    "$python_bin" - "$wheel" "$lock_path" "$provenance_path" "$EXPECTED_VERSION" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

wheel_path = Path(sys.argv[1])
lock_path = Path(sys.argv[2])
provenance_path = Path(sys.argv[3])
expected_version = sys.argv[4]
actual_hash = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
lock_text = lock_path.read_text(encoding="utf-8").replace("\\\n", " ")
entries = re.findall(
    r"(?m)^sslyze==([^\s]+)\s+--hash=sha256:([0-9a-f]{64})$",
    lock_text,
)
if len(entries) != 1:
    raise SystemExit(f"expected one exact SSLyze lock entry, found {len(entries)}")
locked_version, locked_hash = entries[0]
provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
expected_path = f"docker/vendor/wheels/{wheel_path.name}"
if wheel_path.name != "sslyze-6.3.1+justup.1-py3-none-any.whl":
    raise SystemExit(f"unexpected wheel filename: {wheel_path.name}")
if locked_version != expected_version or provenance.get("version") != expected_version:
    raise SystemExit("wheel version does not match lock and provenance")
if provenance.get("wheel") != expected_path or provenance.get("tags") != "py3-none-any":
    raise SystemExit("wheel path or tags do not match provenance")
toolchain = provenance.get("toolchain", {})
if toolchain.get("python") != "3.13.5" or toolchain.get("umask") != "0002":
    raise SystemExit("builder Python version or umask do not match the reproducibility contract")
if locked_hash != actual_hash or provenance.get("sha256") != actual_hash:
    raise SystemExit("wheel SHA-256 does not match lock and provenance")
if provenance.get("size_bytes") != wheel_path.stat().st_size:
    raise SystemExit("wheel size does not match provenance")
PY
    local status=$?
    (( status == 0 )) || fail 1 "wheel, requirements lock, and provenance do not agree"
}

function build_wheel() {
    local check="$1" python_bin="$2" out="${WORKDIR}/out" wheel built_sha stored_sha final_wheel temp_wheel
    mkdir -p "$out" || fail 1 "cannot create wheel output directory"
    unshare --net -- "${WORKDIR}/venv/bin/python" -m pip wheel --quiet --no-build-isolation --no-deps \
        --no-index --no-cache-dir --wheel-dir "$out" "${WORKDIR}/src" \
        || fail 1 "network-isolated wheel build failed"
    wheel="$(select_wheel "$out")"
    validate_wheel_metadata "$wheel" "$python_bin" || fail 1 "wheel metadata validation failed"
    built_sha="$(sha256_file "$wheel")"
    printf 'Built %s sha256=%s\n' "$(basename "$wheel")" "$built_sha"

    final_wheel="${WHEEL_DIR}/${EXPECTED_WHEEL}"
    if [[ "$check" == true ]]; then
        [[ -f "$final_wheel" ]] || fail 1 "stored wheel is missing: ${final_wheel}"
        verify_wheel_triplet "$final_wheel" "${REPO_ROOT}/docker/requirements.lock" \
            "${REPO_ROOT}/docker/vendor/PROVENANCE.json" "$python_bin" \
            || fail 1 "stored wheel, requirements lock, and provenance do not agree"
        stored_sha="$(sha256_file "$final_wheel")"
        [[ "$stored_sha" == "$built_sha" ]] \
            || fail 1 "rebuild differs from stored wheel (stored: ${stored_sha}, built: ${built_sha})"
        printf '%s\n' "Rebuild is byte-identical and lock/provenance hashes agree."
        return
    fi

    mkdir -p "$WHEEL_DIR" || fail 1 "cannot create wheel directory ${WHEEL_DIR}"
    temp_wheel="${WHEEL_DIR}/.${EXPECTED_WHEEL}.tmp.$$"
    STAGED_WHEEL="$temp_wheel"
    cp -- "$wheel" "$temp_wheel" || fail 1 "cannot stage wheel in ${WHEEL_DIR}"
    [[ "$(sha256_file "$temp_wheel")" == "$built_sha" ]] \
        || fail 1 "staged wheel hash changed before replacement"
    mv -f -- "$temp_wheel" "$final_wheel" || fail 1 "cannot atomically replace ${final_wheel}"
    STAGED_WHEEL=""
    printf 'Stored in %s. Update docker/requirements.lock and docker/vendor/PROVENANCE.json.\n' "$final_wheel"
}

function cleanup() {
    if [[ -n "$STAGED_WHEEL" && -e "$STAGED_WHEEL" ]]; then
        rm -f -- "$STAGED_WHEEL"
    fi
    if [[ -n "$WORKDIR" && -d "$WORKDIR" ]]; then
        rm -rf -- "$WORKDIR"
    fi
}

function exit_on_missing_tools() {
    local cmd
    for cmd in "$@"; do
        command -v "$cmd" &>/dev/null || fail 1 "required tool '${cmd}' is not installed or not in PATH"
    done
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
    exit 0
fi
