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
5. Finish import run with statistics
6. Release lock

Retirement of stale entries does not happen in any path yet: the PeeringDB
path only maintains city evidence (retired stays 0) and the UN/LOCODE import
is a placeholder.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from ..db import ixp_prefixes, pop_locations
from ..db import sqlite as sqlite_db
from ..db.pop_locations import SOURCE_PEERINGDB, SOURCE_UNLOCODE
from ..utils import maintenance
from ..utils.time import parse_utc, utcnow
from . import ixp_import, ixp_index

_log = logging.getLogger(__name__)


@contextmanager
def _db_connection(get_conn):
    """Close and unregister a connection supplied by the app factory."""
    conn = get_conn()
    try:
        with conn:
            yield conn
    finally:
        sqlite_db.close_connection(conn)


# Configuration

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
MAX_CITY_GROUPS_PER_RUN = 5000  # Bound city-level evidence growth
MIN_LAT = -90.0
MAX_LAT = 90.0
MIN_LON = -180.0
MAX_LON = 180.0

# Valid POP code pattern (2-5 lowercase alphanumeric)
POP_CODE_PATTERN = re.compile(r"^[a-z0-9]{2,5}$")

# Control characters (reject these)
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Safe city/country pattern (XSS protection)
# Allows: letters, digits, spaces, common punctuation for place names
SAFE_PLACE_NAME_PATTERN = re.compile(r"^[\w\s\-\.,'()\u00C0-\u024F]+$", re.UNICODE)


# Error classification (for structured alerting)


class ErrorCategory:
    """Error categories for structured logging and alerting."""

    NETWORK = "network"  # Connection, timeout, DNS
    RATELIMIT = "ratelimit"  # HTTP 429
    VALIDATION = "validation"  # Data validation failures
    DATABASE = "database"  # SQLite errors
    PARSE = "parse"  # JSON/data structure errors


# Run locking (prevents parallel imports for same source)

_import_locks: dict[str, threading.Lock] = {}
_lock_registry = threading.Lock()


def _get_source_lock(source: str) -> threading.Lock:
    """Get or create a lock for a specific import source."""
    with _lock_registry:
        if source not in _import_locks:
            _import_locks[source] = threading.Lock()
        return _import_locks[source]


# Data validation


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
    error: str | None = None


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
    if not isinstance(canonical_code, str) or not isinstance(alias_code, str):
        return ValidationResult(valid=False, error="code must be string")
    if not isinstance(city, str) or not isinstance(country, str):
        return ValidationResult(valid=False, error="city/country must be string")

    # bool is an int subclass, so float(True) would pass as a coordinate
    if isinstance(lat, bool) or isinstance(lon, bool):
        return ValidationResult(valid=False, error="lat/lon must be numeric")
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return ValidationResult(valid=False, error="lat/lon must be numeric")

    canonical_code = canonical_code.lower().strip()
    alias_code = alias_code.lower().strip()
    city = city.strip()
    country = country.upper().strip()

    for field, name in [(canonical_code, "canonical"), (alias_code, "alias"), (city, "city"), (country, "country")]:
        if CONTROL_CHAR_PATTERN.search(field):
            return ValidationResult(valid=False, error=f"control chars in {name}")

    # XSS protection: validate city/country against safe pattern
    if not SAFE_PLACE_NAME_PATTERN.match(city):
        return ValidationResult(valid=False, error=f"city contains unsafe chars: {city[:20]}")
    if not SAFE_PLACE_NAME_PATTERN.match(country):
        return ValidationResult(valid=False, error=f"country contains unsafe chars: {country}")

    if len(canonical_code) > MAX_ALIAS_LENGTH:
        return ValidationResult(valid=False, error=f"canonical too long: {len(canonical_code)}")
    if len(alias_code) > MAX_ALIAS_LENGTH:
        return ValidationResult(valid=False, error=f"alias too long: {len(alias_code)}")
    if len(city) > MAX_CITY_LENGTH:
        return ValidationResult(valid=False, error=f"city too long: {len(city)}")
    if len(country) > MAX_COUNTRY_LENGTH:
        return ValidationResult(valid=False, error=f"country too long: {len(country)}")

    if not POP_CODE_PATTERN.match(canonical_code):
        return ValidationResult(valid=False, error=f"invalid canonical format: {canonical_code}")
    if not POP_CODE_PATTERN.match(alias_code):
        return ValidationResult(valid=False, error=f"invalid alias format: {alias_code}")

    if not (MIN_LAT <= lat <= MAX_LAT):
        return ValidationResult(valid=False, error=f"lat out of bounds: {lat}")
    if not (MIN_LON <= lon <= MAX_LON):
        return ValidationResult(valid=False, error=f"lon out of bounds: {lon}")

    if not city:
        return ValidationResult(valid=False, error="city empty")
    if not country:
        return ValidationResult(valid=False, error="country empty")

    return ValidationResult(
        valid=True,
        canonical_code=canonical_code,
        alias_code=alias_code,
        city=city,
        country=country,
        lat=lat,
        lon=lon,
    )


