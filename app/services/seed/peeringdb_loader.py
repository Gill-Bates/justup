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
from datetime import UTC, datetime
from pathlib import Path

from ._fetch import atomic_write_bytes, open_no_redirect

_log = logging.getLogger(__name__)

PEERINGDB_FACILITIES_URL = "https://www.peeringdb.com/api/fac"
PEERINGDB_IX_URL = "https://www.peeringdb.com/api/ix"

# Use app-specific cache directory with user isolation for security
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/app/data/cache/peeringdb"))
_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
_MAX_STALE_AGE_SECONDS = 30 * 24 * 3600  # 30 days - max age for stale cache fallback
_MAX_RESPONSE_SIZE = 100 * 1024 * 1024  # 100MB - prevent memory exhaustion
_MAX_RETRIES = 2  # HTTP fetch retry attempts

# Priority levels (lower = higher priority in lookups)
PEERINGDB_FAC_PRIORITY = 20
PEERINGDB_IX_PRIORITY = 25
PEERINGDB_FAC_CONFIDENCE = 80
PEERINGDB_IX_CONFIDENCE = 75

_BATCH_SIZE = 500

# Words that appear in facility/IX names but are NOT POP codes
_FACILITY_NAME_NOISE = frozenset(
    {
        # Generic English
        "THE",
        "AND",
        "FOR",
        "NET",
        "INC",
        "LLC",
        "LTD",
        "PLC",
        "NTT",
        "NAP",
        "POP",
        "NOC",
        "SOC",
        "COL",
        "HUB",
        "ONE",
        "TWO",
        # IX suffixes (these are part of the IX name, not location codes)
        "CIX",
        "PIX",
        "NIX",
        "TIX",
        "MIX",
        "AIX",
        "SIX",
        "BIX",
        "GIX",
        # Country codes that leak from compound names like "DE-CIX"
        "DE",
        "NL",
        "UK",
        "US",
        "FR",
        "AT",
        "CH",
        "IT",
        "ES",
        "SE",
        "NO",
        "DK",
        "FI",
        "PL",
        "CZ",
        "HU",
        "RO",
        "BG",
        "HR",
        "SK",
        "PT",
        "IE",
        "BE",
        "LU",
        "GR",
        "JP",
        "KR",
        "SG",
        "HK",
        "TW",
        "AU",
        "NZ",
        "BR",
        "MX",
        "CA",
        "IN",
        "ZA",
        "AE",
        "IL",
        # Provider brand names that look like codes
        "AWS",
        "OVH",
        "IBM",
        "GTT",
        "OPT",
        "INT",
        "COM",
        "TEL",
        "SAT",
    }
)


# ---------------------------------------------------------------------------
# Cache-aware HTTP fetch
# ---------------------------------------------------------------------------


def _load_cache_json(cache_file: Path) -> dict | None:
    """Return the parsed cache file, or ``None`` when it is unreadable or not a PeeringDB payload."""
    try:
        with cache_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    return data if isinstance(data, dict) and isinstance(data.get("data"), list) else None


def _cached_fetch(url: str, name: str, timeout: int = 30) -> dict:
    """
    Fetch URL with local file-based caching and atomic writes.

    Args:
        url: API endpoint URL
        name: Cache file identifier (used as filename stem)
        timeout: HTTP request timeout in seconds

    Returns:
        Parsed JSON response

    Raises:
        OSError: If fetch fails and no valid cache exists
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
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
            cached = _load_cache_json(cache_file)
            if cached is not None:
                return cached
            _log.warning("PeeringDB cache %s is unreadable, refetching", cache_file)
        _log.info("PeeringDB cache expired: %s (%.1f days old)", name, age / 86400)

    _log.info("Fetching PeeringDB data: %s", url)
    req = urllib.request.Request(  # noqa: S310 - fixed https PeeringDB URL constants, no user-controlled scheme
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "justUp/1.0 (network monitoring; POP seeding)",
        },
    )

    # Explicit TLS context for certificate validation
    ssl_context = ssl.create_default_context()

    # Retry logic with exponential backoff
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with open_no_redirect(req, timeout=timeout, context=ssl_context) as resp:
                # Validate HTTP status
                if resp.status != 200:
                    raise OSError(f"PeeringDB returned HTTP {resp.status}")

                # Limit response size to prevent memory exhaustion
                raw = resp.read(_MAX_RESPONSE_SIZE + 1)
                if len(raw) > _MAX_RESPONSE_SIZE:
                    raise OSError(f"PeeringDB response too large: {len(raw)} bytes")

                data = json.loads(raw)

                # Validate response structure
                if not isinstance(data.get("data"), list):
                    raise OSError(f"Unexpected PeeringDB response structure: {list(data.keys())}")

                break  # Success
        except Exception as e:
            if attempt < _MAX_RETRIES:
                _log.warning(
                    "PeeringDB fetch attempt %d/%d failed: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    e,
                )
                time.sleep(2**attempt)  # Exponential backoff: 1s, 2s
            else:
                # Final failure - check stale cache
                if cache_file.exists():
                    stale_age = time.time() - cache_file.stat().st_mtime
                    if stale_age > _MAX_STALE_AGE_SECONDS:
                        raise OSError(
                            f"PeeringDB cache too stale ({stale_age / 86400:.0f} days) and fetch failed: {e}"
                        ) from e
                    _log.warning(
                        "PeeringDB fetch failed after %d retries (%s), using stale cache (%.1f days old): %s",
                        _MAX_RETRIES + 1,
                        e,
                        stale_age / 86400,
                        cache_file,
                    )
                    stale = _load_cache_json(cache_file)
                    if stale is None:
                        raise OSError(f"PeeringDB cache {cache_file} is corrupt and fetch failed: {e}") from e
                    return stale
                raise OSError(
                    f"PeeringDB fetch failed after {_MAX_RETRIES + 1} retries and no cache exists: {e}"
                ) from e

    # Atomic write: temp file + os.replace (cross-platform atomic)
    atomic_write_bytes(cache_file, raw)

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
        Deduplicated list of candidate POP codes (lowercase, 3-8 chars).
        2-char codes are excluded to avoid country code pollution.
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


def _validate_coords(lat: object, lon: object) -> tuple[float, float] | None:
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
    now = datetime.now(UTC).isoformat()

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
            if len(code) > 8:
                continue
            batch.append(
                (
                    canonical,  # canonical_code
                    code,  # alias_code
                    city[:64],
                    country,
                    lat,
                    lon,
                    priority,
                    confidence,
                    source_tag,
                    now,
                    now,
                )
            )
            total_attempted += 1

            if len(batch) >= _BATCH_SIZE:
                conn.executemany(sql, batch)
                batch.clear()

    if batch:
        conn.executemany(sql, batch)

    conn.commit()

    # Count actual inserts by matching created_at timestamp and source
    inserted = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE source = ? AND created_at = ?",
        (source_tag, now),
    ).fetchone()[0]

    _log.info(
        "PeeringDB %s: %d inserted of %d attempted (skipped: %d no coords, %d no codes, %d invalid country)",
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
