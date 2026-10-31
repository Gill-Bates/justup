#!/usr/bin/env python3
#
# app/utils/version.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Version and build information for justUp!.

pyproject.toml is the single source of truth for the version: the release
workflow checks the git tag against it, and the image bundles it.
"""

from __future__ import annotations

import logging
import tomllib
from functools import lru_cache
from importlib import metadata
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "justUp!"
_PROJECT_ROOT = Path(__file__).absolute().parent.parent.parent
_DIST_NAME = "justup"  # pyproject distribution name, for the installed fallback


def _version_from_pyproject() -> str | None:
    """Return ``[project] version`` from pyproject.toml, or None."""
    path = _PROJECT_ROOT / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        logger.debug("Unable to read %s: %s", path, exc)
        return None
    except tomllib.TOMLDecodeError as exc:
        logger.warning("pyproject.toml is not valid TOML (%s); version falls back", exc)
        return None
    version = data.get("project", {}).get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()
    logger.warning("pyproject.toml has no [project] version entry")
    return None


@lru_cache(maxsize=1)
def get_version() -> str:
    """Version from pyproject.toml, then installed distribution metadata, then 'dev'."""
    version = _version_from_pyproject()
    if version:
        return version
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:
        logger.debug("%s is not installed as a distribution; version is 'dev'", _DIST_NAME)
        return "dev"


@lru_cache(maxsize=1)
def get_build_info() -> str:
    """Build info (git commit) from the optional BUILD_INFO file, or 'dev'."""
    try:
        return (_PROJECT_ROOT / "BUILD_INFO").read_text(encoding="utf-8").strip() or "dev"
    except (OSError, UnicodeDecodeError):
        return "dev"


VERSION = get_version()
BUILD_INFO = get_build_info()
