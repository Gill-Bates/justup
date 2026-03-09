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
- Fallback: Cython embedded data (obfuscated, compiled)
- SQL-based lookup with priority/confidence ranking

Lookup strategy (in order):
0) Provider-specific parser (highest confidence for known carriers)
1) Hostname substring match against alias_code
2) Direct token match against alias_code
3) Prefix match (fra3 → fra)
4) Cython embedded fallback

Scope:
- EU + US + APAC
- Backbone carriers, IXPs, major DC providers
- Conservative matching to avoid false positives
"""

from __future__ import annotations

import logging
import sqlite3
import time
import re
import ipaddress
from collections.abc import Callable
from typing import Optional, TypedDict

from ..db.pop_locations import (
    lookup_pop_by_tokens, 
    lookup_pop_by_prefix,
    lookup_pop_by_hostname,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cython fallback (embedded POP data)
# ---------------------------------------------------------------------------

_CYTHON_AVAILABLE = False
_cython_extract_pop_tokens = None
_cython_lookup_embedded = None

try:
    from .._cython.pop_codes import (
        extract_pop_tokens as _cython_extract_pop_tokens,
        lookup_embedded as _cython_lookup_embedded,
        has_embedded_data,
    )
    if has_embedded_data():
        _CYTHON_AVAILABLE = True
        _log.debug("POP codes: Cython fallback loaded")
except ImportError:
    _log.debug("POP codes: Cython fallback not available (pure Python mode)")


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

_MISS_LOG_INTERVAL_SECONDS = 60  # Log at most every 60 seconds
_MISS_SAMPLE_RATE = 100  # Log every Nth miss

_lookup_miss_count: int = 0
_lookup_miss_last_log: float = 0.0


def _log_lookup_miss_sampled(tokens: list[str], hostname: str) -> None:
    # NOTE: counters are intentionally racy; duplicate logs are acceptable
    # and cheaper than synchronization in hot paths.
    """Log a lookup miss with rate limiting (sampling-based)."""
    global _lookup_miss_count, _lookup_miss_last_log
    
    _lookup_miss_count += 1
    now = time.monotonic()
    
    # Log every Nth miss OR if enough time has passed
    should_log = (
        _lookup_miss_count % _MISS_SAMPLE_RATE == 0 or
        (now - _lookup_miss_last_log) > _MISS_LOG_INTERVAL_SECONDS
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
# Token extraction (robust hostname parsing)
# ---------------------------------------------------------------------------

# Noise tokens to filter out during extraction
_NOISE_TOKENS = frozenset({
    "ip", "static", "dynamic", "cust", "customer", "client",
    "dsl", "fiber", "ftth", "fttb", "pool", "node", "host",
    "net", "com", "org", "de", "uk", "fr", "nl", "us",
    "ptr", "rev", "rdns", "in", "addr", "arpa",
    # Backbone/infrastructure tokens (not location indicators)
    "bb", "bb1", "bb2", "bb3", "bb4", "bb5", "bb6", "bb7", "bb8", "bb9",
    # IPv6 / IP-related
    "ip6", "ipv6",
    "b1", "b2", "b3", "b4", "b5", "b6", "b7", "b8", "b9",
    "link", "core", "edge", "agg", "pe", "ce", "ae",
})

# Pattern for extracting alphabetic prefix from provider hostnames
# Matches: dor1901aihd001 → "dor", fra3 → "fra", berlin1 → "berlin"
# Extended to 2-8 chars to support full city names (berlin, munich, london)
_ALPHA_PREFIX_PATTERN = re.compile(r'^([a-z]{2,8})\d')

_ALPHA_ONLY_PATTERN = re.compile(r"^([a-z]+)")

# Pattern for compact router names without separators:
# e.g. loncore1, amsedge2, berlinedge1 → extract "lon"/"ams"/"berlin"
_ROLE_AFTER_POP_PATTERN = re.compile(r"^([a-z]{2,8})(?:core|edge|agg|bb|gw|rtr|router|sw)\d")


def extract_pop_tokens(hostname: str) -> list[str]:
    """
    Extract candidate POP/location tokens from a hostname.
    
    Normalizes separators and filters out obvious noise.
    Returns tokens of 2-8 characters that could be POP codes or city names,
    sorted by length (longest first) then alphabetically for determinism.
    
    Short-circuits for:
    - Raw IPv4/IPv6 addresses (no POP codes in IP literals)
    - IPv6 reverse DNS (ip6.arpa nibbles are meaningless)
    - IPv4 reverse DNS (in-addr.arpa octets are meaningless)
    
    Token sources:
    1. Direct segments of 2-5 chars (e.g., "fra" from "fra.de-cix.net")
    2. Alphabetic prefixes from long tokens (e.g., "dor" from "dor1901aihd001")
       This handles typical provider naming: <pop><rack/port><function>
    """
    if not hostname:
        return []
    
    # ------------------------------------------------------------------
    # Short-circuit raw IP addresses (IPv4 / IPv6) and IPv6 reverse DNS
    # These are not hostnames and must never produce POP tokens.
    # ------------------------------------------------------------------
    cleaned = hostname.strip().strip("[]")
    try:
        ipaddress.ip_address(cleaned)
        return []
    except ValueError:
        pass

    # Also catch "ip:port" patterns (e.g., "192.168.1.1:8080")
    # Strip port suffix before IP check
    if ":" in cleaned and not cleaned.startswith("["):
        host_part = cleaned.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(host_part)
            return []
        except ValueError:
            pass

    if hostname.lower().endswith(".ip6.arpa"):
        return []

    if hostname.lower().endswith(".in-addr.arpa"):
        return []

    # Normalize: lowercase, replace common separators with dots
    parts = hostname.lower().replace("-", ".").replace("_", ".").split(".")
    
    tokens = set()
    for p in parts:
        if not p:
            continue

        # Filter out obvious router-role tokens even when they have digits (edge9, core1, ae1...)
        m_alpha = _ALPHA_ONLY_PATTERN.match(p)
        if m_alpha:
            alpha = m_alpha.group(1)
            if alpha in _NOISE_TOKENS:
                continue

        # Direct match: 2-8 chars, not pure digits, not noise
        # Extended to 8 to support full city names (berlin, munich, london)
        if 2 <= len(p) <= 8 and not p.isdigit() and p not in _NOISE_TOKENS:
            tokens.add(p)
        # Extract alphabetic prefix from longer tokens (provider hostname pattern)
        elif len(p) > 5:
            # Handle compact patterns like "loncore1" (no separators)
            mr = _ROLE_AFTER_POP_PATTERN.match(p)
            if mr:
                pop = mr.group(1)
                if pop not in _NOISE_TOKENS:
                    tokens.add(pop)

            # Extract alphabetic prefix and optional digit extension (max len 5)
            m = _ALPHA_PREFIX_PATTERN.match(p)
            if m:
                alpha_prefix = m.group(1)
                if alpha_prefix not in _NOISE_TOKENS:
                    tokens.add(alpha_prefix)

                    digits_start = len(alpha_prefix)
                    digits_budget = max(0, 5 - digits_start)
                    if digits_budget > 0:
                        digits = ""
                        for ch in p[digits_start:]:
                            if ch.isdigit() and len(digits) < digits_budget:
                                digits += ch
                            else:
                                break
                        if digits:
                            tokens.add(alpha_prefix + digits)
    
    # Sort: longer tokens first (nyc > ny), then alphabetically for stability
    return sorted(tokens, key=lambda t: (-len(t), t))


# ---------------------------------------------------------------------------
# Connection factory (set by app on startup)
# ---------------------------------------------------------------------------

_pop_get_conn: Optional[Callable[[], sqlite3.Connection]] = None


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
            conn.close()
    except Exception as e:
        raise RuntimeError(f"POP DB connection factory validation failed: {e}") from e
    
    _pop_get_conn = factory
    _log.debug("POP codes: SQL lookup enabled (factory validated)")


def clear_pop_db_connection_factory() -> None:
    """
    Clear the POP database connection factory.
    
    Call this during shutdown to prevent stale factory references
    across app restarts (especially in tests).
    """
    global _pop_get_conn
    _pop_get_conn = None
    _log.debug("POP codes: factory cleared")


def _get_pop_conn():
    """Get a new connection from the factory."""
    if _pop_get_conn is None:
        raise RuntimeError("POP DB connection factory not set")
    return _pop_get_conn()


# ---------------------------------------------------------------------------
# Main lookup function
# ---------------------------------------------------------------------------

def lookup_pop_code(hostname: str, conn: Optional[sqlite3.Connection] = None) -> Location | None:
    """
    Lookup POP location by hostname using SQLite.
    
    Uses multi-strategy lookup:
    0) Provider-specific parser (highest confidence for known carriers)
    1) Hostname substring match against alias_code
    2) Direct token match against alias_code
    3) Prefix match (fra3 → fra)
    4) Cython embedded fallback
    
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
    from .pop_codes_providers import parse_provider_hostname
    
    provider_result = parse_provider_hostname(h)
    if provider_result and provider_result.confidence >= 0.8:
        # Validate against DB to get coordinates
        db = conn or (_get_pop_conn() if _pop_get_conn else None)
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
            # If not in DB, try to use the provider result with Cython fallback
            if _CYTHON_AVAILABLE and _cython_lookup_embedded is not None:
                cython_result = _cython_lookup_embedded(hostname)
                if cython_result:
                    # Override source to indicate provider parser was used
                    cython_result["source"] = f"provider_{provider_result.provider}_embedded"
                    return cython_result
    
    # ── Generic token extraction for DB lookup ──
    tokens = extract_pop_tokens(h)  # longest first, deterministic
    
    if not tokens:
        _log.debug("No POP tokens extracted from hostname: %s", hostname)
        return None
    
    # Use provided connection or get from factory
    if conn is not None:
        return _lookup_pop_sql(conn, tokens, hostname)
    
    if _pop_get_conn is not None:
        db = _get_pop_conn()
        try:
            return _lookup_pop_sql(db, tokens, hostname)
        finally:
            db.close()
    
    # No connection available - try Cython fallback
    if _CYTHON_AVAILABLE and _cython_lookup_embedded is not None:
        _log.debug("POP lookup: using Cython fallback (no DB connection)")
        return _cython_lookup_embedded(hostname)
    
    _log.debug("POP lookup failed: no database connection available")
    return None


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
        
        # Strategy 4: Cython embedded fallback (obfuscated data)
        if _CYTHON_AVAILABLE and _cython_lookup_embedded is not None:
            cython_result = _cython_lookup_embedded(hostname)
            if cython_result:
                return cython_result
        
        # No match - log with rate limiting
        _log_lookup_miss_sampled(tokens, hostname)
        return None
        
    except sqlite3.OperationalError as e:
        # Transient DB issues (lock timeout, etc.) - degrade gracefully
        now = time.monotonic()
        if (now - _db_error_last_log) > _DB_ERROR_LOG_INTERVAL:
            _log.warning("POP lookup DB error (rate-limited): %s", e)
            _db_error_last_log = now
        
        # Try Cython fallback on DB error
        if _CYTHON_AVAILABLE and _cython_lookup_embedded is not None:
            return _cython_lookup_embedded(hostname)
        return None

__all__ = [
    "extract_pop_tokens",
    "lookup_pop_code",
    "set_pop_db_connection_factory",
    "clear_pop_db_connection_factory",
]
