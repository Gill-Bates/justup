#!/usr/bin/env python3
#
# app/db/dashboard_layouts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-user dashboard layout storage (one row per placed widget).

Bounds are enforced by CHECK constraints in the ``dashboard_layouts`` table, a user's rows vanish
with the user (ON DELETE CASCADE) and a replace is one transaction. The active V2 model records
whether each widget height is automatic or manually selected.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .sqlite import transaction

# Vertical bound in rows; mirrors MAX_Y in static/js/dashboard-grid.js and the CHECK in init_schema.
DASHBOARD_MAX_Y = 1000
LAYOUT_VERSION = 2
DASHBOARD_WIDGET_MIN_SIZES = {
    "status-online": (2, 2),
    "status-offline": (2, 2),
    "status-disabled": (2, 2),
    "status-total": (2, 2),
    "quality": (2, 3),
    "downtime": (3, 3),
    "alerts-open": (3, 3),
    "delivery-queue": (3, 3),
    "uptime-day": (3, 2),
    "cert-expiry": (3, 3),
    "slowest-monitors": (4, 3),
    "quality-trend": (4, 3),
    "stale-checks": (3, 3),
}


def _widget_valid(widget: Any) -> bool:
    """Return True when *widget* satisfies the same bounds as the table CHECK constraints."""
    if not isinstance(widget, dict):
        return False
    wid = widget.get("id")
    if not isinstance(wid, str) or not 1 <= len(wid) <= 64:
        return False
    values = [widget.get(key) for key in ("x", "y", "w", "h")]
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return False
    x, y, w, h = values
    min_size = DASHBOARD_WIDGET_MIN_SIZES.get(wid)
    return (
        min_size is not None
        and widget.get("height_mode") in {"auto", "manual"}
        and x >= 0
        and y >= 0
        and min_size[0] <= w <= 12
        and x + w <= 12
        and min_size[1] <= h <= 100
        and y + h <= DASHBOARD_MAX_Y
    )


def get_dashboard_layout(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    """Return the stored layout for a user, or None when the user has none."""
    rows = conn.execute(
        "SELECT widget_id, x, y, w, h, visible, layout_version, height_mode FROM dashboard_layouts "
        "WHERE user_id = ? ORDER BY position",
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    widgets = [
        {
            "id": r["widget_id"],
            "x": r["x"],
            "y": r["y"],
            "w": r["w"],
            "h": r["h"],
            "visible": bool(r["visible"]),
            "height_mode": r["height_mode"],
        }
        for r in rows
    ]
    return {"version": int(rows[0]["layout_version"]), "widgets": widgets}


def set_dashboard_layout(conn: sqlite3.Connection, user_id: int, layout: dict[str, Any]) -> None:
    """Replace the user's layout atomically (delete + insert in one transaction).

    An empty widget list stores nothing, which reads back as "no stored layout" (the client then
    uses its default). Raises ValueError for a layout the table would reject.
    """
    widgets = layout.get("widgets")
    if (
        layout.get("version") != LAYOUT_VERSION
        or not isinstance(widgets, list)
        or not all(_widget_valid(w) for w in widgets)
    ):
        raise ValueError("Invalid dashboard layout")
    if len({w["id"] for w in widgets}) != len(widgets):
        raise ValueError("Duplicate widget id")
    with transaction(conn):
        conn.execute("DELETE FROM dashboard_layouts WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO dashboard_layouts "
            "(user_id, widget_id, position, x, y, w, h, visible, layout_version, height_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    user_id,
                    wd["id"],
                    pos,
                    wd["x"],
                    wd["y"],
                    wd["w"],
                    wd["h"],
                    1 if wd.get("visible", True) else 0,
                    LAYOUT_VERSION,
                    wd["height_mode"],
                )
                for pos, wd in enumerate(widgets)
            ],
        )


def delete_dashboard_layout(conn: sqlite3.Connection, user_id: int) -> bool:
    """Delete the user's layout. Returns True if any row was removed."""
    with transaction(conn):
        return conn.execute("DELETE FROM dashboard_layouts WHERE user_id = ?", (user_id,)).rowcount > 0
