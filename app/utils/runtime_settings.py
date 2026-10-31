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
_path_memo: tuple[str | None, Path] | None = None


def clear_runtime_cache() -> None:
    """Drop all cached values (called on every settings write)."""
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()


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


def _read(key: str, db_path: Path) -> Any:
    if not db_path.exists():
        return None
    conn = sqlite_db.connect(db_path)
    try:
        return sqlite_db.get_setting(conn, key, None)
    finally:
        sqlite_db.close_connection(conn)


def get_runtime_setting(key: str | SettingKeys, default: Any = None) -> Any:
    """Return the stored value of a setting or ``default`` (also when the DB is unavailable)."""
    key_str = str(getattr(key, "value", key))
    try:
        db_path = _resolve_db_path()
        ident = str(db_path)
    except Exception:  # noqa: BLE001 - config problems must never break callers
        return default
    now = time.monotonic()
    with _lock:
        hit = _cache.get((ident, key_str))
        if hit is not None and now < hit[1]:
            return default if hit[0] is None else hit[0]
        generation = _generation
    try:
        value = _read(key_str, db_path)
    except Exception as exc:  # noqa: BLE001 - settings are best-effort for background code
        _log.debug("Runtime setting %s unavailable: %s", key_str, type(exc).__name__)
        return default
    with _lock:
        if generation == _generation:
            _cache[(ident, key_str)] = (value, now + RUNTIME_CACHE_TTL_SECONDS)
    return default if value is None else value


def get_runtime_bool(key: str | SettingKeys, default: bool) -> bool:
    """Read a boolean setting."""
    value = get_runtime_setting(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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
