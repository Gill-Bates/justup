#!/usr/bin/env python3
#
# app/services/pop_refresh.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
POP locations refresh service.

Handles periodic updates from external sources (PeeringDB, UN/LOCODE).
Implements delta-based updates with proper transaction handling.

Features:
- Retry with exponential backoff
- Strict data validation
- Run locking (prevents parallel imports)
- Rate limit handling
- Graceful timeout handling

Lifecycle:
1. Acquire lock (or skip if already running)
2. Start import run
3. Fetch and process external data with retries
4. Validate and upsert locations
5. Retire stale entries (only on success)
6. Finish import run with statistics
7. Release lock
"""

from __future__ import annotations

import httpx
import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

from ..db import pop_locations
from ..db.pop_locations import (
    SOURCE_PEERINGDB,
    SOURCE_UNLOCODE,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    PRIORITY_CANONICAL,
    PRIORITY_ALIAS,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# HTTP settings
HTTP_TIMEOUT_SECONDS = 30
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE = 2.0  # seconds
HTTP_BACKOFF_MAX = 30.0  # seconds
HTTP_JITTER_MAX = 1.0  # seconds
HTTP_MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MB max response size

# Validation limits
MAX_ALIAS_LENGTH = 10
MAX_CITY_LENGTH = 100
MAX_COUNTRY_LENGTH = 5
MAX_RECORDS_PER_RUN = 50000  # Prevent unbounded growth
MAX_NEW_ALIASES_PER_RUN = 5000  # Alias explosion protection
MIN_LAT = -90.0
MAX_LAT = 90.0
MIN_LON = -180.0
MAX_LON = 180.0

# Valid POP code pattern (2-5 lowercase alphanumeric)
POP_CODE_PATTERN = re.compile(r"^[a-z0-9]{2,5}$")

# Control characters (reject these)
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Safe city/country pattern (input sanitization — NOT a substitute for output encoding)
# Allows: letters, digits, spaces, common punctuation for place names
SAFE_PLACE_NAME_PATTERN = re.compile(r"^[\w\s\-\.,'()\u00C0-\u024F]+$", re.UNICODE)


# ---------------------------------------------------------------------------
# Error classification (for structured alerting)
# ---------------------------------------------------------------------------

class ErrorCategory:
    """Error categories for structured logging and alerting."""
    NETWORK = "network"      # Connection, timeout, DNS
    RATELIMIT = "ratelimit"  # HTTP 429
    VALIDATION = "validation"  # Data validation failures
    DATABASE = "database"    # SQLite errors
    PARSE = "parse"          # JSON/data structure errors


# ---------------------------------------------------------------------------
# Run locking (prevents parallel imports for same source)
# ---------------------------------------------------------------------------

_import_locks: dict[str, threading.Lock] = {}
_lock_registry = threading.Lock()


def _get_source_lock(source: str) -> threading.Lock:
    """
    Get or create a lock for a specific import source.
    
    Note: Lock dictionary grows unboundedly with unique source strings.
    Currently only 2 sources (PeeringDB, UN/LOCODE) exist, but consider
    cleanup logic if arbitrary sources are added.
    """
    with _lock_registry:
        if source not in _import_locks:
            _import_locks[source] = threading.Lock()
        return _import_locks[source]


@contextmanager
def _db_connection(get_conn_factory):
    """
    Context manager wrapper for database connections with guaranteed cleanup.
    
    Addresses the connection leak issue: get_conn_factory returns a raw
    sqlite3.Connection, which doesn't close on context exit. This wrapper
    ensures proper cleanup.
    
    Args:
        get_conn_factory: Callable returning sqlite3.Connection
    
    Yields:
        sqlite3.Connection
    """
    conn = get_conn_factory()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Data validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """Result of validating a POP location record."""
    valid: bool
    canonical_code: str = ""
    alias_code: str = ""
    city: str = ""
    country: str = ""
    lat: float = 0.0
    lon: float = 0.0
    error: Optional[str] = None


def validate_pop_record(
    canonical_code: Any,
    alias_code: Any,
    city: Any,
    country: Any,
    lat: Any,
    lon: Any,
) -> ValidationResult:
    """
    Validate and sanitize a POP location record.
    
    Protects against:
    - Alias flooding (length limits)
    - Unicode/control character injection
    - Invalid coordinates
    - Type mismatches
    
    Returns:
        ValidationResult with sanitized values or error.
    """
    # Type checks
    if not isinstance(canonical_code, str) or not isinstance(alias_code, str):
        return ValidationResult(valid=False, error="code must be string")
    if not isinstance(city, str) or not isinstance(country, str):
        return ValidationResult(valid=False, error="city/country must be string")
    
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return ValidationResult(valid=False, error="lat/lon must be numeric")
    
    # Normalize
    canonical_code = canonical_code.lower().strip()
    alias_code = alias_code.lower().strip()
    city = city.strip()
    country = country.upper().strip()
    
    # Empty checks (must come before regex validation)
    if not city:
        return ValidationResult(valid=False, error="city empty")
    if not country:
        return ValidationResult(valid=False, error="country empty")
    
    # Control character check
    for field, name in [(canonical_code, "canonical"), (alias_code, "alias"), 
                        (city, "city"), (country, "country")]:
        if CONTROL_CHAR_PATTERN.search(field):
            return ValidationResult(valid=False, error=f"control chars in {name}")
    
    # Input sanitization: validate city/country against safe pattern
    if not SAFE_PLACE_NAME_PATTERN.match(city):
        return ValidationResult(valid=False, error=f"city contains unsafe chars: {city[:20]}")
    if not SAFE_PLACE_NAME_PATTERN.match(country):
        return ValidationResult(valid=False, error=f"country contains unsafe chars: {country}")
    
    # Length limits
    if len(canonical_code) > MAX_ALIAS_LENGTH:
        return ValidationResult(valid=False, error=f"canonical too long: {len(canonical_code)}")
    if len(alias_code) > MAX_ALIAS_LENGTH:
        return ValidationResult(valid=False, error=f"alias too long: {len(alias_code)}")
    if len(city) > MAX_CITY_LENGTH:
        return ValidationResult(valid=False, error=f"city too long: {len(city)}")
    if len(country) > MAX_COUNTRY_LENGTH:
        return ValidationResult(valid=False, error=f"country too long: {len(country)}")
    
    # POP code format
    if not POP_CODE_PATTERN.match(canonical_code):
        return ValidationResult(valid=False, error=f"invalid canonical format: {canonical_code}")
    if not POP_CODE_PATTERN.match(alias_code):
        return ValidationResult(valid=False, error=f"invalid alias format: {alias_code}")
    
    # Coordinate bounds
    if not (MIN_LAT <= lat <= MAX_LAT):
        return ValidationResult(valid=False, error=f"lat out of bounds: {lat}")
    if not (MIN_LON <= lon <= MAX_LON):
        return ValidationResult(valid=False, error=f"lon out of bounds: {lon}")
    
    return ValidationResult(
        valid=True,
        canonical_code=canonical_code,
        alias_code=alias_code,
        city=city,
        country=country,
        lat=lat,
        lon=lon,
    )


# ---------------------------------------------------------------------------
# HTTP client with retry
# ---------------------------------------------------------------------------

def _fetch_with_retry(
    url: str,
    max_retries: int = HTTP_MAX_RETRIES,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    max_response_bytes: int = HTTP_MAX_RESPONSE_BYTES,
) -> dict:
    """
    Fetch JSON from URL with retry and exponential backoff.
    
    Retries on:
    - Timeout
    - ConnectionError
    - 5xx responses
    - 429 Too Many Requests (with Retry-After)
    
    Does NOT retry on:
    - 4xx responses (except 429)
    - JSON decode error
    - Response too large
    
    Raises:
        RuntimeError: On permanent failure after all retries
    """
    last_error = None
    error_category = ErrorCategory.NETWORK
    
    # Create client once and reuse connection pool across retries
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                response = client.get(url)
                
                # 429 = rate limited, respect Retry-After and retry
                if response.status_code == 429:
                    error_category = ErrorCategory.RATELIMIT
                    retry_after = response.headers.get("retry-after", "60")
                    try:
                        wait_seconds = min(int(retry_after), int(HTTP_BACKOFF_MAX * 2))
                    except ValueError:
                        wait_seconds = int(HTTP_BACKOFF_MAX)
                    _log.warning(
                        "Rate limited (429), waiting %ds (Retry-After: %s)",
                        wait_seconds, retry_after
                    )
                    if attempt < max_retries:
                        time.sleep(wait_seconds)
                        continue
                    raise RuntimeError(f"Rate limited after {max_retries} retries")
                
                # 4xx (except 429) = permanent failure, don't retry
                if 400 <= response.status_code < 500:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                
                # 5xx = transient, retry
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                
                # Response size protection - validate Content-Length header
                content_length = response.headers.get("content-length")
                try:
                    cl = int(content_length) if content_length else None
                except (ValueError, TypeError):
                    cl = None
                
                if cl is not None and cl > max_response_bytes:
                    raise RuntimeError(
                        f"Response too large (Content-Length): {cl} bytes "
                        f"(max {max_response_bytes})"
                    )
                
                # Also check actual body size (Content-Length is optional)
                # Note: response.content has already buffered the full body at this point.
                # For true streaming protection, would need client.stream() with iter_bytes().
                body = response.content
                if len(body) > max_response_bytes:
                    raise RuntimeError(
                        f"Response body too large: {len(body)} bytes "
                        f"(max {max_response_bytes})"
                    )
                
                return response.json()
                    
            except httpx.TimeoutException as e:
                last_error = f"Timeout: {e}"
                error_category = ErrorCategory.NETWORK
            except httpx.ConnectError as e:
                last_error = f"Connection error: {e}"
                error_category = ErrorCategory.NETWORK
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP error: {e}"
                error_category = ErrorCategory.NETWORK
            
            if attempt < max_retries:
                # Exponential backoff with jitter
                backoff = min(HTTP_BACKOFF_BASE ** attempt, HTTP_BACKOFF_MAX)
                jitter = random.uniform(0, HTTP_JITTER_MAX)
                sleep_time = backoff + jitter
                _log.debug("Retry %d/%d in %.1fs: %s", attempt + 1, max_retries, sleep_time, last_error)
                time.sleep(sleep_time)
    
    raise RuntimeError(f"[{error_category}] Failed after {max_retries} retries: {last_error}")


# ---------------------------------------------------------------------------
# Result class
# ---------------------------------------------------------------------------

@dataclass
class PopRefreshResult:
    """Result of a POP refresh operation."""
    run_id: str
    source: str
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    retired: int = 0
    skipped: int = 0
    validation_errors: int = 0
    error: Optional[str] = None
    
    @property
    def success(self) -> bool:
        return self.error is None
    
    def __repr__(self) -> str:
        status = "success" if self.success else f"error: {self.error}"
        return (
            f"PopRefreshResult({self.source}: "
            f"processed={self.processed}, inserted={self.inserted}, "
            f"updated={self.updated}, retired={self.retired}, "
            f"skipped={self.skipped}, errors={self.validation_errors}, {status})"
        )


# ---------------------------------------------------------------------------
# PeeringDB refresh
# ---------------------------------------------------------------------------

PEERINGDB_FAC_URL = "https://api.peeringdb.com/api/fac"
PEERINGDB_IX_URL = "https://api.peeringdb.com/api/ix"

# Batch size for DB operations (performance optimization)
DB_BATCH_SIZE = 100

# ---------------------------------------------------------------------------
# City-to-POP code mapping (authoritative, from seed data)
# This is used to resolve PeeringDB city names to canonical POP codes.
# PeeringDB data is only imported as ALIAS, never creating new canonicals.
# ---------------------------------------------------------------------------

# Major network hubs with standard 3-letter codes
# Source: IATA, major IXP naming conventions
CITY_TO_POP_CODE: dict[str, str] = {
    # Europe
    "frankfurt": "fra",
    "frankfurt am main": "fra",
    "amsterdam": "ams",
    "london": "lon",
    "paris": "par",
    "zurich": "zrh",
    "zürich": "zrh",
    "vienna": "vie",
    "wien": "vie",
    "berlin": "ber",
    "munich": "muc",
    "münchen": "muc",
    "hamburg": "ham",
    "düsseldorf": "dus",
    "dusseldorf": "dus",
    "madrid": "mad",
    "barcelona": "bcn",
    "milan": "mil",
    "milano": "mil",
    "rome": "rom",
    "roma": "rom",
    "brussels": "bru",
    "bruxelles": "bru",
    "stockholm": "sto",
    "copenhagen": "cph",
    "oslo": "osl",
    "helsinki": "hel",
    "warsaw": "waw",
    "warszawa": "waw",
    "prague": "prg",
    "praha": "prg",
    "budapest": "bud",
    "dublin": "dub",
    "lisbon": "lis",
    "lisboa": "lis",
    "marseille": "mrs",
    "lyon": "lyo",
    
    # North America
    "new york": "nyc",
    "new york city": "nyc",
    "los angeles": "lax",
    "chicago": "chi",
    "dallas": "dfw",
    "miami": "mia",
    "atlanta": "atl",
    "seattle": "sea",
    "san francisco": "sfo",
    "san jose": "sjc",
    "denver": "den",
    "phoenix": "phx",
    "boston": "bos",
    "washington": "iad",
    "ashburn": "iad",
    "toronto": "yyz",
    "montreal": "yul",
    "vancouver": "yvr",
    
    # Asia Pacific
    "tokyo": "tyo",
    "osaka": "osa",
    "singapore": "sin",
    "hong kong": "hkg",
    "seoul": "sel",
    "sydney": "syd",
    "melbourne": "mel",
    "mumbai": "bom",
    "delhi": "del",
    "new delhi": "del",
    "bangalore": "blr",
    "chennai": "maa",
    "shanghai": "sha",
    "beijing": "pek",
    "taipei": "tpe",
    "jakarta": "jkt",
    "kuala lumpur": "kul",
    "bangkok": "bkk",
    
    # Middle East / Africa
    "dubai": "dxb",
    "tel aviv": "tlv",
    "johannesburg": "jnb",
    "cape town": "cpt",
    "cairo": "cai",
    
    # South America
    "sao paulo": "gru",
    "são paulo": "gru",
    "rio de janeiro": "gig",
    "buenos aires": "eze",
    "santiago": "scl",
    "bogota": "bog",
    "bogotá": "bog",
    "lima": "lim",
    "mexico city": "mex",
    "ciudad de mexico": "mex",
}


def _lookup_pop_code_by_city(city: str) -> Optional[str]:
    """
    Lookup canonical POP code by city name.
    
    Uses authoritative mapping from seed data.
    Only returns codes for known major network hubs.
    
    Args:
        city: City name from external source
    
    Returns:
        Canonical 3-letter POP code or None
    """
    if not city:
        return None
    
    normalized = city.lower().strip()
    return CITY_TO_POP_CODE.get(normalized)


def refresh_from_peeringdb(get_conn) -> PopRefreshResult:
    """
    Refresh POP locations from PeeringDB API.
    
    PeeringDB provides:
    - Facility locations (data centers, POPs)
    - IX locations (Internet Exchange points)
    
    Uses locking to prevent parallel runs.
    
    Args:
        get_conn: Connection factory callable
    
    Returns:
        PopRefreshResult with statistics
    """
    lock = _get_source_lock(SOURCE_PEERINGDB)
    
    if not lock.acquire(blocking=False):
        _log.warning("PeeringDB refresh already running, skipping")
        return PopRefreshResult(
            run_id="",
            source=SOURCE_PEERINGDB,
            error="already_running",
        )
    
    try:
        return _do_peeringdb_refresh(get_conn)
    finally:
        lock.release()


def _do_peeringdb_refresh(get_conn) -> PopRefreshResult:
    """
    Actual PeeringDB refresh logic (called with lock held).
    
    IMPORTANT: PeeringDB is only used as an ALIAS source, not for creating
    new canonical codes. Only facilities in cities with known POP codes
    (from CITY_TO_POP_CODE) are imported.
    
    Uses single connection for the entire operation to avoid overhead
    and ensure transaction integrity.
    """
    processed = 0
    inserted = 0
    updated = 0
    skipped = 0
    validation_errors = 0
    error_msg = None
    run_id = ""
    
    # Phase tracking for structured logging
    phase = "init"
    
    try:
        # Start import run
        with _db_connection(get_conn) as conn:
            run_id = pop_locations.start_import_run(conn, SOURCE_PEERINGDB)
        
        # ─── PHASE: FETCH ────────────────────────────────────────
        phase = "fetch_facilities"
        _log.info("[%s] Fetching PeeringDB facilities...", run_id)
        fac_data = _fetch_with_retry(PEERINGDB_FAC_URL)
        facilities = fac_data.get("data", [])
        
        _log.info("[%s] Received %d facilities from PeeringDB", run_id, len(facilities))
        
        # ─── PHASE: PROCESS (single connection, batch commits) ───
        phase = "process_facilities"
        batch_count = 0
        
        with _db_connection(get_conn) as conn:
            for fac in facilities:
                # Max records limit
                if processed >= MAX_RECORDS_PER_RUN:
                    _log.warning("[%s] Hit max records limit (%d), stopping", run_id, MAX_RECORDS_PER_RUN)
                    break
                
                # Alias explosion protection
                if inserted >= MAX_NEW_ALIASES_PER_RUN:
                    _log.warning("[%s] Hit max new aliases limit (%d), stopping inserts", run_id, MAX_NEW_ALIASES_PER_RUN)
                    break
                
                # Extract fields
                city = fac.get("city", "")
                country = fac.get("country", "")
                lat = fac.get("latitude")
                lon = fac.get("longitude")
                
                # Skip if no coordinates
                if lat is None or lon is None:
                    skipped += 1
                    continue
                
                # Resolve POP code from city (authoritative mapping only)
                # PeeringDB is ALIAS source - we only import if we have a canonical
                pop_code = _lookup_pop_code_by_city(city)
                if not pop_code:
                    skipped += 1
                    continue
                
                # Validate
                result = validate_pop_record(
                    canonical_code=pop_code,
                    alias_code=pop_code,
                    city=city,
                    country=country,
                    lat=lat,
                    lon=lon,
                )
                
                if not result.valid:
                    validation_errors += 1
                    _log.debug("[%s] Validation failed for city=%s: %s", run_id, city, result.error)
                    continue
                
                # Check if exists and upsert
                existing = pop_locations.lookup_pop_by_tokens(conn, [result.alias_code])
                was_update = existing is not None
                
                try:
                    success = pop_locations.upsert_pop_location(
                        conn,
                        canonical_code=result.canonical_code,
                        alias_code=result.alias_code,
                        city=result.city,
                        country=result.country,
                        lat=result.lat,
                        lon=result.lon,
                        source=SOURCE_PEERINGDB,
                        confidence=CONFIDENCE_HIGH,
                        # ALIAS priority - PeeringDB never creates canonicals
                        priority=PRIORITY_ALIAS,
                        import_run_id=run_id,
                    )
                    
                    if success:
                        if was_update:
                            updated += 1
                        else:
                            inserted += 1
                    
                    processed += 1
                    batch_count += 1
                    
                    # Batch commit for performance
                    if batch_count >= DB_BATCH_SIZE:
                        conn.commit()
                        batch_count = 0
                        
                except Exception as db_err:
                    _log.warning("[%s] DB error for city=%s: %s", run_id, city, db_err)
                    continue
            
            # Final commit for remaining batch
            if batch_count > 0:
                conn.commit()
        
        # ─── PHASE: COMPLETE ─────────────────────────────────────
        phase = "complete"
        _log.info(
            "[%s] Processing complete: processed=%d, inserted=%d, updated=%d, skipped=%d, validation_errors=%d",
            run_id, processed, inserted, updated, skipped, validation_errors
        )
        
    except Exception as e:
        # Determine error category based on exception type and phase
        error_category = ErrorCategory.NETWORK  # default for fetch/network errors
        if "validation" in phase.lower():
            error_category = ErrorCategory.VALIDATION
        elif "database" in str(e).lower() or "sqlite" in str(e).lower():
            error_category = ErrorCategory.DATABASE
        
        error_msg = f"[{error_category}][phase={phase}] {e}"
        _log.error("[%s] PeeringDB refresh failed in phase '%s': %s", run_id, phase, e)
    
    # ─── PHASE: RETIRE ───────────────────────────────────────────
    retired = 0
    if not error_msg and processed > 0:
        phase = "retire"
        with _db_connection(get_conn) as conn:
            # Pass processed count for safety threshold check
            retired = pop_locations.retire_stale_entries(
                conn, SOURCE_PEERINGDB, run_id, records_processed=processed
            )
        _log.info("[%s] Retired %d stale entries", run_id, retired)
    
    # ─── PHASE: FINALIZE ─────────────────────────────────────────
    phase = "finalize"
    with _db_connection(get_conn) as conn:
        pop_locations.finish_import_run(
            conn,
            run_id=run_id,
            records_processed=processed,
            records_inserted=inserted,
            records_updated=updated,
            records_retired=retired,
            error_message=error_msg,
        )
    
    return PopRefreshResult(
        run_id=run_id,
        source=SOURCE_PEERINGDB,
        processed=processed,
        inserted=inserted,
        updated=updated,
        retired=retired,
        skipped=skipped,
        validation_errors=validation_errors,
        error=error_msg,
    )


# ---------------------------------------------------------------------------
# UN/LOCODE refresh (placeholder)
# ---------------------------------------------------------------------------

def refresh_from_unlocode(get_conn) -> PopRefreshResult:
    """
    Refresh POP locations from UN/LOCODE data (placeholder).
    
    UN/LOCODE provides:
    - Standard location codes (5 chars: 2 country + 3 location)
    - City names and coordinates
    
    Currently not implemented. Returns early without creating database records.
    
    Args:
        get_conn: Connection factory callable
    
    Returns:
        PopRefreshResult with no-op status
    """
    _log.debug("UN/LOCODE refresh: not yet implemented")
    return PopRefreshResult(run_id="", source=SOURCE_UNLOCODE)


# ---------------------------------------------------------------------------
# Full refresh
# ---------------------------------------------------------------------------

def run_full_refresh(get_conn) -> list[PopRefreshResult]:
    """
    Run a full refresh from all external sources.
    
    Args:
        get_conn: Connection factory callable
    
    Returns:
        List of PopRefreshResult for each source
    """
    _log.info("Starting full POP locations refresh")
    
    results = []
    
    # PeeringDB (primary source for network POPs)
    results.append(refresh_from_peeringdb(get_conn))
    
    # UN/LOCODE (supplementary location data)
    results.append(refresh_from_unlocode(get_conn))
    
    # Log summary
    total_processed = sum(r.processed for r in results)
    total_inserted = sum(r.inserted for r in results)
    total_updated = sum(r.updated for r in results)
    total_retired = sum(r.retired for r in results)
    total_errors = sum(r.validation_errors for r in results)
    failures = [r for r in results if not r.success]
    
    _log.info(
        "Full refresh complete: processed=%d, inserted=%d, updated=%d, "
        "retired=%d, validation_errors=%d, source_failures=%d",
        total_processed, total_inserted, total_updated, 
        total_retired, total_errors, len(failures)
    )
    
    return results


def get_refresh_status(get_conn) -> dict:
    """
    Get current POP refresh status and statistics.
    
    Note: The 'locks' field uses threading.Lock.locked() which is inherently
    racy. It reports the lock state at query time, but the state may change
    immediately after. Use for monitoring/debugging only, not for synchronization.
    
    Returns:
        Dict with counts and last run info
    """
    with _db_connection(get_conn) as conn:
        return {
            "active_locations": pop_locations.count_active_pop_locations(conn),
            "retired_locations": pop_locations.count_retired_pop_locations(conn),
            "total_locations": pop_locations.get_pop_location_count(conn),
            "last_peeringdb_run": pop_locations.get_last_import_run(conn, SOURCE_PEERINGDB),
            "last_unlocode_run": pop_locations.get_last_import_run(conn, SOURCE_UNLOCODE),
            "recent_runs": pop_locations.get_import_run_stats(conn, limit=5),
            "locks": {
                SOURCE_PEERINGDB: _get_source_lock(SOURCE_PEERINGDB).locked(),
                SOURCE_UNLOCODE: _get_source_lock(SOURCE_UNLOCODE).locked(),
            },
        }
