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
from .recipients import _insert_recipient, cancel_pending_notifications, update_recipient


def _can_close_alerts_value(*, is_admin: bool, can_close_alerts: bool) -> int:
    """Stored can_close_alerts flag: admins always hold the permission."""
    return 1 if is_admin or can_close_alerts else 0


def _insert_user_with_recipient(
    conn: sqlite3.Connection,
    *,
    username: str,
    password_hash: str,
    is_admin: bool,
    can_close_alerts: bool,
    must_change_password: bool,
    phone: str | None,
    email: str | None,
    recipient_enabled: bool | None,
    enable_for_all_targets: bool,
) -> int:
    """Insert a user and its one recipient; the only place that inserts into ``users``.

    Must run inside the caller's write transaction so both rows commit or roll back together. The recipient
    carries the username as its name and stays disabled while it has no contact channel.
    """
    can_close_value = _can_close_alerts_value(is_admin=is_admin, can_close_alerts=can_close_alerts)
    name = username.lower()
    cur = conn.execute(
        "INSERT INTO users(username, password_hash, is_admin, can_close_alerts, is_active, "
        "must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            password_hash,
            1 if is_admin else 0,
            can_close_value,
            1,
            1 if must_change_password else 0,
            utcnow(),
        ),
    )
    user_id = int(cur.lastrowid)
    # The database trigger created a disabled, channel-less recipient with the username as name; set its channels
    # through the same code path as every recipient edit (phone uniqueness, all-targets assignment).
    recipient = get_user_recipient(conn, user_id)
    if recipient is None:  # trigger missing: the layout check does not cover triggers, so stay safe
        _insert_recipient(
            conn,
            name=name,
            phone=None,
            email=None,
            is_enabled=False,
            enable_for_all_targets=False,
            user_id=user_id,
        )
        recipient = get_user_recipient(conn, user_id)
    update_recipient(
        conn,
        int(recipient["id"]),
        phone=phone or None,
        email=email or None,
        clear_phone=not phone,
        clear_email=not email,
        is_enabled=_recipient_enabled(phone, email, recipient_enabled),
        enable_for_all_targets=enable_for_all_targets,
    )
    return user_id


def _recipient_enabled(phone: str | None, email: str | None, requested: bool | None) -> bool:
    """A recipient is only enabled with a contact channel; unset means enabled when it has one.

    Raises:
        ValueError: enabling was requested explicitly although there is no channel.
    """
    has_channel = bool(phone or email)
    if requested and not has_channel:
        raise ValueError("A recipient without phone number and email address cannot be enabled")
    return has_channel and (True if requested is None else bool(requested))


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password_hash: str,
    is_admin: bool,
    can_close_alerts: bool = False,
    must_change_password: bool = False,
    phone: str | None = None,
    email: str | None = None,
    recipient_enabled: bool | None = None,
    enable_for_all_targets: bool = False,
) -> int:
    """Create a user together with its recipient (one transaction) and return the new user id.

    Without ``phone`` and ``email`` the recipient is created disabled and without channel.

    Raises:
        DuplicatePhoneError: another recipient already has this phone number (nothing is written).
        sqlite3.IntegrityError: the username exists.
    """
    with transaction(conn, immediate=True):
        return _insert_user_with_recipient(
            conn,
            username=username,
            password_hash=password_hash,
            is_admin=is_admin,
            can_close_alerts=can_close_alerts,
            must_change_password=must_change_password,
            phone=phone,
            email=email,
            recipient_enabled=recipient_enabled,
            enable_for_all_targets=enable_for_all_targets,
        )


