#!/usr/bin/env python3
#
# app/utils/time.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""UTC time helpers used across the application.

Policy: UTC as single source of truth.
- All datetimes MUST be timezone-aware.
- Naive datetimes are rejected (ValueError).
- All returns are normalized to UTC.
"""

import logging
from datetime import datetime, timezone

__all__ = ["utcnow", "ensure_utc", "parse_utc"]

_log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """
    Get the current time in UTC, timezone-aware.
    This is the single source of truth for 'now' in the application.
    """
    return datetime.now(tz=timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """
    Ensure a datetime object is timezone-aware and in UTC.
    
    Raises:
        TypeError: If input is not a datetime instance.
        ValueError: If the input is naive (no timezone info).
    """
    if not isinstance(dt, datetime):
        raise TypeError(f"ensure_utc expects datetime, got {type(dt).__name__}")
    if dt.tzinfo is None:
        raise ValueError("Naive timestamp is not allowed; input must be timezone-aware.")
    return dt.astimezone(timezone.utc)


def parse_utc(value: str | None, *, strict: bool = False) -> datetime | None:
    """
    Parse an ISO-8601 string into a UTC timezone-aware datetime.
    
    Args:
        value: ISO-8601 formatted string (e.g., "2025-01-19T12:00:00Z").
        strict: If True, raise exceptions instead of returning None on errors.
    
    Returns:
        Timezone-aware datetime in UTC, or None if:
        - input is None or empty
        - parsing fails (invalid format)
        - parsed datetime is naive (no timezone)
    
    Raises:
        TypeError: If value is not str or None.
        ValueError: If strict=True and parsing fails or result is naive.
    """
    if not value:
        return None
    
    # Type check: TypeError is a programming bug, not expected user input
    if not isinstance(value, str):
        raise TypeError(f"parse_utc expects str or None, got {type(value).__name__}")
    
    try:
        cleaned = value.strip()
        
        # Handle 'Z' suffix (JS/ISO format) → replace only suffix, not arbitrary Z occurrences
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        
        dt = datetime.fromisoformat(cleaned)
        
        # Strict UTC policy: naive datetimes are not allowed
        if dt.tzinfo is None:
            if strict:
                raise ValueError("Naive timestamp is not allowed; input must be timezone-aware.")
            _log.debug("Rejecting naive datetime from '%s' - timezone required", value)
            return None
        
        return dt.astimezone(timezone.utc)
    
    except ValueError as e:
        _log.debug("Failed to parse UTC timestamp '%s': %s", value, e)
        if strict:
            raise
        return None

