#!/usr/bin/env python3
#
# app/utils/settings_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Centralized settings cache with TTL-based invalidation.

Reduces SQLite reads for frequently accessed settings like:
- use_utc_dashboard
- pdf_page_size
- purge_enabled

Usage:
    from app.utils.settings_cache import get_setting_cached, invalidate_settings_cache

    value = get_setting_cached(conn, "use_utc_dashboard", False)

Important Usage Contract:
    - This cache is per-process and NOT automatically invalidated on DB writes.
    - Callers MUST call invalidate_settings_cache(key) after updating settings in the DB
      (after the commit when the write runs inside an open transaction).
    - The cache is thread-safe via threading.Lock.
    - SQLite connections are thread-affine (check_same_thread=True by default).
      The 'conn' parameter should be obtained in the same thread that calls this function.
      For cross-thread usage, obtain a fresh connection per thread.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

from ..db import sqlite as sqlite_db

# Cache configuration
CACHE_TTL_SECONDS = 30  # Settings are cached for 30 seconds
CACHE_MAX_SIZE = 64  # Maximum number of cached entries

# Thread-safe cache storage
# Entries are keyed by (db_path, setting_key, ttl, repr(default)) and hold (value, expires_at, inserted_at).
_cache: dict[tuple[str, str, int, str], tuple[Any, float, float]] = {}
_cache_lock = threading.Lock()
# Bumped on every invalidation so a read that raced with a write never republishes its stale value
_generation = 0


def _db_identity(conn: sqlite3.Connection) -> str | None:
    """File path of the main database, or None for in-memory/temporary databases.

    In-memory databases have no stable shared identity, so they are never cached.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row[2]) if row and row[2] else None


def get_setting_cached(
    conn: sqlite3.Connection,
    key: str,
    default: Any = None,
    *,
    ttl: int = CACHE_TTL_SECONDS,
) -> Any:
    """
    Get a setting with caching.

    Args:
        conn: Database connection (must be used from the thread that created it)
        key: Setting key
        default: Default value if not found
        ttl: Time-to-live in seconds (default: 30)

    Returns:
        Setting value (cached or freshly fetched)

    Note:
        Cache key includes TTL, so the same setting requested with different
        TTLs will have separate cache entries with correct expiration behavior.
    """
    now = time.monotonic()
    identity = _db_identity(conn)
    if identity is None:
        return sqlite_db.get_setting(conn, key, default)
    cache_key = (identity, key, ttl, repr(default))

    with _cache_lock:
        if cache_key in _cache:
            value, expires_at, _ = _cache[cache_key]
            if now < expires_at:
                return value
        generation = _generation

    # Cache miss or expired - fetch from DB (outside the lock)
    value = sqlite_db.get_setting(conn, key, default)

    with _cache_lock:
        # An invalidation since the read started means the value may be stale: return it, do not publish it
        if generation == _generation:
            if len(_cache) >= CACHE_MAX_SIZE:
                _evict_oldest()
            _cache[cache_key] = (value, now + ttl, now)

    return value


def invalidate_settings_cache(key: str | None = None) -> None:
    """
    Invalidate cached settings.

    Args:
        key: Specific setting key to invalidate (all TTL variants), or None to clear all

    Note:
        When a specific key is provided, ALL cache entries for that key
        (regardless of TTL) are invalidated. This ensures consistency
        when a setting is updated in the database.
    """
    global _generation
    from .runtime_settings import clear_runtime_cache

    clear_runtime_cache()
    with _cache_lock:
        _generation += 1
        if key is None:
            _cache.clear()
        else:
            # Remove all entries matching this setting key (any TTL, database or default)
            keys_to_remove = [k for k in _cache if k[1] == key]
            for k in keys_to_remove:
                del _cache[k]


def _evict_oldest() -> None:
    """
    Evict the oldest 25% of cache entries by insertion order.

    Uses Python 3.7+ dict insertion order guarantee for simple LRU-like behavior.
    This is preferred over sorting by expiry time, which would unfairly penalize
    entries with longer TTLs even if they were recently inserted.
    """
    # Use insertion order (Python 3.7+ dict guarantee)
    all_keys = list(_cache.keys())
    evict_count = max(1, len(all_keys) // 4)

    for cache_key in all_keys[:evict_count]:
        del _cache[cache_key]


# Security-sensitive settings get shorter TTL
SECURITY_TTL_SECONDS = 5


# Convenience functions for common settings
def is_auth_disabled(conn: sqlite3.Connection) -> bool:
    """Check if authentication is disabled; always reads the DB (workers do not share the cache)."""
    return bool(sqlite_db.get_setting(conn, "auth_disabled", False))


def is_purge_enabled(conn: sqlite3.Connection) -> bool:
    """Check if TSDB purge is enabled."""
    return bool(get_setting_cached(conn, "purge_enabled", True))


def use_utc_dashboard(conn: sqlite3.Connection) -> bool:
    """Check if dashboard should use UTC times."""
    return bool(get_setting_cached(conn, "use_utc_dashboard", False))


def get_pdf_page_size(conn: sqlite3.Connection) -> str:
    """Get PDF page size setting."""
    return str(get_setting_cached(conn, "pdf_page_size", "a4") or "a4").lower()
