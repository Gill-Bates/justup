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

    value = get_setting_cached(conn, SettingKeys.USE_UTC_DASHBOARD, False)

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
from .constants import DEFAULT_PDF_EXPORT_ENABLED, DEFAULT_PDF_PAGE_SIZE, DEFAULT_PURGE_ENABLED, SettingKeys

CACHE_TTL_SECONDS = 30
CACHE_MAX_SIZE = 64

# Entries are keyed by (db_path, setting_key, ttl, repr(default)) and hold (value, expires_at, inserted_at).
_cache: dict[tuple[str, str, int, str], tuple[Any, float, float]] = {}
_cache_lock = threading.Lock()
_uncacheable: set[tuple[str, str]] = set()
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
        uncacheable = (identity, key) in _uncacheable
        if not uncacheable and cache_key in _cache:
            value, expires_at, _ = _cache[cache_key]
            if now < expires_at:
                return value
        generation = _generation

    if uncacheable:
        return sqlite_db.get_setting(conn, key, default)

    # Cache miss or expired - fetch from DB (outside the lock)
    value = sqlite_db.get_setting(conn, key, default)

    with _cache_lock:
        # An invalidation since the read started means the value may be stale: return it, do not publish it
        if generation == _generation and (identity, key) not in _uncacheable:
            if len(_cache) >= CACHE_MAX_SIZE:
                _evict_oldest()
            _cache[cache_key] = (value, now + ttl, now)

    return value


def mark_setting_uncacheable(conn: sqlite3.Connection, key: str) -> None:
    """Bypass the cache when a caller owns an unobservable commit for this database/key."""
    global _generation
    identity = _db_identity(conn)
    if identity is None:
        return
    with _cache_lock:
        _uncacheable.add((identity, key))
        _generation += 1
        for cache_key in [entry for entry in _cache if entry[0] == identity and entry[1] == key]:
            _cache.pop(cache_key, None)


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


# Convenience functions for common settings
def is_auth_disabled(conn: sqlite3.Connection) -> bool:
    """Check if authentication is disabled; always reads the DB (workers do not share the cache)."""
    return bool(sqlite_db.get_setting(conn, SettingKeys.AUTH_DISABLED, False))


def is_auth_required(conn: sqlite3.Connection) -> bool:
    """Inverse of the stored `auth_disabled` flag; a missing value means authentication is required."""
    return not is_auth_disabled(conn)


def is_purge_enabled(conn: sqlite3.Connection) -> bool:
    """Check if TSDB purge is enabled."""
    return bool(get_setting_cached(conn, SettingKeys.PURGE_ENABLED, DEFAULT_PURGE_ENABLED))


def use_utc_dashboard(conn: sqlite3.Connection) -> bool:
    """Check if dashboard should use UTC times."""
    return bool(get_setting_cached(conn, SettingKeys.USE_UTC_DASHBOARD, False))


def is_pdf_export_enabled(conn: sqlite3.Connection) -> bool:
    """Check whether PDF export is on; always reads the DB so a toggle applies at once in every worker."""
    return bool(sqlite_db.get_setting(conn, SettingKeys.PDF_EXPORT_ENABLED, DEFAULT_PDF_EXPORT_ENABLED))


def get_pdf_page_size(conn: sqlite3.Connection) -> str:
    """Get PDF page size setting."""
    return str(
        get_setting_cached(conn, SettingKeys.PDF_PAGE_SIZE, DEFAULT_PDF_PAGE_SIZE) or DEFAULT_PDF_PAGE_SIZE
    ).lower()
