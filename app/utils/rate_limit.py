#!/usr/bin/env python3
#
# app/utils/rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Rate limiting configuration using slowapi."""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

RATE_LIMIT_DEFAULT = "60/minute"
RATE_LIMIT_AUTH = "5/minute"
RATE_LIMIT_HEAVY = "10/minute"
RATE_LIMIT_API = "120/minute"
RATE_LIMIT_CRITICAL = "3/minute"

limiter = Limiter(key_func=get_remote_address)