# HTTP client with retry


def _read_bounded(response: Any, limit: int) -> bytes:
    """Read at most ``limit`` bytes of a streaming response body."""
    data = bytearray()
    for chunk in response.iter_bytes():
        data.extend(chunk)
        if len(data) >= limit:
            break
    return bytes(data[:limit])


def _parse_retry_after(value: str) -> int | None:
    """Seconds from an untrusted Retry-After header (delta-seconds or HTTP date), capped; None if invalid."""
    cap = int(HTTP_BACKOFF_MAX * 2)
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(int(value), cap) if len(value) <= 9 else cap
    try:
        delta = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return min(math.ceil(delta), cap) if delta >= 0 else None


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter for the given zero-based attempt."""
    jitter = random.uniform(0, HTTP_JITTER_MAX)  # noqa: S311 - backoff jitter, not security-sensitive
    return min(HTTP_BACKOFF_BASE**attempt, HTTP_BACKOFF_MAX) + jitter


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
    import httpx

    last_error = None
    error_category = ErrorCategory.NETWORK

    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client, client.stream("GET", url) as response:
                # 429 = rate limited, respect Retry-After and retry
                if response.status_code == 429:
                    error_category = ErrorCategory.RATELIMIT
                    retry_after = response.headers.get("retry-after", "60")
                    wait_seconds = _parse_retry_after(retry_after)
                    if wait_seconds is None:
                        # Invalid (negative, malformed, past date): use the normal backoff instead.
                        _log.warning("Rate limited (429), ignoring invalid Retry-After %.40r", retry_after)
                        wait_seconds = _backoff_delay(attempt)
                    else:
                        _log.warning("Rate limited (429), waiting %ds (Retry-After: %s)", wait_seconds, retry_after)
                    if attempt < max_retries:
                        time.sleep(wait_seconds)
                        continue
                    raise RuntimeError(f"Rate limited after {max_retries} retries")

                # 4xx (except 429) = permanent failure, don't retry
                if 400 <= response.status_code < 500:
                    excerpt = _read_bounded(response, 200)
                    raise RuntimeError(f"HTTP {response.status_code}: {excerpt.decode('utf-8', 'replace')}")

                # 5xx = transient, retry
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )

                # Response size protection: check the header first, then the running total while streaming
                content_length = response.headers.get("content-length")
                if (
                    content_length
                    and content_length.isascii()
                    and content_length.isdigit()
                    and int(content_length) > max_response_bytes
                ):
                    raise RuntimeError(f"Response too large: {content_length} bytes (max {max_response_bytes})")

                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise RuntimeError(f"Response body too large: over {max_response_bytes} bytes")

                return json.loads(bytes(body))

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
            sleep_time = _backoff_delay(attempt)
            _log.debug("Retry %d/%d in %.1fs: %s", attempt + 1, max_retries, sleep_time, last_error)
            time.sleep(sleep_time)

    raise RuntimeError(f"[{error_category}] Failed after {max_retries} retries: {last_error}")


# Result class


class PopRefreshResult:
    """Result of a POP refresh operation."""

    def __init__(
        self,
        run_id: str,
        source: str,
        processed: int = 0,
        inserted: int = 0,
        updated: int = 0,
        retired: int = 0,
        skipped: int = 0,
        validation_errors: int = 0,
        error: str | None = None,
    ):
        self.run_id = run_id
        self.source = source
        self.processed = processed
        self.inserted = inserted
        self.updated = updated
        self.retired = retired
        self.skipped = skipped
        self.validation_errors = validation_errors
        self.error = error
        self.success = error is None

    def __repr__(self) -> str:
        status = "success" if self.success else f"error: {self.error}"
        return (
            f"PopRefreshResult({self.source}: "
            f"processed={self.processed}, inserted={self.inserted}, "
            f"updated={self.updated}, retired={self.retired}, "
            f"skipped={self.skipped}, errors={self.validation_errors}, {status})"
        )


# PeeringDB refresh

PEERINGDB_API = "https://api.peeringdb.com/api"
IXP_TABLES = ("ix", "ixlan", "ixpfx", "ixfac")
PEERINGDB_PAGE_SIZE = 2000
PEERINGDB_MAX_PAGES = 50  # per table; a table that never ends is a broken or hostile response
PEERINGDB_MAX_ROWS = 100_000
# Curated city-to-code hints for grouping PeeringDB city evidence; they do not prove a router hostname alias.

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


def _lookup_pop_code_by_city(city: str) -> str | None:
    """
    Lookup a curated city code hint by city name.

    Only returns codes for known major network hubs; callers must also check country.

    Args:
        city: City name from external source

    Returns:
        City code hint or None
    """
    if not city:
        return None

    normalized = pop_locations.normalize_city(city).casefold()
    return CITY_TO_POP_CODE.get(normalized)


_SEED_COUNTRIES = pop_locations.seed_country_map()
_SEED_COUNTRIES.update(
    {
        "bkk": "TH",
        "blr": "IN",
        "bog": "CO",
        "cai": "EG",
        "cpt": "ZA",
        "dxb": "AE",
        "eze": "AR",
        "gig": "BR",
        "gru": "BR",
        "jkt": "ID",
        "jnb": "ZA",
        "kul": "MY",
        "lim": "PE",
        "lyo": "FR",
        "maa": "IN",
        "mex": "MX",
        "pek": "CN",
        "scl": "CL",
        "sel": "KR",
        "sha": "CN",
        "tlv": "IL",
        "tpe": "TW",
        "yul": "CA",
        "yvr": "CA",
        "yyz": "CA",
    }
)
MAX_FACILITY_SPREAD_KM = 50.0


def city_code_for_country(city: object, country: object) -> str | None:
    """Return a curated city code only when the supplied country matches it."""
    if not isinstance(city, str) or not isinstance(country, str):
        return None
    code = _lookup_pop_code_by_city(city)
    return code if code and _SEED_COUNTRIES.get(code) == pop_locations.normalize_country(country) else None


# Contradictory facility coordinates are detected with the same great-circle distance as the IXP import.
_facility_distance_km = ixp_import.great_circle_km


def _group_facilities(
    facilities: list[dict], run_id: str
) -> tuple[dict[tuple[str, str], list[ValidationResult]], int, int]:
    groups: dict[tuple[str, str], list[ValidationResult]] = {}
    skipped = validation_errors = 0
    for fac in facilities:
        if not isinstance(fac, dict):
            validation_errors += 1
            continue
        city, country = fac.get("city"), fac.get("country")
        code = city_code_for_country(city, country)
        normalized_country = pop_locations.normalize_country(country) if isinstance(country, str) else ""
        if not code:
            skipped += 1
            _log.debug(
                "[%s] Skipping unverified PeeringDB city/country %r/%r", run_id, str(city)[:80], str(country)[:10]
            )
            continue
        result = validate_pop_record(code, code, city, country, fac.get("latitude"), fac.get("longitude"))
        if not result.valid:
            validation_errors += 1
            _log.debug("[%s] Invalid PeeringDB facility for %r/%r: %s", run_id, city[:80], country[:10], result.error)
            continue
        groups.setdefault((code, normalized_country), []).append(result)
    return groups, skipped, validation_errors


def _fetch_peeringdb_table(table: str) -> list[dict]:
    """Fetch every page of one PeeringDB table (``fac`` included); a malformed, repeated or endless page raises.

    A response without a ``data`` list is never read as an empty table: a partial table would make the
    caller trust incomplete evidence.
    """
    rows: list[dict] = []
    first_ids: set[object] = set()
    for page in range(PEERINGDB_MAX_PAGES):
        skip = page * PEERINGDB_PAGE_SIZE
        payload = _fetch_with_retry(f"{PEERINGDB_API}/{table}?limit={PEERINGDB_PAGE_SIZE}&skip={skip}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RuntimeError(f"PeeringDB {table} response is malformed (no data list)")
        marker = data[0].get("id") if data and isinstance(data[0], dict) else None
        if marker is not None and marker in first_ids:
            raise RuntimeError(f"PeeringDB {table} pagination repeats rows (limit/skip ignored)")
        first_ids.add(marker)
        rows.extend(data)
        if len(rows) > PEERINGDB_MAX_ROWS:
            raise RuntimeError(f"PeeringDB {table} exceeds {PEERINGDB_MAX_ROWS} rows")
        if len(data) < PEERINGDB_PAGE_SIZE:
            return rows
    raise RuntimeError(f"PeeringDB {table} exceeds {PEERINGDB_MAX_PAGES} pages")


def _fetch_ixp_snapshot(facilities: list[dict]) -> ixp_import.IxpBuildResult:
    """Download and validate everything first; the database is only touched with a complete result."""
    if not facilities:
        raise RuntimeError("PeeringDB fac table is empty; exchange regions cannot be evidenced")
    tables = {name: _fetch_peeringdb_table(name) for name in IXP_TABLES}
    result = ixp_import.build_snapshot(tables["ix"], tables["ixlan"], tables["ixpfx"], tables["ixfac"], facilities)
    ixp_prefixes.check_plausible(result.snapshot, 0)
    return result


def refresh_from_peeringdb(get_conn) -> PopRefreshResult:
    """
    Refresh POP locations from PeeringDB API.

    Fetches PeeringDB facility locations (data centers, POPs) as city evidence, plus the ix, ixlan,
    ixpfx and ixfac tables for the IXP prefix snapshot. Uses locking to prevent parallel runs.

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
    """Fetch external data without the maintenance lock, then import only if still current."""
    generation = maintenance.current_generation()
    try:
        _log.info("Fetching PeeringDB facilities...")
        facilities = _fetch_peeringdb_table("fac")
    except Exception as e:  # noqa: BLE001 - source-fetch boundary; never write POP data after a failed fetch
        error_msg = f"[network][phase=fetch_facilities] {e}"
        _log.error("PeeringDB refresh failed during fetch: %s", e)
        # Persist the failed attempt (a status row only, never POP location data) so the scheduler's
        # backoff survives a restart and repeated restarts during an outage cannot download-loop. A
        # restore that began during the fetch advances the generation, so the failure record is
        # skipped as stale rather than written over freshly restored data.
        return _record_fetch_failure(get_conn, generation, error_msg)

    # IXP prefixes are fetched and validated completely before any write. A failure keeps the active
    # IXP snapshot and fails the run (so the normal backoff applies); the facility import still happens.
    ixp_result: ixp_import.IxpBuildResult | None = None
    ixp_error: str | None = None
    try:
        _log.info("Fetching PeeringDB IXP prefixes...")
        ixp_result = _fetch_ixp_snapshot(facilities)
    except Exception as e:  # noqa: BLE001 - source-fetch boundary; the active IXP snapshot must survive
        rejected = isinstance(e, ixp_prefixes.IxpSnapshotRejectedError)
        category = ErrorCategory.VALIDATION if rejected else ErrorCategory.NETWORK
        ixp_error = f"[{category}][phase=fetch_ixp] {e}"
        _log.error("PeeringDB IXP fetch failed; keeping the active IXP snapshot: %s", e)

    try:
        with maintenance.work_section(generation):
            return _apply_peeringdb_snapshot(get_conn, facilities, ixp_result=ixp_result, ixp_error=ixp_error)
    except maintenance.MaintenanceActiveError:
        _log.info("PeeringDB import deferred because its data snapshot is stale or restore is active")
        return PopRefreshResult(
            run_id="",
            source=SOURCE_PEERINGDB,
            error="maintenance_in_progress",
        )


