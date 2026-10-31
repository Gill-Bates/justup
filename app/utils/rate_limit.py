#!/usr/bin/env python3
#
# app/utils/rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Rate limiting configuration using slowapi.

Provides:
- Global limiter instance
- Default rate limits for different endpoint types
- Key function for identifying clients

Environment Variables:
- Auth rate limit: Settings (rate_limit_auth, default 10/minute)
  Set to higher value (e.g., 1000/minute) for testing
"""

from __future__ import annotations

import re

from slowapi import Limiter

from .client_ip import resolve_trusted_client_ip
from .constants import RATE_LIMIT_AUTH_DEFAULT, SettingKeys
from .runtime_settings import get_runtime_setting

# Global limiter instance - uses client IP as key
limiter = Limiter(key_func=resolve_trusted_client_ip)

# Rate limit constants (auth limit is configurable in Settings)
RATE_LIMIT_DEFAULT = "60/minute"  # General API endpoints
_AUTH_LIMIT_RE = re.compile(r"^[1-9][0-9]{0,5}/(second|minute|hour|day)$")


def RATE_LIMIT_AUTH() -> str:  # noqa: N802 - slowapi accepts a callable, evaluated per request
    """Login/auth limit from Settings (UI); invalid stored values fall back to the default."""
    value = str(get_runtime_setting(SettingKeys.RATE_LIMIT_AUTH, RATE_LIMIT_AUTH_DEFAULT) or "")
    return value if _AUTH_LIMIT_RE.match(value) else RATE_LIMIT_AUTH_DEFAULT


RATE_LIMIT_HEAVY = "10/minute"  # Heavy operations (backup, restore, purge)
RATE_LIMIT_PDF = "5/minute"  # PDF generation (CPU-intensive, very strict)
