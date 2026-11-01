#!/usr/bin/env python3
#
# app/services/pop_codes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Provider POP (Point of Presence) code lookups for hostname-based geolocation.

Architecture:
- Primary: SQLite pop_locations table
- Provider-specific parsers: Level3, NTT, GTT, Cogent, etc.
- Fallback: embedded data (pop_codes_embedded)
- SQL-based lookup with priority/confidence ranking

Lookup strategy (in order):
0) Provider-specific parser (highest confidence for known carriers)
1) Hostname substring match against alias_code
2) Direct token match against alias_code
3) Prefix match (fra3 → fra)
4) Embedded fallback

Scope:
- EU + US + APAC
- Backbone carriers, IXPs, major DC providers
- Conservative matching to avoid false positives
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from typing import TypedDict

from ..db import sqlite as sqlite_db
from ..db.pop_locations import (
    lookup_pop_by_hostname,
    lookup_pop_by_prefix,
    lookup_pop_by_tokens,
)
from .pop_codes_embedded import extract_pop_tokens
from .pop_codes_embedded import lookup_embedded as _lookup_embedded

_log = logging.getLogger(__name__)


class Location(TypedDict, total=False):
    """Structured POP location result."""

    city: str
    country: str
    lat: float
    lon: float
    confidence: float
    source: str
    matched_token: str
    hostname: str


# ---------------------------------------------------------------------------
# Rate-limited logging for lookup misses (prevent log spam)
# ---------------------------------------------------------------------------

_MISS_LOG_INTERVAL_SECONDS = 60
_MISS_SAMPLE_RATE = 100

_lookup_miss_count: int = 0
_lookup_miss_last_log: float = 0.0


def _log_lookup_miss_sampled(tokens: list[str], hostname: str) -> None:
    # NOTE: counters are intentionally racy; duplicate logs are acceptable
    # and cheaper than synchronization in hot paths.
    """Log a lookup miss with rate limiting (sampling-based)."""
    global _lookup_miss_count, _lookup_miss_last_log

    _lookup_miss_count += 1
    now = time.monotonic()

    should_log = (
        _lookup_miss_count % _MISS_SAMPLE_RATE == 0 or (now - _lookup_miss_last_log) > _MISS_LOG_INTERVAL_SECONDS
    )

    if should_log:
        _log.debug(
            "POP lookup miss #%d (sampled 1/%d): tokens=%s hostname=%s",
            _lookup_miss_count,
            _MISS_SAMPLE_RATE,
            tokens[:3],
            hostname[:50],
        )
        _lookup_miss_last_log = now


# ---------------------------------------------------------------------------
# Connection factory (set by app on startup)
# ---------------------------------------------------------------------------

_pop_get_conn: Callable[[], sqlite3.Connection] | None = None


def set_pop_db_connection_factory(factory) -> None:
    # NOTE: This setter is expected to be called during single-threaded startup.
    # Subsequent lookups assume the factory reference is stable.
    """
    Set the database connection factory for SQL-based POP lookups.

    The factory should return a context-manager-compatible connection.
    Call this once on app startup after schema init.

    Validates factory immediately to fail-fast on misconfiguration.

    Args:
        factory: Callable that returns a sqlite3.Connection (context manager)

    Raises:
        RuntimeError: If factory is None or doesn't return a connection
    """
    global _pop_get_conn

    if factory is None:
        raise RuntimeError("POP DB connection factory cannot be None")

    # Validate factory works (fail-fast at startup, not first lookup)
    try:
        conn = factory()
        try:
            conn.execute("SELECT 1 FROM pop_locations LIMIT 1")
        finally:
            sqlite_db.close_connection(conn)
    except Exception as e:
        raise RuntimeError(f"POP DB connection factory validation failed: {e}") from e

    _pop_get_conn = factory
    _log.debug("POP codes: SQL lookup enabled (factory validated)")


def _get_pop_conn():
    """Get a new connection from the factory."""
    if _pop_get_conn is None:
        raise RuntimeError("POP DB connection factory not set")
    return _pop_get_conn()


# ---------------------------------------------------------------------------
# Main lookup function
# ---------------------------------------------------------------------------