def _record_fetch_failure(get_conn, generation: int, error_msg: str) -> PopRefreshResult:
    """Persist a failed PeeringDB run after a fetch error so backoff state survives a restart.

    The run is recorded inside a work section tied to ``generation``: if a restore replaced the
    data during the (lock-free) fetch, the record is deferred instead of written to restored data.
    """
    try:
        with maintenance.work_section(generation):
            with _db_connection(get_conn) as conn:
                run_id = pop_locations.start_import_run(conn, SOURCE_PEERINGDB)
            with _db_connection(get_conn) as conn:
                pop_locations.finish_import_run(
                    conn,
                    run_id=run_id,
                    records_processed=0,
                    records_inserted=0,
                    records_updated=0,
                    records_retired=0,
                    error_message=error_msg,
                )
            return PopRefreshResult(run_id=run_id, source=SOURCE_PEERINGDB, error=error_msg)
    except maintenance.MaintenanceActiveError:
        _log.info("PeeringDB fetch-failure record deferred: data snapshot is stale or restore is active")
        return PopRefreshResult(run_id="", source=SOURCE_PEERINGDB, error="maintenance_in_progress")


def _apply_peeringdb_snapshot(
    get_conn,
    facilities: list[dict],
    ixp_result: ixp_import.IxpBuildResult | None = None,
    ixp_error: str | None = None,
) -> PopRefreshResult:
    """
    Import a fetched PeeringDB snapshot while a maintenance work section is held.

    PeeringDB facilities supply city-level evidence only. They never create,
    refresh or retire productive router aliases.

    Writes each validated city group through one database connection. ``processed``
    counts accepted facilities; ``inserted`` and ``updated`` count city evidence rows.
    """
    with _db_connection(get_conn) as conn:
        run_id = pop_locations.start_import_run(conn, SOURCE_PEERINGDB)

    processed = 0
    inserted = 0
    updated = 0
    skipped = 0
    validation_errors = 0
    error_msg = None
    error_category = None
    # Retire only after a full pass: a limit or DB error leaves valid rows untouched
    partial_reason: str | None = None

    phase = "process_facilities"

    try:
        _log.info("[%s] Received %d facilities from PeeringDB", run_id, len(facilities))

        # Reject an oversized snapshot before selecting any subset: input order must not pick winners.
        if len(facilities) > MAX_RECORDS_PER_RUN:
            partial_reason = f"max records limit ({MAX_RECORDS_PER_RUN}) reached"
        else:
            groups, skipped, validation_errors = _group_facilities(facilities, run_id)
            if len(groups) > MAX_CITY_GROUPS_PER_RUN:
                partial_reason = f"max city groups limit ({MAX_CITY_GROUPS_PER_RUN}) reached"
            else:
                with _db_connection(get_conn) as conn:
                    for (code, country), records in sorted(groups.items()):
                        coords = sorted((record.lat, record.lon) for record in records)
                        centroid = (
                            sum(point[0] for point in coords) / len(coords),
                            sum(point[1] for point in coords) / len(coords),
                        )
                        if any(_facility_distance_km(point, centroid) > MAX_FACILITY_SPREAD_KM / 2 for point in coords):
                            skipped += len(records)
                            _log.warning(
                                "[%s] Skipping spatially conflicting PeeringDB facilities for %s/%s",
                                run_id,
                                code,
                                country,
                            )
                            continue
                        anchors = conn.execute(
                            "SELECT city, lat, lon, source FROM pop_locations WHERE country = ? "
                            "AND source IN ('manual', 'seed') AND retired = 0 "
                            "AND (canonical_code = ? OR alias_code = ? OR city = ? COLLATE NOCASE) "
                            "ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END, priority, alias_code",
                            (country, code, code, min(record.city for record in records)),
                        ).fetchall()
                        if anchors:
                            city, lat, lon, position_source = anchors[0]
                            if any(
                                _facility_distance_km((item[1], item[2]), centroid) > MAX_FACILITY_SPREAD_KM
                                or _facility_distance_km((item[1], item[2]), (lat, lon)) > MAX_FACILITY_SPREAD_KM
                                for item in anchors
                            ):
                                skipped += len(records)
                                _log.warning(
                                    "[%s] Curated city position conflicts with facilities for %s/%s",
                                    run_id,
                                    code,
                                    country,
                                )
                                continue
                        else:
                            city = min(record.city for record in records)
                            lat, lon = (round(value, 5) for value in centroid)
                            position_source = "facility-centroid"
                        existed = conn.execute(
                            "SELECT 1 FROM pop_city_evidence WHERE city_code = ? AND country = ?",
                            (code, country),
                        ).fetchone()
                        cursor = conn.execute(
                            """INSERT INTO pop_city_evidence
                            (city_code, city, country, lat, lon, facility_count, position_source, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(city_code, country) DO UPDATE SET
                                city = excluded.city, lat = excluded.lat, lon = excluded.lon,
                                facility_count = excluded.facility_count,
                                position_source = excluded.position_source, updated_at = excluded.updated_at
                            WHERE city != excluded.city OR lat != excluded.lat OR lon != excluded.lon
                               OR facility_count != excluded.facility_count
                               OR position_source != excluded.position_source""",
                            (code, city, country, lat, lon, len(records), position_source, utcnow().isoformat()),
                        )
                        if cursor.rowcount:
                            if existed:
                                updated += 1
                            else:
                                inserted += 1
                        processed += len(records)
                    conn.commit()

        phase = "complete"
        _log.info(
            "[%s] Processing complete: processed=%d, inserted=%d, updated=%d, skipped=%d, validation_errors=%d",
            run_id,
            processed,
            inserted,
            updated,
            skipped,
            validation_errors,
        )

    except Exception as e:  # noqa: BLE001 - refresh boundary: categorise and continue so one row/phase error never aborts the run
        error_category = error_category or (
            ErrorCategory.DATABASE if isinstance(e, sqlite3.Error) else ErrorCategory.PARSE
        )
        error_msg = f"[{error_category}][phase={phase}] {e}"
        _log.error("[%s] PeeringDB refresh failed in phase '%s': %s", run_id, phase, e)

    if partial_reason and not error_msg:
        # An incomplete snapshot is recorded but never changes productive aliases.
        error_msg = f"[partial][phase=process_facilities] incomplete snapshot: {partial_reason}"
        _log.warning("[%s] %s", run_id, error_msg)

    # A facility snapshot does not prove that a router alias has disappeared.
    retired = 0

    # IXP prefix snapshot
    if ixp_error:
        error_msg = f"{error_msg}; {ixp_error}" if error_msg else ixp_error
    elif ixp_result is not None:
        phase = "apply_ixp"
        try:
            with _db_connection(get_conn) as conn:
                stats = ixp_prefixes.replace_snapshot(conn, ixp_result.snapshot, run_id)
                # replace_snapshot() has COMMITted by now; a failed or rolled-back import raises before this.
                ixp_index.reload_from_connection(conn)
            processed += stats.prefixes
            inserted += stats.inserted
            updated += stats.updated
            retired += stats.removed
            if ixp_result.excluded:
                _log.info("[%s] IXP records excluded during validation: %s", run_id, ixp_result.excluded)
        except Exception as e:  # noqa: BLE001 - the transaction rolled back, so the previous snapshot is still active
            category = (
                ErrorCategory.VALIDATION
                if isinstance(e, ixp_prefixes.IxpSnapshotRejectedError)
                else ErrorCategory.DATABASE
            )
            ixp_msg = f"[{category}][phase=apply_ixp] {e}"
            _log.error("[%s] IXP snapshot not applied: %s", run_id, e)
            error_msg = f"{error_msg}; {ixp_msg}" if error_msg else ixp_msg

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


