#!/usr/bin/env python3
#
# app/utils/status_helper.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Build target status payloads from the SQLite status cache."""

import json
import sqlite3
from typing import Any

from ..db import sqlite as sqlite_db
from .formatters import format_percent


def get_status_defaults() -> dict[str, Any]:
    """
    Return default status values when no cache exists.

    Returns:
            A dict that matches the status payload schema used by the UI.
    """
    return {
        "ping_status": "unknown",
        "http_status": "unknown",
        "http_response_code": None,
        "tcp_status": "unknown",
        "tcp_port_status": {},
        "cert_status": "unknown",
        "cert_days_left": None,
        "uptime_percent": None,
        "uptime_percent_display": "–",
        "uptime_percent_30d": None,
        "uptime_percent_30d_display": "–",
        "last_downtime": None,
        "last_check": None,
    }


def _has_col(row: Any, key: str) -> bool:
    """True if a sqlite3.Row (or mapping) has the column; ``in`` on a Row would test values."""
    return key in row.keys()  # noqa: SIM118 - sqlite3.Row.__contains__ tests values, not column names


def _col(row: Any, key: str) -> Any:
    """Column value or None when the column is absent."""
    return row[key] if _has_col(row, key) else None


def build_target_status(row: sqlite3.Row, cached: sqlite3.Row | None) -> dict[str, Any]:
    """
    Build a status dict for a target from the SQLite cache.

    All status values come from the cache; this helper does not read TSDB.

    Args:
            row: Target row from SQLite.
            cached: Cached status row from SQLite, if present.

    Returns:
            A status dict suitable for templates and API responses.
    """
    uptime_percent = None
    if cached and _has_col(cached, "uptime_percent") and cached["uptime_percent"] is not None:
        uptime_percent = cached["uptime_percent"]

    uptime_percent_30d = None
    if cached and _has_col(cached, "uptime_percent_30d") and cached["uptime_percent_30d"] is not None:
        uptime_percent_30d = cached["uptime_percent_30d"]

    result = get_status_defaults()
    result |= {
        "uptime_percent": uptime_percent,
        "uptime_percent_display": format_percent(uptime_percent),
        "uptime_percent_30d": uptime_percent_30d,
        "uptime_percent_30d_display": format_percent(uptime_percent_30d),
        "last_downtime": cached["last_down_at"] if cached and _has_col(cached, "last_down_at") else None,
        "last_check": cached["updated_at"] if cached and _has_col(cached, "updated_at") else None,
    }

    if not bool(row["is_enabled"]):
        return result

    if not cached:
        return result

    if cached["overall_status"] == "unknown":
        return result

    if bool(row["enable_ping"]) and cached["ping_up"] is not None:
        result["ping_status"] = "up" if cached["ping_up"] == 1 else "down"

    if bool(row["enable_http_check"]) and cached["http_up"] is not None:
        result["http_status"] = "up" if cached["http_up"] == 1 else "down"
        if cached["http_status"] is not None:
            result["http_response_code"] = cached["http_status"]

    if bool(row["enable_tcp_connect"]) and cached["tcp_up"] is not None:
        result["tcp_status"] = "up" if cached["tcp_up"] == 1 else "down"
        # Parse per-port status JSON if available
        if _col(cached, "tcp_port_status"):
            try:
                result["tcp_port_status"] = json.loads(cached["tcp_port_status"])
            except (json.JSONDecodeError, TypeError):
                result["tcp_port_status"] = {}

    if bool(row["enable_cert_expiration"]) and cached["cert_days_left"] is not None:
        days = cached["cert_days_left"]
        result["cert_days_left"] = days
        if days < 0:
            result["cert_status"] = "expired"
        elif days <= 3:
            result["cert_status"] = "critical"
        elif days <= 7:
            result["cert_status"] = "warning"
        else:
            result["cert_status"] = "ok"

    return result


def get_simple_status(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    """
    Get a target's overall status from the SQLite cache.

    Args:
            row: Target row from SQLite.
            conn: SQLite connection.

    Returns:
            One of: up, down, unknown, disabled.
    """
    if not bool(row["is_enabled"]):
        return "disabled"

    target_id = int(row["id"])
    cached = sqlite_db.get_target_status(conn, target_id)

    if cached and cached["overall_status"]:
        return cached["overall_status"]

    return "unknown"