def get_user_recipient(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Get the recipient bound to a user (None only for a user created by raw SQL, healed on the next save)."""
    return conn.execute("SELECT * FROM recipients WHERE user_id = ?", (user_id,)).fetchone()


def ensure_user_recipients(conn: sqlite3.Connection) -> int:
    """Give every user without a recipient a disabled, channel-less one; returns how many were created.

    Idempotent repair for databases that were written around the application (the triggers prevent it).
    """
    now = utcnow()
    with transaction(conn, immediate=True):
        return conn.execute(
            "INSERT INTO recipients(name, phone, email, is_enabled, enable_for_all_targets, created_at, updated_at, "
            "user_id) SELECT u.username, NULL, NULL, 0, 0, ?, ?, u.id FROM users u "
            "WHERE NOT EXISTS (SELECT 1 FROM recipients r WHERE r.user_id = u.id)",
            (now, now),
        ).rowcount


def get_recipients_by_user(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    """Get the recipients of all users in one query, keyed by user id."""
    return {
        int(row["user_id"]): row
        for row in conn.execute("SELECT * FROM recipients WHERE user_id IS NOT NULL").fetchall()
    }


def update_user_recipient(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    phone: str | None,
    email: str | None,
    is_enabled: bool | None,
    enable_for_all_targets: bool | None,
) -> int:
    """Set the channel configuration of a user's recipient and return the recipient id.

    A missing recipient (user inserted by raw SQL) is created. Without a channel the recipient is disabled.

    Raises:
        DuplicatePhoneError: another recipient already has this phone number.
        LookupError: the user does not exist.
    """
    with transaction(conn, immediate=True):
        user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise LookupError(f"user {user_id} not found")
        enabled = _recipient_enabled(phone, email, is_enabled)
        recipient = get_user_recipient(conn, user_id)
        if recipient is None:
            return _insert_recipient(
                conn,
                name=user["username"],
                phone=phone or None,
                email=email or None,
                is_enabled=enabled,
                enable_for_all_targets=bool(enable_for_all_targets),
                user_id=user_id,
            )
        recipient_id = int(recipient["id"])
        update_recipient(
            conn,
            recipient_id,
            phone=phone or None,
            email=email or None,
            clear_phone=not phone,
            clear_email=not email,
            is_enabled=enabled,
            enable_for_all_targets=enable_for_all_targets,
        )
        return recipient_id


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


class AdminUserError(ValueError):
    """The user is an administrator, and administrators are never deleted."""


def delete_non_admin_user(conn: sqlite3.Connection, user_id: int) -> bool:
    """Delete a user like ``delete_user``, but refuse an administrator.

    The role is read in the same immediate write transaction as the delete, so a promotion that commits
    before the lock is seen and one that comes later waits for the delete.

    Raises:
        AdminUserError: the user is an administrator; nothing was changed.
    Returns True if the user was deleted, False if not found.
    """
    with transaction(conn, immediate=True):
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is not None and row["is_admin"]:
            raise AdminUserError(user_id)
        return delete_user(conn, user_id)


def delete_user(conn: sqlite3.Connection, user_id: int) -> bool:
    """Delete a user together with its recipient (and thus the recipient's target assignments).

    Tokens and alert assignments are removed via ON DELETE CASCADE. The recipient is deleted explicitly first,
    so it never survives even on a connection without foreign key enforcement.

    Returns True if user was deleted, False if not found.
    """
    with transaction(conn, immediate=True):
        for row in conn.execute("SELECT id FROM recipients WHERE user_id = ?", (user_id,)).fetchall():
            cancel_pending_notifications(conn, int(row["id"]))
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        # The cascade removed the recipient; this also covers a connection without foreign key enforcement.
        conn.execute("DELETE FROM recipients WHERE user_id = ?", (user_id,))
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
    """Update a user's basic fields; the recipient name follows a rename while it still equals the old username."""
    with transaction(conn, immediate=True):
        can_close_value = _can_close_alerts_value(is_admin=is_admin, can_close_alerts=can_close_alerts)
        previous = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.execute(
            "UPDATE users SET username = ?, is_admin = ?, can_close_alerts = ? WHERE id = ?",
            (username.lower(), 1 if is_admin else 0, can_close_value, user_id),
        )
        if previous is not None and previous["username"] != username.lower():
            conn.execute(
                "UPDATE recipients SET name = ?, updated_at = ? WHERE user_id = ? AND name = ?",
                (username.lower(), utcnow(), user_id, previous["username"]),
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
