#!/usr/bin/env python3
#
# app/services/seed/iata_loader.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Seed pop_locations from IATA airport codes.

Source: https://raw.githubusercontent.com/ip2location/ip2location-iata-icao/master/iata-icao.csv
License: MIT

IATA codes are the de-facto standard for POP naming worldwide.
Most backbone carriers use 3-letter IATA codes: FRA, AMS, SIN, LAX, NRT...
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

IATA_URL = (
    "https://raw.githubusercontent.com/ip2location/ip2location-iata-icao/"
    "master/iata-icao.csv"
)

# Expected CSV columns (fail-fast if upstream changes format)
_REQUIRED_COLUMNS = frozenset({"iata", "latitude", "longitude", "city", "country_code"})

# Sanity check: IATA has ~9000 codes worldwide, warn if suspiciously low
_MIN_EXPECTED_CODES = 5000

# Priority 90: IATA codes are a fallback, manual entries take precedence
IATA_PRIORITY = 90
IATA_CONFIDENCE = 50

# Cache expires after 30 days
_CACHE_MAX_AGE_SECONDS = 30 * 24 * 3600
_MAX_STALE_AGE_SECONDS = 90 * 24 * 3600  # 90 days - max stale fallback
_MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10MB
_MAX_RETRIES = 2

# Batch size for executemany
_BATCH_SIZE = 500


def fetch_iata_csv(
    cache_path: Path | None = None,
    timeout: int = 30,
    max_age_seconds: int = _CACHE_MAX_AGE_SECONDS,
) -> Path:
    """
    Download IATA CSV with time-based cache invalidation.

    Args:
        cache_path: Local file path for caching (defaults to secure cache dir)
        timeout: HTTP request timeout in seconds
        max_age_seconds: Re-download if cache is older than this

    Returns:
        Path to the CSV file

    Raises:
        OSError: If download fails and no valid cache exists
    """
    # Use secure cache directory with user isolation
    if cache_path is None:
        cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / "iata-icao.csv"
    
    # Check cache freshness
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < max_age_seconds:
            _log.debug(
                "Using cached IATA data: %s (age: %.0f hours)",
                cache_path,
                age / 3600,
            )
            return cache_path
        _log.info("IATA cache expired (%.0f days old), refreshing", age / 86400)

    _log.info("Downloading IATA airport codes from %s", IATA_URL)
    
    # Explicit TLS context
    ssl_context = ssl.create_default_context()
    
    # Retry logic with exponential backoff
    last_error = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                IATA_URL,
                headers={"User-Agent": "justUp/1.0 (POP seeding)"},
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                # Validate HTTP status
                if resp.status != 200:
                    raise OSError(f"IATA download returned HTTP {resp.status}")
                
                # Limit response size
                data = resp.read(_MAX_RESPONSE_SIZE + 1)
                if len(data) > _MAX_RESPONSE_SIZE:
                    raise OSError(f"IATA response too large: {len(data)} bytes")
            
            # Atomic write: os.replace is atomic on all platforms
            tmp_path = cache_path.with_suffix(".tmp")
            tmp_path.write_bytes(data)
            os.replace(str(tmp_path), str(cache_path))

            _log.info("IATA data cached to %s (%d bytes)", cache_path, len(data))
            return cache_path
            
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                _log.warning(
                    "IATA download attempt %d/%d failed: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    e,
                )
                time.sleep(2 ** attempt)
            else:
                # Final failure - check stale cache
                if cache_path.exists():
                    stale_age = time.time() - cache_path.stat().st_mtime
                    if stale_age > _MAX_STALE_AGE_SECONDS:
                        raise OSError(
                            f"IATA cache too stale ({stale_age/86400:.0f} days) "
                            f"and download failed: {e}"
                        ) from e
                    _log.warning(
                        "IATA download failed after %d retries (%s), using stale cache "
                        "(%.0f days old): %s",
                        _MAX_RETRIES + 1,
                        e,
                        stale_age / 86400,
                        cache_path,
                    )
                    return cache_path
                raise OSError(
                    f"IATA download failed after {_MAX_RETRIES + 1} retries "
                    f"and no cache exists: {e}"
                ) from e

    # Should never reach here, but satisfy type checker
    raise OSError(f"IATA download failed: {last_error}")