# UN/LOCODE refresh (placeholder)


ERROR_NOT_IMPLEMENTED = "not_implemented"


def refresh_from_unlocode(get_conn) -> PopRefreshResult:
    """
    UN/LOCODE refresh: not implemented, always returns an ``ERROR_NOT_IMPLEMENTED`` result.

    No import run is recorded, so the status endpoint never shows a successful UN/LOCODE import.

    Args:
        get_conn: Connection factory callable

    Returns:
        PopRefreshResult with statistics
    """
    lock = _get_source_lock(SOURCE_UNLOCODE)

    if not lock.acquire(blocking=False):
        _log.warning("UN/LOCODE refresh already running, skipping")
        return PopRefreshResult(
            run_id="",
            source=SOURCE_UNLOCODE,
            error="already_running",
        )

    try:
        # No data fetch exists yet: report that instead of recording a successful empty import run.
        _log.info("UN/LOCODE refresh requested but data integration is not implemented")
        return PopRefreshResult(run_id="", source=SOURCE_UNLOCODE, error=ERROR_NOT_IMPLEMENTED)
    finally:
        lock.release()


# Self-managing refresh scheduling (state-based, driven by pop_import_runs)
#
# justUp decides itself when the external POP data (PeeringDB) needs refreshing. The decision is
# computed from the persisted import history, not from any user setting and not from the container
# start time, so a restart never re-downloads data that was refreshed recently and overdue
# refreshes are caught up promptly. These are internal scheduler constants, not user settings.

