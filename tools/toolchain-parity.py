#!/usr/bin/env python3
#
# tools/toolchain-parity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Compare the per-architecture toolchain manifests recorded by the release build.

Each ``<dir>/<arch>.txt`` holds ``key=value`` lines (see docker-build.yml, step "Record toolchain
versions"). The gate fails when

  * fewer than two architectures were recorded,
  * a required field is missing or empty in any architecture (a field missing everywhere is a
    gap, not parity), including every direct dependency of ``--pyproject`` when given,
  * a field differs between architectures.

Usage: toolchain-parity.py <dir> [--pyproject pyproject.toml]    (standard library only)
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REQUIRED_FIELDS = ("python", "app_version", "java_version", "libsignal_client", "signal_cli")


def read(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            name, _, value = line.strip().partition("=")
            out[name] = value
    return out


def direct_dependencies(pyproject: Path) -> list[str]:
    """Normalized distribution names of [project].dependencies (PEP 503 normalization)."""
    with pyproject.open("rb") as handle:
        expressions = tomllib.load(handle)["project"]["dependencies"]
    names = []
    for expression in expressions:
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", expression)
        if not match:
            raise ValueError(f"unparseable requirement: {expression!r}")
        names.append(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def problems(recorded: dict[str, dict[str, str]], required: list[str]) -> list[str]:
    found = []
    if len(recorded) < 2:
        found.append("parity needs at least two architectures, got " + ", ".join(sorted(recorded)))
    for arch, versions in sorted(recorded.items()):
        found.extend(
            f"{arch}: required field '{name}' is missing or empty" for name in required if not versions.get(name)
        )
    names = sorted({name for versions in recorded.values() for name in versions})
    for name in names:
        seen = {arch: versions.get(name, "<missing>") for arch, versions in recorded.items()}
        if len(set(seen.values())) > 1:
            detail = ", ".join(f"{arch}={value}" for arch, value in sorted(seen.items()))
            found.append(f"{name}: {detail}")
    return found


def main(argv: list[str]) -> int:
    args = argv[1:]
    pyproject = None
    if len(args) == 3 and args[1] == "--pyproject":
        pyproject = Path(args[2])
    elif len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        recorded = {path.stem: read(path) for path in sorted(Path(args[0]).glob("*.txt"))}
        required = list(REQUIRED_FIELDS) + (direct_dependencies(pyproject) if pyproject else [])
    except (OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    for arch, versions in recorded.items():
        print(f"--- {arch} ---")
        for name, value in sorted(versions.items()):
            print(f"  {name}={value}")
    found = problems(recorded, required)
    if found:
        print("\nFAIL: the architecture manifests are incomplete or differ:", file=sys.stderr)
        for problem in found:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nEvery rolling component is resolved once in the meta job and passed to all builds, "
            "so a difference means a build arg did not reach the Dockerfile or a stale cached layer was reused.",
            file=sys.stderr,
        )
        return 1
    print(f"\nOK: {', '.join(sorted(recorded))} carry identical versions of all {len(required)} required components")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
