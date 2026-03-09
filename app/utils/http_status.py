#!/usr/bin/env python3
#
# app/utils/http_status.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Helpers for parsing and matching HTTP status code specs.

Used for configuring which HTTP response codes count as "UP" for the HTTP check.

Supported formats (comma/space separated):
  - Single codes: "200", "302"
  - Ranges: "200-399"
  - Classes: "2xx", "3xx"
  - Class ranges: "2xx-4xx"
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = [
    "DEFAULT_HTTP_SUCCESS_CODES",
    "is_status_code_allowed",
    "parse_status_code_spec",
]

DEFAULT_HTTP_SUCCESS_CODES = "200-399"

# Safety limits
MAX_TOKENS = 50  # Prevent memory exhaustion from specs like "200,201,202,...,599"

_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")
_CLASS_RE = re.compile(r"^([1-5])xx$", re.IGNORECASE)
_CLASS_RANGE_RE = re.compile(r"^([1-5])xx-([1-5])xx$", re.IGNORECASE)
_NUM_RE = re.compile(r"^(\d{3})$")
_NUM_RANGE_RE = re.compile(r"^(\d{3})-(\d{3})$")


def is_status_code_allowed(code: int, spec: str) -> bool:
    """Return True if HTTP status code matches the spec.
    
    Examples:
        >>> is_status_code_allowed(200, "2xx")
        True
        >>> is_status_code_allowed(404, "200-399")
        False
        >>> is_status_code_allowed(301, "200, 301, 404")
        True
    
    Args:
        code: HTTP status code (coerced to int if needed)
        spec: Status code specification string
    
    Returns:
        True if code matches spec, False otherwise
    """
    # Type guard: coerce to int (handles string inputs gracefully)
    code = int(code)
    ranges = parse_status_code_spec(spec)
    return any(start <= code <= end for start, end in ranges)


def parse_status_code_spec(spec: str) -> tuple[tuple[int, int], ...]:
    """Parse a status code spec into merged inclusive ranges.
    
    Examples:
        >>> parse_status_code_spec("200")
        ((200, 200),)
        >>> parse_status_code_spec("2xx")
        ((200, 299),)
        >>> parse_status_code_spec("200-399, 404")
        ((200, 404),)
    
    Args:
        spec: Status code specification string
    
    Returns:
        Tuple of (start, end) inclusive ranges, sorted and merged
    
    Raises:
        TypeError: If spec is None
        ValueError: If spec is empty, invalid, or has too many tokens
    """
    if spec is None:
        raise TypeError("spec must be a string, not None")
    return _parse_status_code_spec_cached(str(spec).strip())


@lru_cache(maxsize=256)
def _parse_status_code_spec_cached(spec: str) -> tuple[tuple[int, int], ...]:
    """Internal cached parser (note: failed calls are not cached by lru_cache).
    
    Cache poisoning risk: Sending 256+ unique invalid specs will evict valid entries.
    This is acceptable since invalid specs fail fast and don't impact correctness.
    """
    if not spec:
        raise ValueError("empty spec")

    tokens = [t for t in _TOKEN_SPLIT_RE.split(spec) if t]
    
    # Safety: Limit token count to prevent memory exhaustion
    if len(tokens) > MAX_TOKENS:
        raise ValueError(f"too many tokens ({len(tokens)} > {MAX_TOKENS})")
    
    ranges: list[tuple[int, int]] = []

    for raw in tokens:
        t = raw.strip()
        if not t:
            continue

        m = _CLASS_RANGE_RE.match(t)
        if m:
            a = int(m.group(1))
            b = int(m.group(2))
            if a > b:
                raise ValueError(f"invalid class range: {t}")
            if a == b:
                # "2xx-2xx" is redundant, probably user error (should be "2xx")
                # Allow but could log warning in production
                pass
            ranges.append((a * 100, b * 100 + 99))
            continue

        m = _CLASS_RE.match(t)
        if m:
            a = int(m.group(1))
            ranges.append((a * 100, a * 100 + 99))
            continue

        m = _NUM_RANGE_RE.match(t)
        if m:
            start = int(m.group(1))
            end = int(m.group(2))
            if start > end:
                raise ValueError(f"invalid range: {t}")
            if start < 100 or end > 599:
                raise ValueError(f"status code out of range: {t}")
            ranges.append((start, end))
            continue

        m = _NUM_RE.match(t)
        if m:
            code = int(m.group(1))
            if code < 100 or code > 599:
                raise ValueError(f"status code out of range: {code}")
            ranges.append((code, code))
            continue

        raise ValueError(f"invalid token: {t}")

    return _merge_ranges(ranges)


def _merge_ranges(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Merge overlapping/adjacent inclusive ranges.
    
    Note: Range validation (100-599) is already done in caller,
    so this function assumes inputs are pre-validated.
    """
    if not ranges:
        raise ValueError("no codes specified")

    ranges_sorted = sorted(ranges, key=lambda r: (r[0], r[1]))
    merged: list[list[int]] = []
    for start, end in ranges_sorted:
        # Redundant validation removed (already checked in _parse_status_code_spec_cached)
        # Keeping as defensive programming for standalone usage
        if not merged:
            merged.append([start, end])
            continue
        last = merged[-1]
        if start <= last[1] + 1:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])

    # Remove redundant int() cast - a and b are already int
    return tuple((a, b) for a, b in merged)
