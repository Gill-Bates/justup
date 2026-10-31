#!/usr/bin/env python3
#
# tools/relock-runtime.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Rewrite docker/requirements.lock as a wheel-only, hash-pinned runtime lock.

Versions are taken from the existing lock (bump a version there, then rerun).
For every package except the vendored sslyze wheel, the SHA-256 of each PyPI
wheel usable on CPython 3.13 / Debian glibc, for BOTH linux/amd64 and
linux/arm64, is written. A package without a usable wheel for either
architecture aborts the run: sdists are never locked. The sslyze entry takes the
version and hash of the wheel stored in docker/vendor/wheels/.

Usage: relock-runtime.py [--write]   (default: verify only, exit 1 on drift)
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "docker" / "requirements.lock"
WHEELS = ROOT / "docker" / "vendor" / "wheels"
ARCHES = ("x86_64", "aarch64")
MAX_GLIBC_MINOR = 41  # Debian 13 (python:3.13-slim) ships glibc 2.41
LEGACY_MANYLINUX = {"manylinux1": 5, "manylinux2010": 12, "manylinux2014": 17}

HEADER = """\
# Fully pinned, hash-checked, wheel-only runtime dependency set used by docker/Dockerfile and CI.
# Versions come from this file; `python tools/relock-runtime.py --write` refreshes every hash from
# the PyPI JSON API for CPython 3.13 on linux x86_64 and aarch64 (glibc) plus pure-Python wheels.
# No sdist is locked: installs run with --only-binary=:all:, so nothing is compiled at build time.
#
# sslyze is the justUp wheel stored in docker/vendor/wheels/ (upstream commit
# e064c2c8e7d251e5bc5db1a7beb21add5e12a0bc, version 6.3.1+justup.1); its hash is the
# SHA-256 of that file, not of a PyPI release. See docs/development/setup.md.
"""


def _arch_ok(platform: str, arch: str) -> bool:
    if platform == "any":
        return True
    match = re.fullmatch(r"(manylinux(?:1|2010|2014)|manylinux_2_(\d+))_" + arch, platform)
    if not match:
        return False
    minor = int(match.group(2)) if match.group(2) else LEGACY_MANYLINUX[match.group(1)]
    return minor <= MAX_GLIBC_MINOR


def _interp_ok(tag) -> bool:
    if tag.abi == "none":
        return tag.interpreter in ("py3", "py2", "py30") or re.fullmatch(r"py3\d*", tag.interpreter) is not None
    if tag.abi == "abi3":
        m = re.fullmatch(r"cp3(\d+)", tag.interpreter)
        return bool(m) and int(m.group(1)) <= 13
    return tag.interpreter == "cp313" and tag.abi == "cp313"


def wheel_arches(filename: str) -> set[str]:
    tags = parse_wheel_filename(filename)[3]
    found = set()
    for tag in tags:
        if _interp_ok(tag):
            found.update(a for a in ARCHES if _arch_ok(tag.platform, a))
    return found


def _read_lock() -> list[tuple[str, str]]:
    text = LOCK.read_text(encoding="utf-8").replace("\\\n", " ")
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            name, _, version = line.split(" --hash=")[0].strip().partition("==")
            out.append((name, version))
    return out


def _pypi_hashes(name: str, version: str) -> list[str]:
    url = f"https://pypi.org/pypi/{canonicalize_name(name)}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as resp:
        files = json.load(resp)["urls"]
    wheels = [f for f in files if f["filename"].endswith(".whl")]
    covered: set[str] = set()
    hashes = []
    for item in wheels:
        arches = wheel_arches(item["filename"])
        if arches:
            covered |= arches
            hashes.append(item["digests"]["sha256"])
    if covered != set(ARCHES):
        raise SystemExit(f"{name}=={version}: no usable wheel for {sorted(set(ARCHES) - covered)}")
    return sorted(set(hashes))


def _sslyze_entry() -> tuple[str, str]:
    wheels = sorted(WHEELS.glob("sslyze-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one sslyze wheel in {WHEELS}, found {len(wheels)}")
    version = str(parse_wheel_filename(wheels[0].name)[1])
    return version, hashlib.sha256(wheels[0].read_bytes()).hexdigest()


def _render() -> str:
    parts = [HEADER]
    for name, locked_version in sorted(_read_lock(), key=lambda p: canonicalize_name(p[0])):
        if canonicalize_name(name) == "sslyze":
            version, digest = _sslyze_entry()
            hashes = [digest]
        else:
            version, hashes = locked_version, _pypi_hashes(name, locked_version)
        lines = [f"{name}=={version}"] + [f"    --hash=sha256:{h}" for h in hashes]
        parts.append(" \\\n".join(lines) + "\n")
    return "".join(parts)


def main() -> int:
    rendered = _render()
    if "--write" in sys.argv[1:]:
        LOCK.write_text(rendered, encoding="utf-8")
        print(f"wrote {LOCK}")
        return 0
    if rendered != LOCK.read_text(encoding="utf-8"):
        print("docker/requirements.lock is out of date; run with --write", file=sys.stderr)
        return 1
    print("docker/requirements.lock is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
