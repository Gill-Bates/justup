#!/usr/bin/env python3
#
# app/db/sqlite/targets.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitoring targets and their alert-user assignment."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from typing import Any

from ...utils.time import utcnow
from .connection import transaction

# ─── Target Alert Users ─────────────────────────────────────────────────────────


def set_target_alert_users(conn: sqlite3.Connection, target_id: int, user_ids: list[int]) -> None:
    """Set which users get alerts for a target (replaces existing)."""
    with transaction(conn):
        conn.execute("DELETE FROM target_alert_users WHERE target_id = ?", (target_id,))
        for user_id in user_ids:
            conn.execute(
                "INSERT INTO target_alert_users(target_id, user_id) VALUES (?, ?)",
                (target_id, user_id),
            )


def get_target_alert_users(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """Get all users that should receive alerts for a target."""
    return list(
        conn.execute(
            """
            SELECT u.*
            FROM users u
            JOIN target_alert_users tau ON tau.user_id = u.id
            WHERE tau.target_id = ?
            ORDER BY u.username
            """,
            (target_id,),
        ).fetchall()
    )


# Target rows carry the group name from the groups table as "group" (the public API field).
_TARGET_SELECT = 'SELECT t.*, g.name AS "group" FROM targets t LEFT JOIN groups g ON g.id = t.group_id'


def _resolve_group_id(conn: sqlite3.Connection, name: str | None) -> int | None:
    """Return the id of the named group, creating it when unknown (free-text group names stay accepted).

    The insert is conflict-tolerant, so a group created concurrently never raises here.
    """
    if not name:
        return None
    now = utcnow()
    conn.execute(
        "INSERT INTO groups(name, description, created_at, updated_at) VALUES (?, NULL, ?, ?) "
        "ON CONFLICT(name) DO NOTHING",
        (name, now, now),
    )
    return int(conn.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()[0])


def list_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List targets ordered by group/name/id."""
    rows = conn.execute(f"{_TARGET_SELECT} ORDER BY g.name, t.name, t.id").fetchall()
    return list(rows)


def list_targets_paginated(
    conn: sqlite3.Connection, page: int = 1, page_size: int = 50
) -> tuple[list[sqlite3.Row], int]:
    """List targets with pagination. Returns (rows, total_count)."""
    offset = (page - 1) * page_size

    total = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]

    # Get page items - sort groups first, NULL groups (ungrouped) last
    rows = conn.execute(
        f"""
        {_TARGET_SELECT}
        ORDER BY CASE WHEN g.name IS NULL THEN 1 ELSE 0 END, g.name, t.name, t.id
        LIMIT ? OFFSET ?
        """,
        (page_size, offset),
    ).fetchall()

    return list(rows), total


def get_target(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
    """Get a target row by id."""
    return conn.execute(f"{_TARGET_SELECT} WHERE t.id = ?", (target_id,)).fetchone()


class DuplicateTargetError(ValueError):
    """A target with an identical monitoring identity (same checks on the same endpoints) already exists."""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__("An identical monitoring target already exists")
        self.conflicts = conflicts


def _assert_no_exact_duplicate(conn: sqlite3.Connection, data: dict[str, Any], exclude_id: int | None = None) -> None:
    """Raise DuplicateTargetError when ``data`` repeats an existing target's complete check identity.

    Runs inside the writer's transaction, so the DAL stays the single place that enforces the rule
    for the API, importers and direct callers alike.
    """
    from ...services.target_identity import classify_conflicts, identity_signature

    signature = identity_signature(data)
    if signature is None:
        return
    candidates = conn.execute(
        "SELECT * FROM targets WHERE identity_signature = ? OR identity_signature IS NULL", (signature,)
    ).fetchall()
    exact = [c for c in classify_conflicts(data, candidates, exclude_id=exclude_id) if c["classification"] == "exact"]
    if exact:
        raise DuplicateTargetError(exact)


def create_target(conn: sqlite3.Connection, values: dict[str, Any], *, manage_transaction: bool = True) -> int:
    """Create a target and return the new target id.

    Raises:
        DuplicateTargetError: An identical monitoring target exists.
    """
    now = utcnow()
    from ...services.target_identity import identity_signature

    with transaction(conn, immediate=True) if manage_transaction else nullcontext():
        _assert_no_exact_duplicate(conn, values)
        cur = conn.execute(
            """
                INSERT INTO targets(
                    name, group_id, is_enabled,
                    interval_seconds, retention_days,
                    enable_ping, enable_http_check, enable_cert_expiration, enable_tcp_connect,
                    host, ping_host, http_url, http_username, http_password, http_prefer_head, http_basic_auth_enabled,
                    http_success_codes, http_port,
                    cert_host, cert_port, ignore_cert_errors, tcp_host, tcp_ports,
                    max_retries, ping_timeout_minutes,
                    auto_close_alert,
                    sla_enabled, sla_availability_pct, sla_response_time_ms, sla_ping_latency_ms,
                    enable_udp_scan, udp_ports,
                    identity_signature, created_at, updated_at
                ) VALUES(
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?
                )
                """,
            (
                values["name"],
                _resolve_group_id(conn, values.get("group")),
                1 if values.get("is_enabled", True) else 0,
                int(values["interval_seconds"]),
                int(values["retention_days"]),
                1 if values.get("enable_ping", False) else 0,
                1 if values.get("enable_http_check", False) else 0,
                1 if values.get("enable_cert_expiration", False) else 0,
                1 if values.get("enable_tcp_connect", False) else 0,
                values["host"],
                values.get("ping_host"),
                values.get("http_url"),
                values.get("http_username"),
                values.get("http_password"),
                1 if values.get("http_prefer_head", True) else 0,
                1 if values.get("http_basic_auth_enabled", False) else 0,
                values.get("http_success_codes"),
                int(values["http_port"]) if values.get("http_port") else None,
                values.get("cert_host"),
                int(values["cert_port"]) if values.get("cert_port") else None,
                1 if values.get("ignore_cert_errors", False) else 0,
                values.get("tcp_host"),
                str(values.get("tcp_ports") or "80"),
                int(values.get("max_retries", 5)),
                int(values.get("ping_timeout_minutes", 5)),
                1 if values.get("auto_close_alert", True) else 0,
                1 if values.get("sla_enabled", False) else 0,
                values.get("sla_availability_pct"),
                values.get("sla_response_time_ms"),
                values.get("sla_ping_latency_ms"),
                1 if values.get("enable_udp_scan", False) else 0,
                str(values.get("udp_ports") or ""),
                identity_signature(values),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_target(
    conn: sqlite3.Connection, *, target_id: int, patch: dict[str, Any], manage_transaction: bool = True
) -> None:
    """Apply a partial update to a target.

    Raises:
        DuplicateTargetError: The resulting target would repeat another target's identity.
    """
    if not patch:
        return

    _allowed_target_columns = frozenset(
        {
            "name",
            "is_enabled",
            "interval_seconds",
            "retention_days",
            "enable_ping",
            "enable_http_check",
            "enable_cert_expiration",
            "enable_tcp_connect",
            "enable_udp_scan",
            "host",
            "ping_host",
            "http_url",
            "http_username",
            "http_password",
            "http_prefer_head",
            "http_basic_auth_enabled",
            "http_success_codes",
            "http_port",
            "cert_host",
            "cert_port",
            "ignore_cert_errors",
            "tcp_host",
            "tcp_ports",
            "udp_ports",
            "max_retries",
            "auto_close_alert",
            "ping_timeout_minutes",
            "sla_enabled",
            "sla_availability_pct",
            "sla_response_time_ms",
            "sla_ping_latency_ms",
        }
    )

    cols: list[str] = []
    params: list[Any] = []
    for k, v in patch.items():
        if k not in _allowed_target_columns:  # "group" is resolved to group_id below
            continue
        if (
            k
            in {
                "is_enabled",
                "enable_ping",
                "enable_http_check",
                "enable_cert_expiration",
                "enable_tcp_connect",
                "enable_udp_scan",
                "ignore_cert_errors",
                "sla_enabled",
                "http_prefer_head",
                "http_basic_auth_enabled",
                "auto_close_alert",
            }
            and v is not None
        ):
            value = 1 if bool(v) else 0
        else:
            value = v
        cols.append(f'"{k}" = ?')
        params.append(value)
    cols.append("updated_at = ?")
    params.append(utcnow())
    params.append(target_id)
    with transaction(conn, immediate=True) if manage_transaction else nullcontext():
        existing = get_target(conn, target_id)
        if "group" in patch:
            cols.insert(0, "group_id = ?")
            params.insert(0, _resolve_group_id(conn, patch["group"]))
        if existing is not None:
            from ...services.target_identity import identity_signature

            updated = {**dict(existing), **patch}
            _assert_no_exact_duplicate(conn, updated, exclude_id=target_id)
            cols.insert(-1, "identity_signature = ?")
            params.insert(-2, identity_signature(updated))
        conn.execute(f"UPDATE targets SET {', '.join(cols)} WHERE id = ?", params)  # noqa: S608 - code-built column list, values bound as params


def delete_target(conn: sqlite3.Connection, target_id: int) -> None:
    """Delete a target by id."""
    with transaction(conn):
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
