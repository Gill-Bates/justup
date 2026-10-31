#!/usr/bin/env python3
#
# app/db/notifications.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Central notification inbox: persistent events with per-user read state.

A notification is a user-facing record of one business event. It is deliberately separate from
``alerts`` (monitoring state, acknowledge workflow) and ``notification_queue`` (SMTP/Signal
delivery): marking a notification read never touches an alert or a queue row.

Visibility is derived from the row's audience at query time:

* ``admins``: every admin.
* ``user``:   only ``recipient_user_id`` (private events such as PDF results; admins do not see them).
* ``target``: admins and the users assigned to ``target_id`` (``target_alert_users``).

Read state lives in ``notification_reads`` (a missing row means unread). Every writer here runs in
the caller's transaction, so an event is persisted atomically with the state change that caused it,
and a failure to record an event never aborts that state change.
"""

from __future__ import annotations

import functools
import logging
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..services import event_bus
from ..utils.time import utcnow
from .sqlite import run_after_commit, transaction

_log = logging.getLogger(__name__)

CATEGORIES = ("monitoring", "reports", "delivery", "security", "activity")
SEVERITIES = ("info", "success", "warning", "error")
AUDIENCE_ADMINS = "admins"
AUDIENCE_USER = "user"
AUDIENCE_TARGET = "target"

TITLE_MAX = 120
BODY_MAX = 240
PREVIEW_LIMIT = 5
MAX_PAGE_SIZE = 100
# Successful deliveries of one channel are folded into one row per window instead of one per message.
SENT_GROUP_SECONDS = 600

# Retired notices (e.g. a release the installation already runs): kept for idempotency, never listed or counted.
EVENT_RELEASE_SUPERSEDED = "activity.release.superseded"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(value: object, limit: int) -> str:
    """Strip control characters and clamp to *limit*; the UI renders the result as plain text."""
    text = _CONTROL_CHARS.sub(" ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _publish_after_commit(
    conn: sqlite3.Connection,
    notification_id: int,
    category: str,
    audience: str,
    recipient_user_id: int | None,
    target_id: int | None,
) -> None:
    """Tell open SSE streams about the new row, only once the surrounding transaction committed."""
    payload = {"id": notification_id, "category": category}
    try:
        if audience == "user" and recipient_user_id is not None:
            publish_event = functools.partial(
                event_bus.publish, "notification.changed", payload, user_ids=[recipient_user_id]
            )
        elif audience == "target":
            assigned = [
                int(r[0])
                for r in conn.execute("SELECT user_id FROM target_alert_users WHERE target_id = ?", (target_id,))
            ]
            publish_event = functools.partial(
                event_bus.publish, "notification.changed", payload, user_ids=assigned, admins_too=True
            )
        else:
            publish_event = functools.partial(event_bus.publish, "notification.changed", payload, admin_only=True)
        run_after_commit(conn, publish_event)
    except Exception:  # noqa: BLE001 - live updates must never fail the write that raised the notification
        _log.debug("Could not schedule notification event", exc_info=True)


def emit(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    category: str,
    severity: str,
    title: str,
    idempotency_key: str,
    audience: str,
    body: str = "",
    recipient_user_id: int | None = None,
    target_id: int | None = None,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    actor_user_id: int | None = None,
    actor_name: str | None = None,
    created_at: datetime | None = None,
) -> int | None:
    """Persist one event; return its id, or None when it already exists or could not be stored.

    Runs inside the caller's transaction (as a savepoint), so it commits or rolls back with the
    state change that caused it. A storage error is logged and swallowed: losing a notification must
    never fail the monitoring, PDF, login or delivery path that raised it.
    """
    try:
        with transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO notifications (
                    event_type, category, severity, title, body, created_at,
                    resource_type, resource_id, target_id, actor_user_id, actor_name,
                    audience, recipient_user_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    event_type,
                    category,
                    severity,
                    _clean(title, TITLE_MAX),
                    _clean(body, BODY_MAX),
                    created_at or utcnow(),
                    resource_type,
                    None if resource_id is None else str(resource_id),
                    target_id,
                    actor_user_id,
                    _clean(actor_name, 64) if actor_name else None,
                    audience,
                    recipient_user_id,
                    idempotency_key,
                ),
            )
            if not cur.rowcount:
                return None
            new_id = int(cur.lastrowid)
            _publish_after_commit(conn, new_id, category, audience, recipient_user_id, target_id)
            return new_id
    except sqlite3.Error as exc:
        _log.warning("Notification %s could not be stored: %s", event_type, exc)
        return None


def emit_delivery_sent(conn: sqlite3.Connection, channel: str, *, now: datetime | None = None) -> None:
    """Record one successful delivery, grouped per channel and time window to cap the volume."""
    now = now or utcnow()
    key = f"delivery.{channel}.sent:{int(now.timestamp()) // SENT_GROUP_SECONDS}"
    label = "Email" if channel == "email" else "Signal"
    try:
        with transaction(conn):
            # SET expressions read the old row, so event_count + 1 is the new total.
            cur = conn.execute(
                "UPDATE notifications SET event_count = event_count + 1, "
                "title = ? || ' (' || (event_count + 1) || ')' WHERE idempotency_key = ?",
                (f"{label} notifications sent", key),
            )
            if cur.rowcount:
                # The grouped row keeps its id, so clear read state: a further delivery is new
                # activity and must surface as unread again instead of staying silently read.
                row = conn.execute("SELECT id FROM notifications WHERE idempotency_key = ?", (key,)).fetchone()
                conn.execute("DELETE FROM notification_reads WHERE notification_id = ?", (row[0],))
                _publish_after_commit(conn, int(row[0]), "delivery", AUDIENCE_ADMINS, None, None)
                return
        emit(
            conn,
            event_type=f"delivery.{channel}.sent",
            category="delivery",
            severity="success",
            title=f"{label} notifications sent",
            body=f"Channel: {label} · delivered",
            idempotency_key=key,
            audience=AUDIENCE_ADMINS,
            resource_type="queue",
            created_at=now,
        )
    except sqlite3.Error as exc:
        _log.warning("Delivery notification (%s) could not be stored: %s", channel, exc)


# ─── Visibility ──────────────────────────────────────────────────────────────

_VISIBLE = """(
    (n.audience = 'admins' AND :is_admin = 1)
    OR (n.audience = 'user' AND n.recipient_user_id = :uid)
    OR (n.audience = 'target' AND (
        :is_admin = 1
        OR EXISTS (SELECT 1 FROM target_alert_users tau WHERE tau.target_id = n.target_id AND tau.user_id = :uid)
    ))
) AND n.event_type NOT IN ('security.login.success', 'report.requested', 'activity.release.superseded')"""
_UNREAD = "NOT EXISTS (SELECT 1 FROM notification_reads r WHERE r.notification_id = n.id AND r.user_id = :uid)"

_COLUMNS = """
    n.id, n.event_type, n.category, n.severity, n.title, n.body, n.created_at, n.resource_type,
    n.resource_id, n.target_id, n.actor_name, n.event_count, n.audience,
    EXISTS (SELECT 1 FROM notification_reads r WHERE r.notification_id = n.id AND r.user_id = :uid) AS is_read