def _parse_iata_rows(path: Path) -> list[tuple]:
    """
    Parse and validate IATA CSV rows.

    Returns:
        List of (iata, city, country, lat, lon) tuples

    Raises:
        ValueError: If CSV is missing required columns or structure is invalid
    """
    rows: list[tuple] = []

    # Use utf-8-sig to handle UTF-8 BOM if present
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        # Validate CSV structure
        if reader.fieldnames is None:
            raise ValueError(f"Empty or invalid CSV: {path}")

        # Normalize header names to lowercase for case-insensitive matching
        reader.fieldnames = [col.strip().lower() for col in reader.fieldnames]
        
        actual_columns = frozenset(reader.fieldnames)
        missing = _REQUIRED_COLUMNS - actual_columns
        if missing:
            raise ValueError(
                f"IATA CSV missing required columns: {missing}. "
                f"Available: {sorted(actual_columns)}"
            )

        for row in reader:
            iata = row.get("iata", "").strip().lower()
            if not iata or len(iata) != 3 or not iata.isalpha():
                continue

            try:
                lat = float(row.get("latitude", ""))
                lon = float(row.get("longitude", ""))
            except (ValueError, TypeError):
                continue

            # Skip null-island and equator/prime-meridian artifacts
            if lat == 0.0 and lon == 0.0:
                continue

            # Basic coordinate sanity
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                _log.debug(
                    "IATA %s: invalid coordinates (%.4f, %.4f)",
                    iata, lat, lon,
                )
                continue

            city = row.get("city", "").strip()
            if not city:
                continue

            country = row.get("country_code", "").strip().upper()
            # Validate country code: ISO 3166-1 alpha-2 (exactly 2 letters)
            if country and (len(country) != 2 or not country.isalpha()):
                _log.debug("IATA %s: invalid country code %r", iata, country)
                country = ""

            rows.append((iata, city[:64], country, round(lat, 6), round(lon, 6)))

    return rows

    return rows


def seed_iata_codes(
    conn: sqlite3.Connection,
    csv_path: Path | None = None,
) -> int:
    """
    Import IATA codes as low-priority POP candidates.

    Only inserts if (alias_code, canonical_code) pair doesn't exist yet.
    Hand-curated entries (lower priority number) always win in lookups.

    Uses batched inserts with INSERT OR IGNORE for performance.

    Args:
        conn: SQLite connection with pop_locations table
        csv_path: Optional path to pre-downloaded CSV

    Returns:
        Number of records actually inserted
    """
    path = csv_path or fetch_iata_csv()
    rows = _parse_iata_rows(path)

    if not rows:
        _log.warning("No valid IATA entries parsed from %s", path)
        return 0
    
    # Sanity check: warn if suspiciously few codes
    if len(rows) < _MIN_EXPECTED_CODES:
        _log.error(
            "Suspiciously few IATA codes: %d (expected >%d). "
            "Possible corrupt download. Aborting seed.",
            len(rows),
            _MIN_EXPECTED_CODES,
        )
        return 0

    _log.info("Parsed %d valid IATA entries, seeding database...", len(rows))

    now = datetime.now(timezone.utc).isoformat()

    # Batch insert
    sql = """
        INSERT OR IGNORE INTO pop_locations 
            (canonical_code, alias_code, city, country, lat, lon,
             priority, confidence, source, created_at, updated_at, retired)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'iata', ?, ?, 0)
    """

    batch: list[tuple] = []
    for iata, city, country, lat, lon in rows:
        batch.append((
            iata, iata, city, country, lat, lon,
            IATA_PRIORITY, IATA_CONFIDENCE, now, now,
        ))

        if len(batch) >= _BATCH_SIZE:
            conn.executemany(sql, batch)
            batch.clear()

    # Flush remaining
    if batch:
        conn.executemany(sql, batch)

    conn.commit()

    # Count actual inserts by matching created_at timestamp and source
    inserted = conn.execute(
        "SELECT COUNT(*) FROM pop_locations WHERE source = 'iata' AND created_at = ?",
        (now,),
    ).fetchone()[0]

    _log.info(
        "IATA seeding complete: %d new entries (of %d parsed)",
        inserted,
        len(rows),
    )
    return inserted


__all__ = ["seed_iata_codes", "fetch_iata_csv"]
