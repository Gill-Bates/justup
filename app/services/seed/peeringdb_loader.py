#!/usr/bin/env python3
#
# app/services/seed/peeringdb_loader.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Seed pop_locations from PeeringDB API – actual facility and IX locations.

This is the highest-quality source for network infrastructure locations.
API: https://www.peeringdb.com/api/
Rate limit: Be respectful, cache aggressively (7-day TTL).

PeeringDB data is licensed under CC BY-SA 4.0.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

PEERINGDB_FACILITIES_URL = "https://www.peeringdb.com/api/fac"
PEERINGDB_IX_URL = "https://www.peeringdb.com/api/ix"

# Use app-specific cache directory with user isolation for security
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/app/data/cache/peeringdb"))
_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
_MAX_STALE_AGE_SECONDS = 30 * 24 * 3600  # 30 days - max age for stale cache fallback
_MAX_RESPONSE_SIZE = 100 * 1024 * 1024  # 100MB - prevent memory exhaustion
_MAX_RETRIES = 2  # HTTP fetch retry attempts
_MAX_CITY_LEN = 64  # VARCHAR(64) column limit

# Priority levels (lower = higher priority in lookups)
PEERINGDB_FAC_PRIORITY = 20
PEERINGDB_IX_PRIORITY = 25
PEERINGDB_FAC_CONFIDENCE = 80
PEERINGDB_IX_CONFIDENCE = 75

_BATCH_SIZE = 500

# Words that appear in facility/IX names but are NOT POP codes
# Note: 2-letter codes are unreachable by current regex patterns (min 3 chars)
_FACILITY_NAME_NOISE = frozenset({
    # Generic English
    "THE", "AND", "FOR", "NET", "INC", "LLC", "LTD", "PLC", "NTT",
    "NAP", "POP", "NOC", "SOC", "COL", "HUB", "ONE", "TWO",
    # IX suffixes (these are part of the IX name, not location codes)
    "CIX", "PIX", "NIX", "TIX", "MIX", "AIX", "SIX", "BIX", "GIX",
    # Provider brand names that look like codes
    "AWS", "OVH", "IBM", "GTT", "OPT", "INT", "COM", "TEL", "SAT",
})


# ---------------------------------------------------------------------------
# Cache-aware HTTP fetch
# ---------------------------------------------------------------------------


def _cached_fetch(url: str, name: str, timeout: int = 30) -> dict:
    """
    Fetch URL with local file-based caching and atomic writes.

    Args:
        url: API endpoint URL
        name: Cache file identifier (used as filename stem, alphanumeric only)
        timeout: HTTP request timeout in seconds

    Returns:
        Parsed JSON response

    Raises:
        ValueError: If cache name contains unsafe characters
        OSError: If fetch fails and no valid cache exists
    """
    # Validate cache name to prevent path traversal
    if not re.fullmatch(r"[a-z0-9_-]+", name):
        raise ValueError(f"Unsafe cache name: {name!r}")
    
    # Restrict cache directory permissions (owner-only)
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_file = CACHE_DIR / f"{name}.json"

    # Check cache freshness
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < _CACHE_MAX_AGE_SECONDS:
            _log.debug(
                "Using cached PeeringDB data: %s (age: %.1f hours)",
                name,
                age / 3600,
            )
            with cache_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        _log.info(
            "PeeringDB cache expired: %s (%.1f days old)", name, age / 86400
        )

    _log.info("Fetching PeeringDB data: %s", url)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "justUp/1.0 (network monitoring; POP seeding)",
        },
    )

    # Explicit TLS context for certificate validation
    ssl_context = ssl.create_default_context()
    
    # Initialize response variables before retry loop to prevent UnboundLocalError
    raw: bytes | None = None
    data: dict | None = None
    
    # Retry logic with exponential backoff
    # Note: time.sleep() blocks the thread - acceptable for CLI seeding, but not for async contexts
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                # Validate HTTP status
                if resp.status != 200:
                    raise OSError(f"PeeringDB returned HTTP {resp.status}")
                
                # Early gate: check Content-Length header to avoid allocating memory for oversized responses
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > _MAX_RESPONSE_SIZE:
                    raise OSError(f"PeeringDB response too large: {content_length} bytes (limit: {_MAX_RESPONSE_SIZE})")
                
                # Read response with size limit
                raw = resp.read(_MAX_RESPONSE_SIZE + 1)
                if len(raw) > _MAX_RESPONSE_SIZE:
                    raise OSError(f"PeeringDB response too large: {len(raw)} bytes")
                
                data = json.loads(raw)
                
                # Validate response structure
                if not isinstance(data.get("data"), list):
                    raise OSError(
                        f"Unexpected PeeringDB response structure: {list(data.keys())}"
                    )
                
                break  # Success
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
            if attempt < _MAX_RETRIES:
                _log.warning(
                    "PeeringDB fetch attempt %d/%d failed: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    e,
                )
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s
            else:
                # Final failure - check stale cache
                if cache_file.exists():
                    stale_age = time.time() - cache_file.stat().st_mtime
                    if stale_age > _MAX_STALE_AGE_SECONDS:
                        raise OSError(
                            f"PeeringDB cache too stale ({stale_age/86400:.0f} days) "
                            f"and fetch failed: {e}"
                        ) from e
                    _log.warning(
                        "PeeringDB fetch failed after %d retries (%s), using stale cache "
                        "(%.1f days old): %s",
                        _MAX_RETRIES + 1,
                        e,
                        stale_age / 86400,
                        cache_file,
                    )
                    with cache_file.open("r", encoding="utf-8") as f:
                        return json.load(f)
                raise OSError(
                    f"PeeringDB fetch failed after {_MAX_RETRIES + 1} retries and no cache exists: {e}"
                ) from e

    # Explicit assertion for type checker and runtime safety
    assert raw is not None and data is not None, "Loop exited without setting raw/data"
    
    # Atomic write with unique temp file to prevent concurrent corruption
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=CACHE_DIR,
        prefix=f"{name}_",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    
    # Atomic rename (POSIX guarantees atomicity even if destination exists)
    os.replace(tmp_path, cache_file)

    record_count = len(data.get("data", []))
    _log.info(
        "PeeringDB data cached: %s (%d records, %d bytes)",
        name,
        record_count,
        len(raw),
    )
    return data


