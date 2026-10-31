#!/usr/bin/env bash
#
# tools/purge-secret-history.sh
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Purpose: Remove leaked setup.conf blobs (DEC-11) from the whole Git history.
# Usage:   tools/purge-secret-history.sh [--execute]
# Without --execute only the affected paths and blobs are listed (dry run).
# Prerequisites: secrets already rotated, git-filter-repo installed, all work
# pushed or backed up. The rewrite changes every commit id; a force-push of all
# refs and a fresh clone for every collaborator follow it.

PATHS=(setup.conf tracking/setup.conf)

function usage() {
    sed -n '2,8p' "$0" >&2
    exit 1
}

function list_affected() {
    local path commit blob
    for path in "${PATHS[@]}"; do
        while read -r commit; do
            blob=$(git rev-parse "${commit}:${path}" 2>/dev/null) || continue
            printf '%s %s %s\n' "$path" "$commit" "$blob"
        done < <(git log --all --format=%H -- "$path")
    done | sort -u
}

function main() {
    local execute=false
    while (( $# > 0 )); do
        case $1 in
        --execute) execute=true ;;
        *) usage ;;
        esac
        shift
    done

    command -v git-filter-repo >/dev/null 2>&1 || {
        echo "Error: git-filter-repo is required" >&2
        exit 1
    }

    echo "Affected path / commit / blob:"
    list_affected

    if [[ "$execute" != true ]]; then
        echo "Dry run only. Re-run with --execute to rewrite history."
        return 0
    fi

    if [[ -n "$(git status --porcelain)" ]]; then
        echo "Error: working tree is not clean" >&2
        exit 1
    fi

    local args=()
    local path
    for path in "${PATHS[@]}"; do
        args+=(--path "$path")
    done
    # filter-repo drops the origin remote on purpose; re-add it before pushing.
    git filter-repo --invert-paths "${args[@]}" --force || exit 1
    echo "Done. Re-add the remote, then: git push --force --all && git push --force --tags"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
    exit 0
fi
