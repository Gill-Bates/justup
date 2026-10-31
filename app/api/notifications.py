#!/usr/bin/env python3
#
# app/api/notifications.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Notification inbox endpoints for the web UI (polling summary, list, read state).

They live under ``/ui/`` on purpose: that prefix already accepts the session cookie and is covered
by the CSRF middleware, so no API route gains cookie authentication. Every response is
``private, no-store``. Visibility and read state are always resolved from the authenticated user,
never from the request: an id the user may not see behaves exactly like one that does not exist.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..db import notifications as inbox
from ..db import sqlite as sqlite_db
from ..services import event_bus, release_check
from ..utils.deps import get_conn
from ..utils.time import utcnow
from .auth import get_current_user

router = APIRouter(prefix="/ui/notifications", tags=["notifications"])

_NO_STORE = "private, no-store"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = _NO_STORE


def _action(conn: sqlite3.Connection, user: dict, item: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve what the user may do with the referenced resource right now (None = nothing to offer).

    A deleted or expired resource yields an explanatory, non-clickable action instead of a dead link.
    """
    rtype, rid = item["resource_type"], item["resource_id"]
    if rtype == "report_job" and rid:
        job = sqlite_db.get_report_job(conn, str(rid))
        owner = job["user_id"] if job else None
        if job is None or owner is None or int(owner) != int(user["id"]):
            return {"type": "unavailable", "label": "Report no longer available"}
        expires_at = job["expires_at"]
        if item.get("event_type") == "report.requested":
            # The request event stays informational even after the job finished.
            return None
        if job["status"] == "completed":
            if expires_at is not None and expires_at <= utcnow():
                return {"type": "unavailable", "label": "Report expired"}
            return {"type": "download", "label": "Download PDF", "url": f"/ui/reports/jobs/{job['job_id']}/download"}
        target_id = item.get("target_id")
        if job["status"] == "failed" and target_id and sqlite_db.get_target(conn, int(target_id)):
            return {"type": "link", "label": "Open target", "url": f"/ui/targets/{int(target_id)}"}
        return None
    if rtype == "target" and rid:
        if sqlite_db.get_target(conn, int(rid)):
            return {"type": "link", "label": "Open target", "url": f"/ui/targets/{int(rid)}"}
        return {"type": "unavailable", "label": "Target no longer exists"}
    if rtype == "alert" and rid:
        # Alerts are managed on the Monitoring tab; the alert row may be gone with its target.
        if sqlite_db.get_alert(conn, str(rid)):
            return {"type": "link", "label": "View alert", "url": "/ui/notifications?category=monitoring"}
        return {"type": "unavailable", "label": "Alert no longer available"}
    if item.get("event_type") == release_check.EVENT_TYPE and user.get("is_admin"):
        # Only a validated release tag maps to the fixed official GitHub release page.
        url = release_check.release_url(rid)
        return {"type": "link", "label": "View release", "url": url} if url else None
    if rtype == "queue" and user.get("is_admin"):
        return {"type": "link", "label": "Delivery settings", "url": "/ui/settings"}
    return None


def decorate_items(conn: sqlite3.Connection, user: dict, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        item["action"] = _action(conn, user, item)
    return items


@router.get("/summary")
def summary(
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
    user: dict = Depends(get_current_user),
):
    """Unread count, newest unread id (change sequence for the bell) and a five-item preview."""
    _no_store(response)
    data = inbox.get_summary(conn, user)
    data["items"] = decorate_items(conn, user, data["items"])
    return data


@router.get("/items")
def list_items(
    response: Response,
    category: str | None = Query(None, pattern="^(monitoring|reports|delivery|security|activity)$"),
    unread: bool = Query(False),
    severity: str | None = Query(None, pattern="^(info|success|warning|error)$"),
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=inbox.MAX_PAGE_SIZE),
    conn: sqlite3.Connection = Depends(get_conn),
    user: dict = Depends(get_current_user),
):
    """Paginated list of visible notifications, newest first."""
    _no_store(response)
    items, total = inbox.list_notifications(
        conn, user, category=category, unread_only=unread, severity=severity, page=page, page_size=page_size
    )
    return {
        "items": decorate_items(conn, user, items),
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "unread": inbox.count_unread(conn, user),
    }


@router.post("/read-all")
def read_all(
    response: Response,
    category: str | None = Query(None, pattern="^(monitoring|reports|delivery|security|activity)$"),
    conn: sqlite3.Connection = Depends(get_conn),
    user: dict = Depends(get_current_user),
):
    """Mark everything visible to the caller as read. Alerts and queue rows are not touched."""
    _no_store(response)
    changed = inbox.mark_all_read(conn, user, category=category)
    if changed:
        event_bus.publish("notification.changed", {}, user_ids=[int(user["id"])])
    return {"updated": changed, "unread": inbox.count_unread(conn, user)}


@router.post("/{notification_id}/read")
def read_one(
    notification_id: int,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
    user: dict = Depends(get_current_user),
):
    """Mark one notification read for the caller only."""
    _no_store(response)
    if notification_id < 1 or not inbox.mark_read(conn, user, notification_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    event_bus.publish("notification.changed", {"id": notification_id}, user_ids=[int(user["id"])])
    return {"id": notification_id, "unread": inbox.count_unread(conn, user)}
