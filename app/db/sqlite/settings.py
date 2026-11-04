#!/usr/bin/env python3
#
# app/db/sqlite/settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Key/value application settings stored in SQLite."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ...utils.time import utcnow
from .connection import _TX_LOCK, _TX_STATES, run_after_commit, transaction


def set_setting(conn: sqlite3.Connection, key: str, value: Any, *, auto_commit: bool = True) -> None:
    """Persist a setting value and invalidate the in-process settings cache.

    Args:
            value: Setting value (will be JSON-serialized)
            auto_commit: If True, wraps in transaction() (default; nests via savepoint). Set False when
                the caller commits itself; the cache is then invalidated after the commit of an enclosing
                transaction() or, outside one, NOT here and the caller must invalidate after its own commit.
    """
    payload = json.dumps(value)

    def _execute():
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, payload, utcnow()),
        )

    from ...utils.settings_cache import invalidate_settings_cache, mark_setting_uncacheable

    key_str = key.value if hasattr(key, "value") else str(key)

    with _TX_LOCK:
        state = _TX_STATES.get(id(conn))
    foreign = conn.in_transaction and (state is None or state.foreign)
    if foreign:
        mark_setting_uncacheable(conn, key_str)

    if not auto_commit:
        # A foreign commit cannot be observed; bypass caching for this database/key instead.
        _execute()
        with _TX_LOCK:
            state = _TX_STATES.get(id(conn))
        if state is not None and not state.foreign:
            run_after_commit(conn, lambda: invalidate_settings_cache(key_str))
        elif state is None:
            mark_setting_uncacheable(conn, key_str)
        return

    with transaction(conn):
        _execute()
        if not foreign:
            run_after_commit(conn, lambda: invalidate_settings_cache(key_str))


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Read a setting value from the database."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return json.loads(row["value"])


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    """Delete a setting key and invalidate its in-process cache entry after the commit."""
    from ...utils.settings_cache import invalidate_settings_cache, mark_setting_uncacheable

    key_str = key.value if hasattr(key, "value") else str(key)
    foreign = False
    if conn.in_transaction:
        with _TX_LOCK:
            state = _TX_STATES.get(id(conn))
        foreign = state is None or state.foreign
        if foreign:
            mark_setting_uncacheable(conn, key_str)
    with transaction(conn):
        conn.execute("DELETE FROM settings WHERE key=?", (key_str,))
        if not foreign:
            run_after_commit(conn, lambda: invalidate_settings_cache(key_str))
