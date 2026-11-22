#!/usr/bin/env python3
#
# app/utils/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup banner: the justUp! logo on top of the shared banner renderer (colour only on a TTY)."""

from __future__ import annotations

import gillbates_logging

from .version import VERSION as _DEFAULT_VERSION
from .version import get_build_info

TITLE = ">>> Uptime Monitor - done right! <<<"

_LOGO = r"""
   _           _     _   _       _
  (_)_   _ ___| |_  | | | |_ __ | |
  | | | | / __| __| | | | | '_ \| |
  | | |_| \__ \ |_  | |_| | |_) |_|
 _/ |\__,_|___/\__|  \___/| .__/(_)
|__/                      |_|
"""


def print_banner(version: str | None = None) -> None:
    """Print the centred logo, title and version line to stdout."""
    if version is None:
        version = _DEFAULT_VERSION
    build_info = get_build_info()
    if build_info and build_info != "dev":
        build_info = build_info[:7]
    gillbates_logging.print_banner(_LOGO, TITLE, (f"v{version} ({build_info}) | by Gill-Bates",))