"""


def _scope(user: Mapping[str, Any]) -> dict[str, Any]:
    return {"uid": int(user["id"]), "is_admin": 1 if user.get("is_admin") else 0}


def _filters(params: dict[str, Any], *, category: str | None, unread_only: bool, severity: str | None) -> str:
    clauses = [_VISIBLE]
    if category in CATEGORIES:
        clauses.append("n.category = :category")
        params["category"] = category
    if severity in SEVERITIES:
        clauses.append("n.severity = :severity")
        params["severity"] = severity
    if unread_only:
        clauses.append(_UNREAD)
    return " AND ".join(clauses)


def _item(row: sqlite3.Row) -> dict[str, Any]:
    created = row["created_at"]
    return {
        "id": int(row["id"]),
        "event_type": row["event_type"],
        "category": row["category"],
        "severity": row["severity"],
        "title": row["title"],
        "body": row["body"],
        "created_at": created.isoformat().replace("+00:00", "Z") if isinstance(created, datetime) else str(created),
        "is_read": bool(row["is_read"]),
        "event_count": int(row["event_count"]),
        "actor": row["actor_name"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "target_id": row["target_id"],
    }


# ─── Queries ─────────────────────────────────────────────────────────────────


def get_summary(conn: sqlite3.Connection, user: Mapping[str, Any], *, preview: int = PREVIEW_LIMIT) -> dict[str, Any]:
    """Unread count, newest unread id, an unread-activity sequence and a short preview.

    ``unread_event_seq`` is the summed ``event_count`` of the visible unread rows. It rises not only
    when a new row appears but also when a regrouped delivery re-surfaces as unread with a higher
    ``event_count`` on its existing id, which ``latest_unread_id`` (``MAX(id)``) cannot detect.
    """
    params = _scope(user)
    where_unread = _filters(params, category=None, unread_only=True, severity=None)
    row = conn.execute(
        f"SELECT COUNT(*) AS unread, MAX(n.id) AS latest, "  # noqa: S608 - code-built clauses, values bound
        f"COALESCE(SUM(n.event_count), 0) AS event_seq FROM notifications n WHERE {where_unread}",
        params,
    ).fetchone()
    params = _scope(user)
    where_all = _filters(params, category=None, unread_only=False, severity=None)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM notifications n WHERE {where_all} ORDER BY n.id DESC LIMIT :lim",  # noqa: S608
        {**params, "lim": max(0, min(int(preview), PREVIEW_LIMIT * 4))},
    ).fetchall()
    return {
        "unread": int(row["unread"]),
        "latest_unread_id": int(row["latest"]) if row["latest"] is not None else 0,
        "unread_event_seq": int(row["event_seq"]),
        "items": [_item(r) for r in rows],
    }


def count_unread(conn: sqlite3.Connection, user: Mapping[str, Any], *, category: str | None = None) -> int:
    params = _scope(user)
    where = _filters(params, category=category, unread_only=True, severity=None)
    return int(conn.execute(f"SELECT COUNT(*) FROM notifications n WHERE {where}", params).fetchone()[0])  # noqa: S608


def list_notifications(
    conn: sqlite3.Connection,
    user: Mapping[str, Any],
    *,
    category: str | None = None,
    unread_only: bool = False,
    severity: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Visible notifications, newest first, with the total for pagination."""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    params = _scope(user)
    where = _filters(params, category=category, unread_only=unread_only, severity=severity)
    total = int(conn.execute(f"SELECT COUNT(*) FROM notifications n WHERE {where}", params).fetchone()[0])  # noqa: S608
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM notifications n WHERE {where} ORDER BY n.id DESC LIMIT :lim OFFSET :off",  # noqa: S608
        {**params, "lim": page_size, "off": (page - 1) * page_size},
    ).fetchall()
    return [_item(r) for r in rows], total


