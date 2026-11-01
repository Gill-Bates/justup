#!/usr/bin/env python3
#
# app/utils/runtime_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Connection-free runtime access to UI-managed settings.

Code without a request-scoped DB connection (rate limiter, probes, lookups, background
loops) reads its tunables here. Values come from the settings table, are cached for a
few seconds and dropped on every settings write, so UI changes apply without a restart.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..db import sqlite as sqlite_db
from .constants import SettingKeys
from .crypto import decrypt_secret

_log = logging.getLogger(__name__)

RUNTIME_CACHE_TTL_SECONDS = 5.0
_cache: dict[tuple[str, str], tuple[Any, float]] = {}
_lock = threading.Lock()
# Bumped on every invalidation; a fill is published only if no invalidation ran since its read began.
_generation = 0
# Last successfully read value per key; survives cache expiry, dropped on every settings write.
_last_good: dict[tuple[str, str], Any] = {}
_path_memo: tuple[str | None, Path] | None = None


def clear_runtime_cache() -> None:
    """Drop all cached values (called on every settings write)."""
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()
        _last_good.clear()


def _resolve_db_path() -> Path:
    """DB path, resolved via load_config() (.env, directory creation) only when the data-dir env changes."""
    from .config import load_config

    global _path_memo
    env = os.environ.get("JUSTUP_DATA_DIR")
    memo = _path_memo
    if memo is not None and memo[0] == env:
        return memo[1]
    db_path = load_config().db_path
    # Key on the env value seen after load_config(): it may have been set from .env just now.
    _path_memo = (os.environ.get("JUSTUP_DATA_DIR"), db_path)
    return db_path


def _db_ident(db_path: Path) -> str:
    """Stable identity of a database: the resolved path; in-memory databases keep their literal name."""
    raw = str(db_path)
    if raw == ":memory:" or raw.startswith("file::memory:"):
        return raw
    try:
        return str(db_path.resolve())
    except (OSError, RuntimeError):
        return str(Path(raw).absolute())


def _read(key: str, db_path: Path) -> Any:
    if not db_path.exists():
        return None
    conn = sqlite_db.connect(db_path)
    try:
        return sqlite_db.get_setting(conn, key, None)
    finally:
        sqlite_db.close_connection(conn)


def _lookup(key_str: str, *, strict: bool = False) -> tuple[Any, bool]:
    """Return (stored value or None, readable); ``readable`` is False when the store could not be read.

    ``strict`` also treats a missing database file as unreadable instead of as "no value stored".
    """
    try:
        db_path = _resolve_db_path()
        ident = _db_ident(db_path)
    except Exception:  # noqa: BLE001 - config problems must never break callers
        return None, False
    now = time.monotonic()
    with _lock:
        hit = _cache.get((ident, key_str))
        if hit is not None and now < hit[1]:
            return hit[0], True
        generation = _generation
    try:
        if strict and not db_path.exists():
            raise FileNotFoundError
        value = _read(key_str, db_path)
    except Exception as exc:  # noqa: BLE001 - settings are best-effort for background code
        _log.debug("Runtime setting %s unavailable: %s", key_str, type(exc).__name__)
        return None, False
    with _lock:
        if generation == _generation:
            _cache[(ident, key_str)] = (value, now + RUNTIME_CACHE_TTL_SECONDS)
            _last_good[(ident, key_str)] = value
    return value, True


def get_runtime_setting(key: str | SettingKeys, default: Any = None) -> Any:
    """Return the stored value of a setting or ``default`` (also when the DB is unavailable)."""
    value, _readable = _lookup(str(getattr(key, "value", key)))
    return default if value is None else value


def get_runtime_bool(key: str | SettingKeys, default: bool) -> bool:
    """Read a boolean setting."""
    value = get_runtime_setting(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def get_runtime_bool_fail_closed(key: str | SettingKeys, default: bool) -> bool:
    """Read a boolean privacy switch; ``default`` applies only when the store was read and holds no value.

    An unreadable store (database missing, locked or being restored) yields the last value read in
    this process for the same database, else False, so an opt-out is never lost and no data leaves
    the host unchecked.
    """
    key_str = str(getattr(key, "value", key))
    value, readable = _lookup(key_str, strict=True)
    if not readable:
        try:
            ident = _db_ident(_resolve_db_path())
        except Exception:  # noqa: BLE001 - unknown database: fail closed
            ident = None
        with _lock:
            known = None if ident is None else _last_good.get((ident, key_str))
        _log.warning("Setting %s is unreadable; using %s", key_str, "last known value" if known is not None else "off")
        value = False if known is None else known
    elif value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)


def get_runtime_int(key: str | SettingKeys, default: int, *, minimum: int, maximum: int) -> int:
    """Read an integer setting; invalid or out-of-range values fall back to ``default``."""
    try:
        value = int(get_runtime_setting(key, default))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def get_ip_api_key() -> str:
    """Return the decrypted ip-api key, or an empty string when none is stored or readable."""
    stored = get_runtime_setting(SettingKeys.IP_API_KEY, "")
    if not stored:
        return ""
    try:
        return decrypt_secret(str(stored)).strip()
    except Exception:  # noqa: BLE001 - never log secret material
        _log.warning("Stored ip-api key could not be decrypted; ignoring it")
        return ""
