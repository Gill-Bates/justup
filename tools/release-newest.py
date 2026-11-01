#!/usr/bin/env python3
#
# tools/release-newest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Decide which release privileges a version has, given the tags already published.
Prints two GITHUB_OUTPUT lines; reasoning goes to stderr.
  is_newest    no published release of any kind is newer; gates the tag cleanup.
  moves_latest stable version and no newer stable release is published; gates 'latest'.
A prerelease never moves 'latest'. Any parse problem fails closed (exit 1, no output).
Usage: release-newest.py <published-tags-file> <version>
"""
from __future__ import annotations
import re
import sys

# Architecture fragments (1.6.1-amd64) parse as prereleases of 1.6.1; they are
# build artefacts of the release workflow, not releases.
ARCH_SUFFIXES = {
    "amd64", "arm64", "arm", "armv6", "armv7", "386",
    "ppc64le", "s390x", "riscv64", "mips64le", "loong64",
}
VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?$")


def suffix_key(suffix: str) -> tuple:
    # Identifier-wise like semver: rc.10 must sort above rc.2.
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in suffix.split("."))


def parse(tag: str):
    """Return (sort key, is_prerelease), or None for a non-release tag."""
    match = VERSION_RE.match(tag)
    if not match:
        return None
    suffix = match.group(2)
    if suffix in ARCH_SUFFIXES:
        return None
    # Padded so 1.7 and 1.7.0 are one version.
    release = tuple(int(p) for p in match.group(1).split("."))
    release += (0,) * (3 - len(release))
    # A prerelease sorts below its own release: 1.7.0-rc.1 < 1.7.0.
    key = (release, (0, suffix_key(suffix)) if suffix else (1, ()))
    return key, suffix is not None


def decide(published: set[str], current: str) -> tuple[bool, bool, list[str], list[str]]:
    parsed = parse(current)
    if parsed is None:
        raise ValueError(f"unparseable version: {current}")
    current_key, current_pre = parsed
    newer, newer_stable = [], []
    for tag in sorted(published):
        entry = parse(tag)
        if entry is None or entry[0] <= current_key:
            continue
        newer.append(tag)
        if not entry[1]:
            newer_stable.append(tag)
    return not newer, (not current_pre and not newer_stable), newer, newer_stable


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    with open(argv[1], encoding="utf-8") as handle:
        published = {line.strip() for line in handle if line.strip()}
    try:
        is_newest, moves_latest, newer, newer_stable = decide(published, argv[2])
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if newer:
        print(f"Newer versions already published: {', '.join(newer)}", file=sys.stderr)
    if moves_latest:
        print(f"{argv[2]} is the newest stable release; 'latest' moves.", file=sys.stderr)
    else:
        print(f"{argv[2]} will be republished, but 'latest' stays put "
              f"(newer stable: {', '.join(newer_stable) or 'none'}).", file=sys.stderr)
    if not is_newest:
        print("A newer release exists; nothing is pruned.", file=sys.stderr)
    print(f"is_newest={str(is_newest).lower()}")
    print(f"moves_latest={str(moves_latest).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