# ---------------------------------------------------------------------------
# Facility name → POP code extraction
# ---------------------------------------------------------------------------


def _extract_facility_codes(name: str) -> list[str]:
    """
    Extract POP-like codes from facility or IX names.

    Examples:
        "Equinix FR5"     → ["fr5"]
        "DE-CIX FRA"      → ["fra"]  (not "de")
        "Interxion AMS7"  → ["ams7", "ams"]
        "NTT Frankfurt 1" → []  (full city names handled elsewhere)

    Returns:
        Deduplicated list of candidate POP codes (lowercase, 3-6 chars).
        Codes shorter than 3 chars are excluded to avoid country code pollution.
    """
    codes: list[str] = []
    seen: set[str] = set()
    name_upper = name.upper()

    # Pattern: "XX1" or "XXX1" or "XXXX12" at word boundary
    # Matches: FR5, AMS1, FRA15, EQDC10
    for m in re.finditer(r"\b([A-Z]{2,4}\d{1,2})\b", name_upper):
        candidate = m.group(1).lower()
        # Must be at least 3 chars total to avoid "DE5" → "de5" noise
        if len(candidate) >= 3 and candidate not in seen:
            seen.add(candidate)
            codes.append(candidate)

    # Pattern: standalone 3-letter alpha codes (FRA, AMS, SIN)
    # Minimum 3 chars to exclude country codes (DE, NL, US)
    for m in re.finditer(r"\b([A-Z]{3})\b", name_upper):
        candidate = m.group(1)
        if candidate not in _FACILITY_NAME_NOISE and candidate.lower() not in seen:
            seen.add(candidate.lower())
            codes.append(candidate.lower())

    return codes


# ---------------------------------------------------------------------------
# Coordinate validation
# ---------------------------------------------------------------------------


def _validate_coords(
    lat: object, lon: object
) -> tuple[float, float] | None:
    """
    Validate and normalize coordinates.

    Returns (lat, lon) rounded to 6 decimal places, or None if invalid.
    Rejects null-island (0.0, 0.0) as likely missing data.
    """
    try:
        flat = float(lat)  # type: ignore[arg-type]
        flon = float(lon)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None

    if flat == 0.0 and flon == 0.0:
        return None

    if not (-90 <= flat <= 90 and -180 <= flon <= 180):
        return None

    return round(flat, 6), round(flon, 6)


# ---------------------------------------------------------------------------
# Common seeding logic
# ---------------------------------------------------------------------------


