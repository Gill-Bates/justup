#!/usr/bin/env python3
#
# tools/verify-manifest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Check a multi-arch image index against the platforms and digests the build jobs pushed.

``imagetools create`` only proves the registry accepted an index; this proves it lists exactly
the expected platforms and, for ``verify``, the digests that were scanned and pushed.

  verify <manifest.json> <digest-dir>   <digest-dir> holds one file per architecture, named
                                        after it, containing that image's sha256 digest.
  children <manifest.json> <platform>...  print "<platform> <digest>" for each child of the
                                        index; the platform set must match exactly.

``manifest.json`` is the output of ``docker buildx imagetools inspect --raw``. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


class ManifestError(Exception):
    """The index does not match the expectation."""


def platform_digests(index: dict) -> dict[str, str]:
    """Return ``os/architecture -> child digest``; duplicates and non-indexes raise."""
    manifests = index.get("manifests")
    if manifests is None:
        raise ManifestError("not a manifest index")
    identities = []
    digests = {}
    for entry in manifests:
        platform = entry.get("platform") or {}
        architecture = platform.get("architecture")
        # Attestations ride along as unknown/unknown entries.
        if not architecture or architecture == "unknown":
            continue
        # Identity includes the variant; the compared set is os/architecture.
        identities.append((platform.get("os"), architecture, platform.get("variant") or ""))
        digests[f"{platform.get('os')}/{architecture}"] = entry.get("digest")
    duplicates = sorted({"/".join(part for part in i if part) for i in identities if identities.count(i) > 1})
    if duplicates:
        raise ManifestError(f"index lists duplicate platforms: {', '.join(duplicates)}")
    return digests


def check_platforms(found: dict[str, str], expected: list[str]) -> None:
    if sorted(found) != sorted(expected):
        raise ManifestError(f"index describes {sorted(found)}, expected {sorted(expected)}")


def pushed_digests(digest_dir: Path) -> dict[str, str]:
    """``linux/<arch> -> digest`` from the per-architecture digest files."""
    return {f"linux/{path.name}": path.read_text(encoding="utf-8").strip() for path in digest_dir.iterdir()}


def verify(index: dict, pushed: dict[str, str]) -> dict[str, str]:
    found = platform_digests(index)
    check_platforms(found, list(pushed))
    if found != pushed:
        raise ManifestError(f"index digests {found} differ from the pushed {pushed}")
    return found


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 4 and argv[1] == "verify":
            index = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
            found = verify(index, pushed_digests(Path(argv[3])))
            print(f"OK: {argv[2]} describes {', '.join(sorted(found))} with the pushed digests")
            return 0
        if len(argv) >= 4 and argv[1] == "children":
            found = platform_digests(json.loads(Path(argv[2]).read_text(encoding="utf-8")))
            check_platforms(found, argv[3:])
            for platform in sorted(found):
                print(f"{platform} {found[platform]}")
            return 0
    except (ManifestError, ValueError, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