def lookup_pop_code(hostname: str, conn: sqlite3.Connection | None = None) -> Location | None:
    """
    Lookup POP location by hostname using SQLite.

    Uses multi-strategy lookup:
    0) Provider-specific parser (highest confidence for known carriers)
    1) Hostname substring match against alias_code
    2) Direct token match against alias_code
    3) Prefix match (fra3 → fra)
    4) Embedded fallback

    Args:
        hostname: The hostname to lookup
        conn: Optional SQLite connection (uses factory if not provided)

    Returns:
        Location dict with metadata, or None if not found.
    """
    if not hostname:
        return None

    h = hostname.lower()

    # ── Strategy 0: Provider-specific parser (highest confidence) ──
    from .pop_codes_providers import clli_city, parse_provider_hostname

    provider_result = parse_provider_hostname(h)
    if provider_result and provider_result.confidence >= 0.8:
        # Validate against DB to get coordinates
        owns_db = conn is None and _pop_get_conn is not None
        db = _get_pop_conn() if owns_db else conn
        try:
            if db:
                loc = lookup_pop_by_tokens(db, [provider_result.pop_code])
                if loc:
                    return {
                        "city": loc["city"],
                        "country": loc["country"],
                        "lat": loc["lat"],
                        "lon": loc["lon"],
                        "confidence": provider_result.confidence,
                        "source": f"provider_{provider_result.provider}",
                        "matched_token": provider_result.pop_code,
                        "hostname": hostname,
                    }
            # Embedded and CLLI data live in Python, so they need no DB connection.
            # If not in DB, try to use the provider result with embedded fallback
            embedded_result = _lookup_embedded(hostname)
            if embedded_result:
                # Override source to indicate provider parser was used
                embedded_result["source"] = f"provider_{provider_result.provider}_embedded"
                return embedded_result
            # CLLI codes carry their own coordinates; neither the DB nor the embedded table has them.
            clli = clli_city(provider_result.pop_code) if provider_result.provider == "clli" else None
            if clli:
                return {
                    "city": clli[0],
                    "country": clli[1],
                    "lat": clli[2],
                    "lon": clli[3],
                    "confidence": provider_result.confidence,
                    "source": "provider_clli",
                    "matched_token": provider_result.pop_code,
                    "hostname": hostname,
                }
        finally:
            if owns_db and db is not None:
                sqlite_db.close_connection(db)

    # ── Generic token extraction for DB lookup ──
    tokens = extract_pop_tokens(h)  # longest first, deterministic

    if not tokens:
        _log.debug("No POP tokens extracted from hostname: %s", hostname)
        return None

    if conn is not None:
        return _lookup_pop_sql(conn, tokens, hostname)

    if _pop_get_conn is not None:
        db = _get_pop_conn()
        try:
            return _lookup_pop_sql(db, tokens, hostname)
        finally:
            sqlite_db.close_connection(db)

    # No connection available - try embedded fallback
    _log.debug("POP lookup: using embedded fallback (no DB connection)")
    return _lookup_embedded(hostname)


# Rate-limited warning for DB errors (prevent log spam on transient issues)
_DB_ERROR_LOG_INTERVAL = 60.0
_db_error_last_log: float = 0.0


def _lookup_pop_sql(
    conn: sqlite3.Connection,
    tokens: list[str],
    hostname: str,
) -> Location | None:
    """
    SQL-based POP lookup with priority ranking.

    Strategy (in order of preference):
    1) Hostname substring match (most reliable for embedded codes like frf1)
    2) Direct token match against alias_code (extracted tokens)
    3) Prefix match (fra3 → fra, as last resort)

    IMPORTANT: All lookup functions guarantee `WHERE retired = 0` filtering.
    This is enforced in the DB layer (pop_locations.py).
    """
    global _db_error_last_log

    def _build_location(result: dict, source: str) -> Location:
        return {
            "city": result["city"],
            "country": result["country"],
            "lat": result["lat"],
            "lon": result["lon"],
            "confidence": result["confidence"],
            "source": source,
            "matched_token": result.get("matched_token", result["alias_code"]),
            "hostname": hostname,
        }

    try:
        # Strategy 1: Direct hostname substring match (best for frf1, fra04d, wup01)
        result = lookup_pop_by_hostname(conn, hostname)
        if result:
            return _build_location(result, "sql_hostname")

        # Strategy 2: Direct token match (for clean hostnames where tokens are isolated)
        result = lookup_pop_by_tokens(conn, tokens)
        if result:
            return _build_location(result, f"sql_{result['source']}")

        # Strategy 3: Prefix match (fra3 → fra, last resort)
        result = lookup_pop_by_prefix(conn, tokens)
        if result:
            return _build_location(result, "sql_prefix")

        # Strategy 4: embedded fallback
        embedded_result = _lookup_embedded(hostname)
        if embedded_result:
            return embedded_result

        _log_lookup_miss_sampled(tokens, hostname)
        return None

    except sqlite3.OperationalError as e:
        # Transient DB issues (lock timeout, etc.) - degrade gracefully
        now = time.monotonic()
        if (now - _db_error_last_log) > _DB_ERROR_LOG_INTERVAL:
            _log.warning("POP lookup DB error (rate-limited): %s", e)
            _db_error_last_log = now

        return _lookup_embedded(hostname)


__all__ = [
    "extract_pop_tokens",
    "lookup_pop_code",
    "set_pop_db_connection_factory",
]
