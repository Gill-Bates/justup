# cython: language_level=3
# Safety checks controlled by setup_cython.py (_SAFETY_CRITICAL list)
# Do NOT add boundscheck/wraparound/cdivision directives here!

"""
Rate limiting configuration using slowapi.

Provides:
- Global limiter instance
- Default rate limits for different endpoint types
- Key function for identifying clients

Environment Variables:
- UPMON_RATE_LIMIT_AUTH: Override auth rate limit (default: 10/minute)
  Set to higher value (e.g., 1000/minute) for testing
"""

from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Global limiter instance - uses client IP as key
limiter = Limiter(key_func=get_remote_address)

# Rate limit constants (can be overridden via environment)
RATE_LIMIT_DEFAULT = "60/minute"  # General API endpoints
RATE_LIMIT_AUTH = os.getenv("UPMON_RATE_LIMIT_AUTH", "10/minute")  # Login/auth
RATE_LIMIT_HEAVY = "10/minute"  # Heavy operations (backup, restore, purge)
RATE_LIMIT_METRICS = "120/minute"  # Metrics queries (higher for charts)
RATE_LIMIT_PDF = "5/minute"  # PDF generation (CPU-intensive, very strict)