def _seed_peeringdb_source(
    conn: sqlite3.Connection,
    url: str,
    cache_name: str,
    source_tag: str,
    priority: int,
    confidence: int,
) -> int:
    """
    Common import logic for PeeringDB data sources.
    
    Requires pop_locations table with UNIQUE constraint on (alias_code, source)
    to enable INSERT OR IGNORE deduplication.
    
    Args:
        conn: SQLite connection with pop_locations table
        url: API endpoint URL
        cache_name: Cache file identifier
        source_tag: Value for 'source' column
        priority: Priority level for lookups
        confidence: Confidence score (0-100)
    
    Returns:
        Number of records actually inserted
    """
    data = _cached_fetch(url, cache_name)
    now = datetime.now(timezone.utc).isoformat()
    
    # Count existing records before insertion for accurate delta
    count_before = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE source = ?",
        (source_tag,)
    ).fetchone()[0]

    sql = """
        INSERT OR IGNORE INTO pop_locations
            (canonical_code, alias_code, city, country, lat, lon,
             priority, confidence, source, created_at, updated_at, retired)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """

    batch: list[tuple] = []
    skipped_no_coords = 0
    skipped_no_codes = 0
    skipped_invalid_country = 0
    total_attempted = 0

    for entry in data.get("data", []):
        city = entry.get("city", "").strip()
        country = entry.get("country", "").strip().upper()
        name = entry.get("name", "")

        if not city:
            continue

        # Strict country validation: ISO 3166-1 alpha-2 (exactly 2 letters)
        if not country or len(country) != 2 or not country.isalpha():
            skipped_invalid_country += 1
            continue

        coords = _validate_coords(entry.get("latitude"), entry.get("longitude"))
        if coords is None:
            skipped_no_coords += 1
            continue

        lat, lon = coords
        codes = _extract_facility_codes(name)

        if not codes:
            skipped_no_codes += 1
            continue

        # First code is canonical, all codes are aliases
        canonical = codes[0]
        for code in codes:
            # Maximum length enforced by schema (note: len > 8 is unreachable given current regex)
            batch.append((
                canonical,  # canonical_code
                code,       # alias_code
                city[:_MAX_CITY_LEN],
                country,
                lat,
                lon,
                priority,
                confidence,
                source_tag,
                now,
                now,
            ))
            total_attempted += 1

            if len(batch) >= _BATCH_SIZE:
                conn.executemany(sql, batch)
                batch.clear()

    if batch:
        conn.executemany(sql, batch)

    conn.commit()

    # Calculate actual inserts by comparing before and after counts
    count_after = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE source = ?",
        (source_tag,)
    ).fetchone()[0]
    inserted = count_after - count_before

    _log.info(
        "PeeringDB %s: %d inserted of %d attempted "
        "(skipped: %d no coords, %d no codes, %d invalid country)",
        cache_name,
        inserted,
        total_attempted,
        skipped_no_coords,
        skipped_no_codes,
        skipped_invalid_country,
    )
    return inserted


# ---------------------------------------------------------------------------
# Facility seeding
# ---------------------------------------------------------------------------


def seed_peeringdb_facilities(conn: sqlite3.Connection) -> int:
    """
    Import PeeringDB facility locations as high-priority POP candidates.

    Facilities have exact coordinates and standardized naming.
    Many use IATA-derived codes (e.g., "FRA15", "AMS-IX").

    Entries without valid coordinates are skipped entirely.

    Args:
        conn: SQLite connection with pop_locations table

    Returns:
        Number of records actually inserted
    """
    return _seed_peeringdb_source(
        conn,
        PEERINGDB_FACILITIES_URL,
        "facilities",
        "peeringdb_fac",
        PEERINGDB_FAC_PRIORITY,
        PEERINGDB_FAC_CONFIDENCE,
    )


# ---------------------------------------------------------------------------
# IXP seeding
# ---------------------------------------------------------------------------


def seed_peeringdb_ixps(conn: sqlite3.Connection) -> int:
    """
    Import IXP (Internet Exchange Point) locations.

    IXP names almost always contain the city POP code.
    IXPs without coordinates are skipped – entries with (0.0, 0.0)
    would pollute downstream lookups. These locations are typically
    covered by IATA or facility data anyway.

    Args:
        conn: SQLite connection with pop_locations table

    Returns:
        Number of records actually inserted
    """
    return _seed_peeringdb_source(
        conn,
        PEERINGDB_IX_URL,
        "ixps",
        "peeringdb_ix",
        PEERINGDB_IX_PRIORITY,
        PEERINGDB_IX_CONFIDENCE,
    )


__all__ = ["seed_peeringdb_facilities", "seed_peeringdb_ixps"]