# How long an existing successful PeeringDB import stays fresh before a refresh is due.
POP_FRESHNESS_SECONDS = 30 * 24 * 3600  # 30 days

# Startup offset applied before the very first refresh attempt on a fresh install (no successful
# external import yet). Short, randomised to spread many installations' first API calls.
POP_STARTUP_JITTER_MIN_SECONDS = 30
POP_STARTUP_JITTER_MAX_SECONDS = 300  # 5 minutes

# Light jitter added to a normally-scheduled refresh so many installations do not hit the API at
# the same wall-clock instant once their data expires together.
POP_SCHEDULE_JITTER_SECONDS = 3600  # up to 1 hour

# Exponential backoff after a failed attempt: 15 min, 30, 60, ... capped at 24 h. A Retry-After
# from the provider (honoured inside _fetch_with_retry) governs the in-run wait; this governs the
# gap between whole attempts so repeated restarts during an outage cannot cause a download loop.
POP_BACKOFF_BASE_SECONDS = 15 * 60  # 15 minutes
POP_BACKOFF_MAX_SECONDS = 24 * 3600  # 24 hours

# Catch-up delay for a refresh that was deferred because a restore held the maintenance lock.
POP_MAINTENANCE_RETRY_SECONDS = 60


@dataclass(frozen=True)
class RefreshPlan:
    """Decision of the scheduler for the next loop iteration.

    ``delay_seconds`` is how long to wait before attempting the next refresh; ``0`` means a refresh
    is due now. ``reason`` is a short, log-friendly label for why that delay was chosen.
    """

    delay_seconds: float
    reason: str


