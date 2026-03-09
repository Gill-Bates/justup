# cython: language_level=3
# cython: boundscheck=False, wraparound=False

"""
Centralized settings cache with TTL-based invalidation.

Reduces SQLite reads for frequently accessed settings like:
- auth_disabled
- use_utc_dashboard
- signal_api_base_url
- pdf_page_size

Usage:
    from app.utils.settings_cache import get_setting_cached, invalidate_settings_cache
    
    value = get_setting_cached(conn, "use_utc_dashboard", False)

Important Usage Contract:
    - This cache is per-process and NOT automatically invalidated on DB writes.
    - Callers MUST call invalidate_settings_cache(key) after updating settings in the DB.
    - The cache is thread-safe via threading.Lock.
    - SQLite connections are thread-affine (check_same_thread=True by default).
      The 'conn' parameter should be obtained in the same thread that calls this function.
      For cross-thread usage, obtain a fresh connection per thread.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Optional

from ..db import sqlite as sqlite_db

# Cache configuration
CACHE_TTL_SECONDS = 30  # Settings are cached for 30 seconds
CACHE_MAX_SIZE = 64  # Maximum number of cached entries

# Thread-safe cache storage
# Key: (setting_key, ttl) -> ensures different TTLs don't share cache entries
# Value: (cached_value, expires_at, inserted_at)
_cache: dict[tuple[str, int], tuple[Any, float, float]] = {}
_cache_lock = threading.Lock()


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
    cache_key = (key, ttl)
    
    with _cache_lock:
        if cache_key in _cache:
            value, expires_at, _ = _cache[cache_key]
            if now < expires_at:
                return value
    
    # Cache miss or expired - fetch from DB
    value = sqlite_db.get_setting(conn, key, default)
    
    with _cache_lock:
        # Evict oldest entries if cache is full
        if len(_cache) >= CACHE_MAX_SIZE:
            _evict_oldest()
        
        _cache[cache_key] = (value, now + ttl, now)
    
    return value


def invalidate_settings_cache(key: Optional[str] = None) -> None:
    """
    Invalidate cached settings.
    
    Args:
        key: Specific setting key to invalidate (all TTL variants), or None to clear all
    
    Note:
        When a specific key is provided, ALL cache entries for that key
        (regardless of TTL) are invalidated. This ensures consistency
        when a setting is updated in the database.
    """
    with _cache_lock:
        if key is None:
            _cache.clear()
        else:
            # Remove all entries matching this setting key (any TTL)
            keys_to_remove = [k for k in _cache if k[0] == key]
            for k in keys_to_remove:
                del _cache[k]


def _evict_oldest() -> None:
    """
    Evict the oldest 25% of cache entries by insertion order.
    
    Uses Python 3.7+ dict insertion order guarantee for simple LRU-like behavior.
    This is preferred over sorting by expiry time, which would unfairly penalize
    entries with longer TTLs even if they were recently inserted.
    """
    if not _cache:
        return
    
    # Use insertion order (Python 3.7+ dict guarantee)
    all_keys = list(_cache.keys())
    evict_count = max(1, len(all_keys) // 4)
    
    for cache_key in all_keys[:evict_count]:
        del _cache[cache_key]


# Security-sensitive settings get shorter TTL
SECURITY_TTL_SECONDS = 5
_SECURITY_SETTINGS = frozenset({"auth_disabled"})


# Convenience functions for common settings
def is_auth_disabled(conn: sqlite3.Connection) -> bool:
    """Check if authentication is disabled (shorter TTL for security)."""
    return bool(get_setting_cached(conn, "auth_disabled", False, ttl=SECURITY_TTL_SECONDS))


def is_purge_enabled(conn: sqlite3.Connection) -> bool:
    """Check if TSDB purge is enabled."""
    return bool(get_setting_cached(conn, "purge_enabled", True))


def use_utc_dashboard(conn: sqlite3.Connection) -> bool:
    """Check if dashboard should use UTC times."""
    return bool(get_setting_cached(conn, "use_utc_dashboard", False))


def get_pdf_page_size(conn: sqlite3.Connection) -> str:
    """Get PDF page size setting."""
    return str(get_setting_cached(conn, "pdf_page_size", "a4") or "a4").lower()


def get_signal_api_base_url(conn: sqlite3.Connection) -> Optional[str]:
    """Get Signal API base URL."""
    return get_setting_cached(conn, "signal_api_base_url", None)


def get_signal_api_recipient(conn: sqlite3.Connection) -> Optional[str]:
    """Get Signal API recipient."""
    return get_setting_cached(conn, "signal_api_recipient", None)


def get_theme(conn: sqlite3.Connection) -> str:
    """Get UI theme setting."""
    return str(get_setting_cached(conn, "theme", "system") or "system")


def get_last_backup_at(conn: sqlite3.Connection) -> Optional[str]:
    """Get last backup timestamp."""
    return get_setting_cached(conn, "last_backup_at", None)


def get_feature_flags(conn: sqlite3.Connection) -> dict:
    """Get feature flags for conditional rendering."""
    return {
        "enable_uptime_charts": bool(get_setting_cached(conn, "enable_uptime_charts", True)),
        "enable_cert_monitoring_ui": bool(get_setting_cached(conn, "enable_cert_monitoring_ui", True)),
        "use_cached_status": bool(get_setting_cached(conn, "use_cached_status", True)),
    }

