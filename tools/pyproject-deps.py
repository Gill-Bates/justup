#!/usr/bin/env python3
#
# tools/pyproject-deps.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Print dependencies declared in pyproject.toml, one per line.

pyproject.toml is the single dependency manifest. CI pipelines call this to
materialise a requirements file for a given dependency set without adding a
second source of truth.

Usage:
    pyproject-deps.py                 # print [project.dependencies]
    pyproject-deps.py <extra>         # print [project.optional-dependencies.<extra>]

The output is requirements.txt-formatted (one requirement specifier per line)
and is written to stdout, so callers redirect it into a file.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def _load_pyproject() -> dict:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():
        sys.exit(f"error: {pyproject} not found")
    with pyproject.open("rb") as fh:
        return tomllib.load(fh)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        sys.exit("usage: pyproject-deps.py [extra]")

    data = _load_pyproject()
    project = data.get("project", {})

    if not argv:
        deps = project.get("dependencies", [])
    else:
        extra = argv[0]
        optional = project.get("optional-dependencies", {})
        if extra not in optional:
            available = ", ".join(sorted(optional)) or "(none)"
            sys.exit(f"error: no optional-dependencies group '{extra}'. available: {available}")
        deps = optional[extra]

    for dep in deps:
        print(dep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
