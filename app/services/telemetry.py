#!/usr/bin/env python3
#
# app/services/telemetry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Telemetry service wrapper.

Re-exports from compiled Cython module for external use.
"""

from __future__ import annotations

from app._cython.telemetry import (
    telemetry_loop,
    collect_status_data,
    submit_status,
)

__all__ = [
    "telemetry_loop",
    "collect_status_data",
    "submit_status",
]
