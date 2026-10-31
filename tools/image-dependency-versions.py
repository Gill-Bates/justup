#!/usr/bin/env python3
#
# tools/image-dependency-versions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Print the installed versions of justUp's direct dependencies as key=value lines.

pyproject.toml lists requirement expressions (``cryptography>=50.0.2``,
``name[extra]; marker``, ``name @ url``), but importlib.metadata.version() wants a
distribution name. Each expression is therefore parsed with
packaging.requirements.Requirement first; the version then comes from the
installed distribution. docker-build.yml pipes this script into the finished image
(``python - < tools/image-dependency-versions.py``), where /src/pyproject.toml lives.

Usage: image-dependency-versions.py [path/to/pyproject.toml]
"""

from __future__ import annotations

import platform
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

DEFAULT_PYPROJECT = Path("/src/pyproject.toml")


def distribution_name(requirement: str) -> str:
    """Return the normalized distribution name of a PEP 508 requirement expression."""
    return canonicalize_name(Requirement(requirement).name)


def collect(pyproject: Path) -> list[str]:
    """Return ``key=value`` lines for the interpreter, the app and every direct dependency."""
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    lines = [f"python={platform.python_version()}", f"app_version={project['version']}"]
    for expression in project["dependencies"]:
        name = distribution_name(expression)
        lines.append(f"{name}={metadata.version(name)}")
    return lines


def main(argv: list[str]) -> int:
    pyproject = Path(argv[1]) if len(argv) > 1 else DEFAULT_PYPROJECT
    print("\n".join(collect(pyproject)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
