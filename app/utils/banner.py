#!/usr/bin/env python3
#
# app/utils/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Banner Module.
Displays the application header/logo.
"""

from __future__ import annotations

import sys

from .version import VERSION as _DEFAULT_VERSION
from .version import get_build_info


def print_banner(version: str | None = None) -> None:
    """
    Prints the ASCII banner to stdout and automatically centers the text below it
    based on the banner's maximum line width.
    """
    if version is None:
        version = _DEFAULT_VERSION
    # Get build info (short hash)
    build_info = get_build_info()
    if build_info and build_info != "dev":
        build_info = build_info[:7]  # Short hash

    # Check if we have color support
    is_tty = sys.stdout.isatty()

    if is_tty:
        cyan = "\033[96m"
        white = "\033[97m"
        gray = "\033[90m"
        reset = "\033[0m"
        bold = "\033[1m"
    else:
        cyan = white = gray = reset = bold = ""

    banner = r"""
   _           _     _   _       _ 
  (_)_   _ ___| |_  | | | |_ __ | |
  | | | | / __| __| | | | | '_ \| |
  | | |_| \__ \ |_  | |_| | |_) |_|
 _/ |\__,_|___/\__|  \___/| .__/(_)
|__/                      |_|      
    """

    lines = [line for line in banner.split("\n") if line]
    banner_width = max((len(line) for line in lines), default=40)

    title_text = ">>> Uptime Monitor - done right! <<<"
    version_text = f"v{version} ({build_info}) | by Gill-Bates"

    print(f"{cyan}{bold}{banner}{reset}")
    print(f"{white}{title_text.center(banner_width)}{reset}")
    print(f"{gray}{version_text.center(banner_width)}{reset}")
    print()