def get_notification(conn: sqlite3.Connection, user: Mapping[str, Any], notification_id: int) -> dict[str, Any] | None:
    """One notification when it is visible to *user*; otherwise None (identical to 'does not exist')."""
    params = {**_scope(user), "nid": int(notification_id)}
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM notifications n WHERE n.id = :nid AND {_VISIBLE}",  # noqa: S608
        params,
    ).fetchone()
    return _item(row) if row else None


# ─── Read state ──────────────────────────────────────────────────────────────


def mark_read(conn: sqlite3.Connection, user: Mapping[str, Any], notification_id: int) -> bool:
    """Mark one visible notification read for *user* only. False when it is not visible to them."""
    params = {**_scope(user), "nid": int(notification_id), "now": utcnow()}
    with transaction(conn):
        cur = conn.execute(
            f"""
            INSERT INTO notification_reads (notification_id, user_id, read_at)
            SELECT n.id, :uid, :now FROM notifications n WHERE n.id = :nid AND {_VISIBLE}
            ON CONFLICT DO NOTHING
            """,  # noqa: S608
            params,
        )
        if cur.rowcount:
            return True
        # Already read still counts as success; an invisible or missing id does not.
        return (
            conn.execute(
                f"SELECT 1 FROM notifications n WHERE n.id = :nid AND {_VISIBLE}",  # noqa: S608
                {k: params[k] for k in ("uid", "is_admin", "nid")},
            ).fetchone()
            is not None
        )


def mark_all_read(conn: sqlite3.Connection, user: Mapping[str, Any], *, category: str | None = None) -> int:
    """Mark every visible unread notification read for *user*; returns how many were changed."""
    params = _scope(user)
    where = _filters(params, category=category, unread_only=True, severity=None)
    params["now"] = utcnow()
    with transaction(conn):
        cur = conn.execute(
            f"""
            INSERT INTO notification_reads (notification_id, user_id, read_at)
            SELECT n.id, :uid, :now FROM notifications n WHERE {where}
            ON CONFLICT DO NOTHING
            """,  # noqa: S608
            params,
        )
        return int(cur.rowcount)


# ─── Event helpers (one place per business event, shared by all writers) ─────


def _admin_label(name: str | None) -> str:
    return _clean(name, 64) or "unknown user"


def notify_alert_opened(
    conn: sqlite3.Connection, *, alert_id: str, target_id: int, target_name: str, failed_service: str
) -> None:
    is_cert = failed_service.upper().startswith("CERT")
    emit(
        conn,
        event_type="monitoring.certificate.warning" if is_cert else "monitoring.alert.opened",
        category="monitoring",
        severity="warning" if is_cert else "error",
        title=f"Certificate warning: {target_name}" if is_cert else f"Alert opened: {target_name}",
        body=f"Check: {failed_service}",
        idempotency_key=f"monitoring.alert.opened:{alert_id}",
        audience=AUDIENCE_TARGET,
        target_id=target_id,
        resource_type="alert",
        resource_id=alert_id,
    )


