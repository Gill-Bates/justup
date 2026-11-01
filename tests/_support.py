#!/usr/bin/env python3
#
# tests/_support.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared helpers for the unittest-style test modules."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
