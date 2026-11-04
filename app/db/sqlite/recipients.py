#!/usr/bin/env python3
#
# app/db/sqlite/recipients.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Notification recipients and their target assignments."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from typing import Any

from ...utils.time import utcnow
from .connection import transaction

# ─── Recipients ─────────────────────────────────────────────────────────────


def list_recipients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all alert recipients."""
    return list(conn.execute("SELECT * FROM recipients ORDER BY name").fetchall())


def get_recipient(conn: sqlite3.Connection, recipient_id: int) -> sqlite3.Row | None:
    """Get a single recipient by ID."""
    return conn.execute("SELECT * FROM recipients WHERE id = ?", (recipient_id,)).fetchone()


def get_recipient_by_phone(conn: sqlite3.Connection, phone: str) -> sqlite3.Row | None:
    """Get a recipient by phone number."""
    return conn.execute("SELECT * FROM recipients WHERE phone = ?", (phone,)).fetchone()


class DuplicatePhoneError(ValueError):
    """The phone number already belongs to another recipient."""


def _assert_phone_free(conn: sqlite3.Connection, phone: str, *, exclude_id: int | None = None) -> None:
    """Raise DuplicatePhoneError if another recipient owns ``phone``.

    Must run inside the same write transaction as the INSERT/UPDATE. It keeps the rule atomic even
    on a legacy database whose non-unique phone index could not be upgraded because of duplicates.
    """
    row = conn.execute(
        "SELECT 1 FROM recipients WHERE phone = ? AND id IS NOT ? LIMIT 1", (phone, exclude_id)
    ).fetchone()
    if row is not None:
        raise DuplicatePhoneError(phone)


def assign_flagged_recipients(conn: sqlite3.Connection, target_id: int) -> None:
    """Assign every recipient with ``enable_for_all_targets`` to one target (idempotent).

    Runs inside the caller's transaction so a new target and its automatic assignments commit together.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO target_recipients(target_id, recipient_id)
        SELECT ?, id FROM recipients WHERE enable_for_all_targets = 1
        """,
        (target_id,),
    )


def create_recipient(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str | None = None,
    email: str | None = None,
    is_enabled: bool = True,
    enable_for_all_targets: bool = False,
) -> int:
    """Create a new recipient.

    Raises:
        DuplicatePhoneError: another recipient already has this phone number.
    """
    now = utcnow()
    with transaction(conn, immediate=True):
        if phone:
            _assert_phone_free(conn, phone)
        cur = conn.execute(
            """
            INSERT INTO recipients(name, phone, email, is_enabled, enable_for_all_targets, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, phone, email, 1 if is_enabled else 0, 1 if enable_for_all_targets else 0, now, now),
        )
        if enable_for_all_targets:
            _assign_to_all_targets(conn, cur.lastrowid)
        return cur.lastrowid


def _assign_to_all_targets(conn: sqlite3.Connection, recipient_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO target_recipients(target_id, recipient_id) SELECT id, ? FROM targets",
        (recipient_id,),
    )


def update_recipient(
    conn: sqlite3.Connection,
    recipient_id: int,
    *,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    is_enabled: bool | None = None,
    enable_for_all_targets: bool | None = None,
    clear_phone: bool = False,
    clear_email: bool = False,
) -> None:
    """Update a recipient.

    Args:
            recipient_id: ID of recipient to update
            name: New name (optional)
            phone: New phone number (optional)
            email: New email address (optional)
            is_enabled: New enabled status (optional)
            enable_for_all_targets: New flag (optional). Switching it on assigns the recipient to every
                target; switching it off (from on) removes it from every target, in the same transaction.
            clear_phone: If True, set phone to NULL
            clear_email: If True, set email to NULL
    """
    updates = ["updated_at = ?"]
    params: list[Any] = [utcnow()]

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if clear_phone:
        updates.append("phone = NULL")
    elif phone is not None:
        updates.append("phone = ?")
        params.append(phone)
    if clear_email:
        updates.append("email = NULL")
    elif email is not None:
        updates.append("email = ?")
        params.append(email)
    if is_enabled is not None:
        updates.append("is_enabled = ?")
        params.append(1 if is_enabled else 0)
    if enable_for_all_targets is not None:
        updates.append("enable_for_all_targets = ?")
        params.append(1 if enable_for_all_targets else 0)

    params.append(recipient_id)
    with transaction(conn, immediate=True):
        previous = conn.execute(
            "SELECT enable_for_all_targets FROM recipients WHERE id = ?", (recipient_id,)
        ).fetchone()
        if phone:
            current = conn.execute("SELECT phone FROM recipients WHERE id = ?", (recipient_id,)).fetchone()
            # An unchanged number stays editable even while legacy duplicates of it exist.
            if current is None or current["phone"] != phone:
                _assert_phone_free(conn, phone, exclude_id=recipient_id)
        conn.execute(f"UPDATE recipients SET {', '.join(updates)} WHERE id = ?", params)  # noqa: S608 - code-built column list, values bound as params
        if enable_for_all_targets and previous is not None:
            _assign_to_all_targets(conn, recipient_id)
        elif enable_for_all_targets is False and previous is not None and previous["enable_for_all_targets"]:
            conn.execute("DELETE FROM target_recipients WHERE recipient_id = ?", (recipient_id,))


def delete_recipient(conn: sqlite3.Connection, recipient_id: int) -> None:
    """Delete a recipient."""
    with transaction(conn):
        conn.execute("DELETE FROM recipients WHERE id = ?", (recipient_id,))


# ─── Target Recipients (Many-to-Many) ───────────────────────────────────────


def get_target_recipients(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """Get all recipients assigned to a target."""
    return list(
        conn.execute(
            """
        SELECT r.* FROM recipients r
        JOIN target_recipients tr ON r.id = tr.recipient_id
        WHERE tr.target_id = ?
        ORDER BY r.name
        """,
            (target_id,),
        ).fetchall()
    )


def get_target_recipient_ids(conn: sqlite3.Connection, target_id: int) -> list[int]:
    """Get recipient IDs assigned to a target."""
    rows = conn.execute(
        "SELECT recipient_id FROM target_recipients WHERE target_id = ?",
        (target_id,),
    ).fetchall()
    return [row["recipient_id"] for row in rows]


def set_target_recipients(
    conn: sqlite3.Connection, target_id: int, recipient_ids: list[int], *, manage_transaction: bool = True
) -> None:
    """Set the recipients for a target (replaces existing assignments)."""
    with transaction(conn) if manage_transaction else nullcontext():
        conn.execute("DELETE FROM target_recipients WHERE target_id = ?", (target_id,))
        for rid in recipient_ids:
            conn.execute(
                "INSERT INTO target_recipients(target_id, recipient_id) VALUES (?, ?)",
                (target_id, rid),
            )


def get_targets_by_recipient(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """Get the assigned targets of all recipients in one query, grouped by recipient id."""
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT tr.recipient_id AS recipient_id, t.id, t.name
        FROM targets t
        JOIN target_recipients tr ON t.id = tr.target_id
        ORDER BY t.name
        """
    ).fetchall():
        grouped.setdefault(row["recipient_id"], []).append(row)
    return grouped


def get_recipient_targets(conn: sqlite3.Connection, recipient_id: int) -> list[sqlite3.Row]:
    """Get all targets assigned to a recipient."""
    return list(
        conn.execute(
            """
        SELECT t.id, t.name
        FROM targets t
        JOIN target_recipients tr ON t.id = tr.target_id
        WHERE tr.recipient_id = ?
        ORDER BY t.name
        """,
            (recipient_id,),
        ).fetchall()
    )