def notify_alert_resolved(
    conn: sqlite3.Connection, *, alert_id: str, target_id: int, target_name: str, failed_service: str
) -> None:
    emit(
        conn,
        event_type="monitoring.alert.resolved",
        category="monitoring",
        severity="success",
        title=f"Alert resolved: {target_name}",
        body=f"Check: {failed_service}",
        idempotency_key=f"monitoring.alert.resolved:{alert_id}",
        audience=AUDIENCE_TARGET,
        target_id=target_id,
        resource_type="alert",
        resource_id=alert_id,
    )


def notify_report(
    conn: sqlite3.Connection,
    *,
    stage: str,
    job_id: str,
    user_id: int | None,
    target_id: int,
    target_name: str,
    created_at: datetime | None = None,
) -> None:
    """Report result for the requesting user only. Jobs without an owner notify nobody."""
    if user_id is None:
        return
    title, severity, body = {
        "completed": (f"PDF report ready: {target_name}", "success", "Ready to download."),
        "failed": (f"PDF generation failed: {target_name}", "error", "Open the target to try again."),
    }[stage]
    emit(
        conn,
        event_type=f"report.{stage}",
        category="reports",
        severity=severity,
        title=title,
        body=body,
        idempotency_key=f"report.{stage}:{job_id}",
        audience=AUDIENCE_USER,
        recipient_user_id=user_id,
        target_id=target_id,
        resource_type="report_job",
        resource_id=job_id,
        created_at=created_at,
    )


def notify_admin_password_changed(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    username: str,
    event_key: str,
    reset_by: Mapping[str, Any] | None = None,
) -> None:
    """Notify admins when an admin account's password changes or is reset."""
    event_type = "security.admin_password.reset" if reset_by is not None else "security.admin_password.changed"
    if reset_by is None:
        title = "Administrator password changed"
        body = f"{_admin_label(username)} changed their administrator password."
        actor_user_id = user_id
        actor_name = username
    else:
        title = "Administrator password reset"
        resetter = _admin_label(reset_by.get("username"))
        body = f"The administrator password for {_admin_label(username)} was reset by {resetter}."
        # The host CLI has no account: its reset carries no user id.
        actor_user_id = int(reset_by["id"]) if reset_by.get("id") is not None else None
        actor_name = str(reset_by.get("username") or "")

    emit(
        conn,
        event_type=event_type,
        category="security",
        severity="info",
        title=title,
        body=body,
        idempotency_key=f"{event_type}:{user_id}:{event_key}",
        audience=AUDIENCE_ADMINS,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
    )


def notify_user_created(conn: sqlite3.Connection, *, user_id: int, username: str, actor: Mapping[str, Any]) -> None:
    emit(
        conn,
        event_type="activity.user.created",
        category="activity",
        severity="info",
        title=f"User created: {_admin_label(username)}",
        body=f"Created by {_admin_label(actor.get('username'))}",
        idempotency_key=f"activity.user.created:{user_id}",
        audience=AUDIENCE_ADMINS,
        actor_user_id=int(actor["id"]) if actor.get("id") is not None else None,
        actor_name=actor.get("username"),
    )


def notify_target_created(conn: sqlite3.Connection, *, target_id: int, name: str, actor: Mapping[str, Any]) -> None:
    emit(
        conn,
        event_type="activity.target.created",
        category="activity",
        severity="info",
        title=f"Target created: {_clean(name, 80)}",
        body=f"Created by {_admin_label(actor.get('username'))}",
        idempotency_key=f"activity.target.created:{target_id}",
        audience=AUDIENCE_ADMINS,
        actor_user_id=int(actor["id"]) if actor.get("id") is not None else None,
        actor_name=actor.get("username"),
        target_id=target_id,
        resource_type="target",
        resource_id=target_id,
    )


def notify_delivery_failed(conn: sqlite3.Connection, *, queue_id: int, channel: str, target_id: int | None) -> None:
    """Permanent delivery failure. No message content, recipient address or error text is stored."""
    label = "Email" if channel == "email" else "Signal"
    emit(
        conn,
        event_type=f"delivery.{channel}.failed",
        category="delivery",
        severity="error",
        title=f"{label} delivery failed",
        body=f"Channel: {label} · not delivered after all retries; see the spooler log",
        idempotency_key=f"delivery.{channel}.failed:{queue_id}",
        audience=AUDIENCE_ADMINS,
        resource_type="queue",
        resource_id=queue_id,
        target_id=target_id,
    )