def _backoff_seconds(consecutive_failures: int) -> float:
    """Exponential backoff for ``consecutive_failures`` prior failures, capped at the 24 h max."""
    if consecutive_failures <= 0:
        return 0.0
    # Cap the exponent so a long outage cannot overflow the float power.
    exponent = min(consecutive_failures - 1, 16)
    return min(POP_BACKOFF_BASE_SECONDS * (2.0**exponent), POP_BACKOFF_MAX_SECONDS)


def _startup_offset(rng: random.Random, include_jitter: bool) -> float:
    """Short randomised offset before a first import, so many installations do not call the API together."""
    if not include_jitter:
        return POP_STARTUP_JITTER_MIN_SECONDS
    return rng.uniform(POP_STARTUP_JITTER_MIN_SECONDS, POP_STARTUP_JITTER_MAX_SECONDS)


def compute_next_action(
    *,
    now: datetime,
    last_success_at: datetime | None,
    last_attempt_at: datetime | None,
    consecutive_failures: int,
    rng: random.Random | None = None,
    include_jitter: bool = True,
    ixp_snapshot_missing: bool = False,
) -> RefreshPlan:
    """Decide whether a PeeringDB refresh is due and, if not, how long to wait.

    Pure function of the persisted state so the same decision is reached after any restart:

    - No successful import yet: attempt soon, after a short randomised startup offset (first
      install downloads promptly without a rigid wait, but still spreads load).
    - A prior failure streak: wait the exponential backoff measured from the last attempt, so
      repeated restarts during an outage resume the backoff instead of retrying immediately.
    - No IXP snapshot yet (an upgrade added the IXP tables): the facility import's freshness does not
      count; the first IXP import follows after the startup offset, never faster than the base backoff
      after the previous attempt, so an import that cannot produce a snapshot cannot loop.
    - Fresh data: wait until the freshness window expires, plus light jitter.
    - Overdue data: refresh now.
    """
    rng = rng or random
    if consecutive_failures > 0 and last_attempt_at is not None:
        backoff = _backoff_seconds(consecutive_failures)
        elapsed = (now - last_attempt_at).total_seconds()
        remaining = backoff - elapsed
        if remaining > 0:
            return RefreshPlan(remaining, f"backoff after {consecutive_failures} failure(s)")
        return RefreshPlan(0.0, f"retry after backoff ({consecutive_failures} failure(s))")

    if last_success_at is None:
        return RefreshPlan(_startup_offset(rng, include_jitter), "first install: no successful external import yet")

    if ixp_snapshot_missing:
        wait = _startup_offset(rng, include_jitter)
        if last_attempt_at is not None:
            wait = max(wait, POP_BACKOFF_BASE_SECONDS - (now - last_attempt_at).total_seconds())
        return RefreshPlan(wait, "IXP snapshot missing: first IXP import due")

    age = (now - last_success_at).total_seconds()
    remaining = POP_FRESHNESS_SECONDS - age
    if remaining > 0:
        jitter = rng.uniform(0, POP_SCHEDULE_JITTER_SECONDS) if include_jitter else 0
        return RefreshPlan(remaining + jitter, "data still fresh")
    return RefreshPlan(0.0, "freshness window expired")


