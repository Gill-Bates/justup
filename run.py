#!/usr/bin/env python3
#
# run.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Developer/operator entry point: delegate to the app's uvicorn launcher."""

import sys

from app.__main__ import main

if __name__ == "__main__":
    sys.exit(main() or 0)
