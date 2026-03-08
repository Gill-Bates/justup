#!/usr/bin/env python3
#
# app/utils/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup banner for justUp."""

from __future__ import annotations

import os
import sys
import tempfile

from .version import VERSION, BUILD_INFO

_BANNER_LOCK_FILE = os.path.join(tempfile.gettempdir(), "justup_banner.lock")


def print_banner() -> None:
	build_short = BUILD_INFO[:7] if BUILD_INFO else "dev"

	ascii_art = r"""
   _           _     _   _       _ 
  (_)_   _ ___| |_  | | | |_ __ | |
  | | | | / __| __| | | | | '_ \| |
  | | |_| \__ \ |_  | |_| | |_) |_|
 _/ |\__,_|___/\__|  \___/| .__/(_)
|__/                      |_|        
""".strip("\n")

	text_lines = [
		f"Uptime Monitor  v{VERSION} ({build_short})",
		"(C) 2026 by Gill-Bates (https://github.com/Gill-Bates/justup)",
	]

	ascii_lines = ascii_art.splitlines()
	ascii_width = max((len(l) for l in ascii_lines), default=0)
	text_width = max((len(t) for t in text_lines), default=0)
	master_width = max(ascii_width, text_width)

	left_pad = max((master_width - ascii_width) // 2, 0)
	pad = " " * left_pad
	ascii_centered = "\n".join(pad + line for line in ascii_lines)
	text_centered = [t.center(master_width) for t in text_lines]
	banner = "\n" + "\n".join([ascii_centered, *text_centered]) + "\n"

	if sys.stdout.isatty():
		green = "\033[92m"
		reset = "\033[0m"
		sys.stdout.write(green + banner + reset + "\n")
	else:
		sys.stdout.write(banner + "\n")


def print_banner_once() -> None:
	try:
		import fcntl

		fd = os.open(_BANNER_LOCK_FILE, os.O_CREAT | os.O_RDWR)
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			print_banner()
		except BlockingIOError:
			pass
		finally:
			os.close(fd)
	except (ImportError, OSError):
		print_banner()
