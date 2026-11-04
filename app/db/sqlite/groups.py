#!/usr/bin/env python3
#
# app/db/sqlite/groups.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Target groups."""

from __future__ import annotations

import sqlite3
from typing import Any

from ...utils.time import utcnow
from .connection import transaction

# ─────────────────────────────────────────────────────────────────────────────
# Groups CRUD
# ─────────────────────────────────────────────────────────────────────────────


def list_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all groups."""
    return conn.execute("SELECT * FROM groups ORDER BY name").fetchall()


def list_groups_with_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List all groups with their target count (N+1 query optimization)."""
    sql = """
        SELECT g.*, COUNT(t.id) as target_count
        FROM groups g
        LEFT JOIN targets t ON t.group_id = g.id
        GROUP BY g.id
        ORDER BY g.name
    """
    return conn.execute(sql).fetchall()


def get_group(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    """Get a group by id."""
    return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def get_group_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Get a group by name."""
    return conn.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()


def create_group(conn: sqlite3.Connection, *, name: str, description: str | None = None) -> int:
    """Create a group and return the new group id.

    Raises:
        sqlite3.IntegrityError: The name is taken (UNIQUE constraint; decides concurrent creates).
    """
    now = utcnow()
    with transaction(conn, immediate=True):
        cur = conn.execute(
            "INSERT INTO groups(name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, description, now, now),
        )
        return int(cur.lastrowid)


def update_group(conn: sqlite3.Connection, *, group_id: int, patch: dict[str, Any]) -> bool:
    """Apply a partial update to a group; False when the group does not exist.

    Raises:
        sqlite3.IntegrityError: The new name is taken (UNIQUE constraint).
    """
    if not patch:
        return get_group(conn, group_id) is not None
    allowed = {"name", "description"}
    cols: list[str] = []
    params: list[Any] = []
    for k, v in patch.items():
        if k not in allowed:
            continue
        cols.append(f"{k} = ?")
        params.append(v)
    cols.append("updated_at = ?")
    params.append(utcnow())
    params.append(group_id)
    with transaction(conn, immediate=True):
        # Targets reference the group by id, so a rename needs no change on targets.
        cur = conn.execute(f"UPDATE groups SET {', '.join(cols)} WHERE id = ?", params)  # noqa: S608 - code-built column list, values bound as params
        return cur.rowcount > 0


class GroupNotFoundError(LookupError):
    """The group does not exist (anymore)."""


class GroupNotEmptyError(ValueError):
    """The group still has targets assigned."""

    def __init__(self, count: int) -> None:
        super().__init__(f"{count} target(s) still use this group")
        self.count = count


def delete_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Delete a group by id; its targets become ungrouped (ON DELETE SET NULL)."""
    with transaction(conn, immediate=True):
        conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))


def delete_group_if_empty(conn: sqlite3.Connection, group_id: int) -> str:
    """Delete a group only while no target uses it; return the deleted group's name.

    The emptiness check and the delete share one write transaction, so a target cannot be
    assigned in between.

    Raises:
        GroupNotFoundError: Unknown group id.
        GroupNotEmptyError: Targets are still assigned.
    """
    with transaction(conn, immediate=True):
        row = conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            raise GroupNotFoundError(group_id)
        count = conn.execute("SELECT COUNT(*) FROM targets WHERE group_id = ?", (group_id,)).fetchone()[0]
        if count:
            raise GroupNotEmptyError(int(count))
        conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return str(row["name"])


def count_targets_in_group(conn: sqlite3.Connection, group_name: str) -> int:
    """Count how many targets are assigned to the given group name."""
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM targets t JOIN groups g ON g.id = t.group_id WHERE g.name = ?", (group_name,)
    ).fetchone()
    return int(row["cnt"]) if row else 0
