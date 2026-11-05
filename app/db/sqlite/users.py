#!/usr/bin/env python3
#
# app/db/sqlite/users.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""User accounts: lookup, creation, update, password and preferences."""

from __future__ import annotations

import sqlite3
from typing import Any

from ...utils import bootstrap_credentials
from ...utils.time import utcnow
from .connection import run_after_commit, transaction


def _can_close_alerts_value(*, is_admin: bool, can_close_alerts: bool) -> int:
    """Stored can_close_alerts flag: admins always hold the permission."""
    return 1 if is_admin or can_close_alerts else 0


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password_hash: str,
    is_admin: bool,
    can_close_alerts: bool = False,
    must_change_password: bool = False,
) -> int:
    """Create a user and return the new user id."""
    with transaction(conn):
        can_close_value = _can_close_alerts_value(is_admin=is_admin, can_close_alerts=can_close_alerts)
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, is_admin, can_close_alerts, is_active, "
            "must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                username.lower(),
                password_hash,
                1 if is_admin else 0,
                can_close_value,
                1,
                1 if must_change_password else 0,
                utcnow(),
            ),
        )
        return int(cur.lastrowid)


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    """Get a user row by username (case-insensitive)."""
    return conn.execute("SELECT * FROM users WHERE username = ?", (username.lower(),)).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Get a user row by id."""
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_theme(conn: sqlite3.Connection, user_id: int) -> str:
    """Return a user's theme preference."""
    row = conn.execute("SELECT theme FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["theme"] if row else "system"


def set_user_theme(conn: sqlite3.Connection, user_id: int, theme: str) -> None:
    """Persist a user's theme preference."""
    with transaction(conn):
        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Alias for get_user_by_id for consistency."""
    return get_user_by_id(conn, user_id)


def get_all_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get all users ordered by username."""
    return conn.execute("SELECT * FROM users ORDER BY username").fetchall()


def delete_user(conn: sqlite3.Connection, user_id: int) -> bool:
    """Delete a user. Associated tokens and alert assignments are
    automatically removed via ON DELETE CASCADE.

    Returns True if user was deleted, False if not found.
    """
    with transaction(conn):
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cursor.rowcount > 0


def get_first_admin(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Get the first admin user (used as fallback when auth is disabled)."""
    return conn.execute("SELECT * FROM users WHERE is_admin = 1 AND is_active = 1 ORDER BY id LIMIT 1").fetchone()


def update_user(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    username: str,
    is_admin: bool,
    can_close_alerts: bool = False,
) -> None:
    """Update a user's basic fields."""
    with transaction(conn):
        can_close_value = _can_close_alerts_value(is_admin=is_admin, can_close_alerts=can_close_alerts)
        conn.execute(
            "UPDATE users SET username = ?, is_admin = ?, can_close_alerts = ? WHERE id = ?",
            (username.lower(), 1 if is_admin else 0, can_close_value, user_id),
        )


def set_user_active(conn: sqlite3.Connection, user_id: int, *, is_active: bool) -> None:
    """Activate/deactivate a user."""
    with transaction(conn):
        conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, user_id),
        )


def update_user_password(
    conn: sqlite3.Connection,
    user_id: int,
    password_hash: str,
    *,
    must_change_password: bool | None = None,
    expected_hash: str | None = None,
) -> bool:
    """Update a user's password hash and optionally the must-change flag.

    ``expected_hash`` turns the write into a compare-and-swap so a concurrent
    password change is not overwritten. Returns True if a row was updated.
    """
    sets = ["password_hash = ?"]
    params: list[Any] = [password_hash]
    if must_change_password is not None:
        sets.append("must_change_password = ?")
        params.append(1 if must_change_password else 0)
    where = "id = ?"
    params.append(user_id)
    if expected_hash is not None:
        where += " AND password_hash = ?"
        params.append(expected_hash)
    with transaction(conn):
        cursor = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE {where}", params)  # noqa: S608 - fixed column names only
        updated = cursor.rowcount > 0
        if updated:
            _discard_bootstrap_credentials_for(conn, user_id)
    return updated


def _discard_bootstrap_credentials_for(conn: sqlite3.Connection, user_id: int) -> None:
    """Drop the retrievable initial password once its account got a new one.

    Must run inside the transaction() that changed the password: the actual file unlink is
    deferred via run_after_commit() so it only happens once the OUTERMOST commit succeeds. If
    the enclosing transaction rolls back (e.g. an outer transaction fails after this call), the
    callback is dropped and the file survives, keeping the file and the DB password consistent.
    The username decision is read here, under the same transaction, so it reflects the state the
    commit will persist. If the real transaction was opened by someone else (foreign), its commit
    cannot be observed and run_after_commit() drops the callback with a warning - the safe
    direction, since that only risks showing the bootstrap password once more, never deleting it
    while the DB still holds the old one.
    """
    pending = bootstrap_credentials.configured_path()
    if pending is None or not pending.exists():
        return
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    stored = bootstrap_credentials.stored_username()
    if row is not None and stored in (None, row["username"]):
        run_after_commit(conn, bootstrap_credentials.discard)


def update_user_last_login(conn: sqlite3.Connection, user_id: int, ip_address: str | None = None) -> None:
    """Update the last_login_at timestamp and IP address for a user."""
    with transaction(conn):
        conn.execute(
            "UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
            (utcnow(), ip_address, user_id),
        )