def _attempt_timestamp(run: dict | None) -> datetime | None:
    """Best-effort attempt time from an import-run row (finished, else started)."""
    if not run:
        return None
    return parse_utc(run.get("finished_at")) or parse_utc(run.get("started_at"))


def get_scheduler_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read the self-managing refresh state for PeeringDB from the import history.

    Returns the data the scheduler decides on and the diagnostics exposed through the admin status
    endpoint: last successful update, last attempt, its outcome, and the consecutive-failure count.
    """
    last_success = pop_locations.get_last_import_run(conn, SOURCE_PEERINGDB)
    last_attempt = pop_locations.get_last_import_attempt(conn, SOURCE_PEERINGDB)
    consecutive_failures = pop_locations.count_consecutive_failures(conn, SOURCE_PEERINGDB)
    return {
        "source": SOURCE_PEERINGDB,
        "last_success_at": last_success.get("finished_at") or last_success.get("started_at") if last_success else None,
        "last_attempt_at": (last_attempt.get("finished_at") or last_attempt.get("started_at"))
        if last_attempt
        else None,
        "last_attempt_status": last_attempt.get("status") if last_attempt else None,
        "consecutive_failures": consecutive_failures,
    }


def _ixp_snapshot_missing(conn: sqlite3.Connection) -> bool:
    """True while no usable IXP snapshot exists; an unreadable table is not treated as missing."""
    try:
        snapshot = ixp_prefixes.snapshot_status(conn)
    except sqlite3.Error:
        return False
    return snapshot is None or not snapshot["completed_at"] or not snapshot["prefix_count"]


def plan_next_refresh(
    conn: sqlite3.Connection, *, rng: random.Random | None = None, include_jitter: bool = True
) -> RefreshPlan:
    """Compute the next refresh action from the persisted PeeringDB import history."""
    last_success = pop_locations.get_last_import_run(conn, SOURCE_PEERINGDB)
    last_attempt = pop_locations.get_last_import_attempt(conn, SOURCE_PEERINGDB)
    consecutive_failures = pop_locations.count_consecutive_failures(conn, SOURCE_PEERINGDB)
    return compute_next_action(
        now=utcnow(),
        last_success_at=_attempt_timestamp(last_success),
        last_attempt_at=_attempt_timestamp(last_attempt),
        consecutive_failures=consecutive_failures,
        rng=rng,
        include_jitter=include_jitter,
        ixp_snapshot_missing=_ixp_snapshot_missing(conn),
    )


# Full refresh


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

    total_processed = sum(r.processed for r in results)
    total_inserted = sum(r.inserted for r in results)
    total_updated = sum(r.updated for r in results)
    total_retired = sum(r.retired for r in results)
    total_errors = sum(r.validation_errors for r in results)
    failures = [r for r in results if not r.success and r.error != ERROR_NOT_IMPLEMENTED]

    _log.info(
        "Full refresh complete: processed=%d, inserted=%d, updated=%d, "
        "retired=%d, validation_errors=%d, source_failures=%d",
        total_processed,
        total_inserted,
        total_updated,
        total_retired,
        total_errors,
        len(failures),
    )

    return results


def refresh_external_sources(get_conn) -> list[PopRefreshResult]:
    """Refresh only the sources with a real external import (PeeringDB).

    The self-managing scheduler calls this, not ``run_full_refresh``: the UN/LOCODE refresh is a
    placeholder with no data import, so running it on a schedule would spawn an empty job and must
    not count as a successful external-data update.
    """
    return [refresh_from_peeringdb(get_conn)]


def get_refresh_status(get_conn) -> dict:
    """
    Get current POP refresh status and statistics.

    Returns:
        Dict with counts, last run info and the self-managing scheduler state (last success/attempt,
        outcome, consecutive failures and the next planned attempt).
    """
    with _db_connection(get_conn) as conn:
        scheduler_state = get_scheduler_state(conn)
        plan = plan_next_refresh(conn, include_jitter=False)
        next_attempt_at = (utcnow() + timedelta(seconds=plan.delay_seconds)).isoformat()
        jitter_seconds = 0
        if not scheduler_state["consecutive_failures"]:
            if not scheduler_state["last_success_at"] or _ixp_snapshot_missing(conn):
                jitter_seconds = POP_STARTUP_JITTER_MAX_SECONDS - POP_STARTUP_JITTER_MIN_SECONDS
            elif plan.delay_seconds > 0:
                jitter_seconds = POP_SCHEDULE_JITTER_SECONDS
        return {
            "active_locations": pop_locations.count_active_pop_locations(conn),
            "retired_locations": pop_locations.count_retired_pop_locations(conn),
            "total_locations": pop_locations.get_pop_location_count(conn),
            "last_peeringdb_run": pop_locations.get_last_import_run(conn, SOURCE_PEERINGDB),
            "last_unlocode_run": pop_locations.get_last_import_run(conn, SOURCE_UNLOCODE),
            "ixp_snapshot": ixp_prefixes.snapshot_status(conn),
            "recent_runs": pop_locations.get_import_run_stats(conn, limit=5),
            "auto_refresh": {
                **scheduler_state,
                "next_attempt_at": next_attempt_at,
                "next_attempt_jitter_seconds": jitter_seconds,
                "next_attempt_reason": plan.reason,
                "freshness_days": POP_FRESHNESS_SECONDS // (24 * 3600),
            },
            "locks": {
                SOURCE_PEERINGDB: _get_source_lock(SOURCE_PEERINGDB).locked(),
                SOURCE_UNLOCODE: _get_source_lock(SOURCE_UNLOCODE).locked(),
            },
        }
