#!/usr/bin/env python3
#
# app/api/targets.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Target management API routes."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from ..db import notifications
from ..db import sqlite as sqlite_db
from ..db import tsdb as tsdb_db
from ..models.targets import (
    DEFAULT_TCP_PORTS,
    TargetCreate,
    TargetPublic,
    TargetUpdate,
    TargetWizardPreview,
    target_row_to_dict,
)
from ..services import event_bus
from ..services.target_identity import classify_conflicts, normalize_host
from ..services.target_profiles import PROFILE_LABELS, build_profile
from ..utils import maintenance
from ..utils.client_ip import audit_client_ip
from ..utils.crypto import encrypt_secret
from ..utils.deps import get_conn, get_tsdb_dir
from ..utils.http_status import DEFAULT_HTTP_SUCCESS_CODES
from ..utils.rate_limit import limiter
from .auth import get_current_user, require_admin

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/targets", tags=["targets"])


_get_client_ip = audit_client_ip


def _validate_http_url(url) -> str | None:
    """
    Validate and normalize HTTP URL for target configuration.

    Only http:// and https:// schemes are allowed.
    Rejects file://, ftp://, javascript:, etc.

    NOTE: Pydantic's HttpUrl already validates URL syntax and structure.
    This function enforces application-level POLICY (allowed schemes)
    and provides user-friendly error messages. Both layers are intentional:
    - Pydantic: parsing & structure validation
    - API: security policy enforcement
    """
    if url is None:
        return None
    url_str = str(url).strip()
    if not url_str:
        return None

    parsed = urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=422, detail=f"http_url must use http:// or https:// scheme, got '{parsed.scheme}://'"
        )
    if not parsed.netloc:
        raise HTTPException(status_code=422, detail="http_url must include a valid hostname")
    if "@" in parsed.netloc:
        # Credentials belong in the dedicated encrypted auth fields; never echo the URL back.
        raise HTTPException(
            status_code=422, detail="http_url must not contain credentials (user:password@); use the auth fields"
        )
    return url_str


def _get_username(user) -> str:
    """Safely extract username from user object for logging."""
    if hasattr(user, "username"):
        return user.username
    if isinstance(user, dict):
        return user.get("username", "unknown")
    return str(user)


def _norm_host(v: str | None) -> str | None:
    """Normalize hostname (lowercase, strip whitespace and trailing dot)."""
    return normalize_host(v)


def _validate_tcp_ports(ports: str | None) -> str:
    """
    Validate and normalize TCP ports string.

    This is the authoritative port validation - Model validators are
    intentionally minimal (type coercion only). API enforces:
    - Valid port numbers (1-65535)
    - Unique ports (no duplicates)
    - Sorted output for consistency

    Args:
        ports: Comma-separated port string (e.g., "80,443,8080")

    Returns:
        Normalized, validated port string (sorted, unique)

    Raises:
        HTTPException: If any port is invalid
    """
    if not ports or not ports.strip():
        return DEFAULT_TCP_PORTS

    parts = re.split(r"[,\s]+", ports.strip())
    validated = set()

    for raw_p in parts:
        p = raw_p.strip()
        if not p:
            continue
        if not (p.isascii() and p.isdigit()):
            raise HTTPException(status_code=422, detail=f"Invalid TCP port: '{p}' (must be a number)")
        port = int(p)
        if not 1 <= port <= 65535:
            raise HTTPException(status_code=422, detail=f"Invalid TCP port: {port} out of range (1-65535)")
        validated.add(port)

    if not validated:
        return DEFAULT_TCP_PORTS

    return ",".join(str(p) for p in sorted(validated))


def _encrypt_password(password: str | object | None) -> str | None:
    """Extract plain password from SecretStr and encrypt for storage."""
    if password is None:
        return None
    # Handle SecretStr from Pydantic
    if hasattr(password, "get_secret_value"):
        plain = password.get_secret_value()
    else:
        plain = str(password)
    if not plain:
        return None
    return encrypt_secret(plain)


def _check_host_collision(conn, data: dict, exclude_id: int | None = None) -> None:
    """Block exact monitoring duplicates while allowing different services on one host."""
    exact = [
        c
        for c in classify_conflicts(data, sqlite_db.list_targets(conn), exclude_id=exclude_id)
        if c["classification"] == "exact"
    ]
    if exact:
        raise HTTPException(
            status_code=409,
            detail={"message": "An identical monitoring target already exists", "conflicts": exact},
        )


def _validate_target_consistency(data: dict) -> None:
    """
    Validate field dependencies for target configuration.

    This is the AUTHORITATIVE validation layer. Model validators are
    intentionally permissive (formal type/range checks only).

    API rules are STRICTER than Model rules:
    - Model allows enable_http_check with host fallback → API requires http_url
    - Model allows enable_cert_expiration standalone → API requires HTTP+HTTPS
    - Model validates fields in isolation → API validates combinations

    This separation allows:
    - Models to accept flexible input formats
    - API to enforce business rules after normalization
    - Clear error messages with full context
    """
    errors: list[str] = []

    # Primary host is always required
    if not data.get("host"):
        errors.append("host is required")

    if data.get("is_enabled") and not any(
        data.get(field) for field in ("enable_ping", "enable_http_check", "enable_tcp_connect")
    ):
        errors.append("An enabled target requires at least one monitoring check")

    # HTTP check requires http_url
    if data.get("enable_http_check") and not data.get("http_url"):
        errors.append("http_url is required when enable_http_check is true")

    # HTTP credentials require http_url
    if (data.get("http_username") or data.get("http_password")) and not data.get("http_url"):
        errors.append("http_url is required when setting HTTP credentials")

    # TCP connect requires tcp_host
    if data.get("enable_tcp_connect") and not data.get("tcp_host"):
        errors.append("tcp_host is required when enable_tcp_connect is true")

    # Ping requires host or ping_host
    if data.get("enable_ping") and not data.get("host") and not data.get("ping_host"):
        errors.append("Host or ping_host is required when enable_ping is true")

    # Cert expiration requires HTTP check to be enabled (cert is checked via TLS handshake during HTTP)
    if data.get("enable_cert_expiration") and not data.get("enable_http_check"):
        errors.append("HTTP check must be enabled when certificate expiration monitoring is enabled")

    # Cert expiration requires http_url (cert_host is derived from it)
    if data.get("enable_cert_expiration") and not data.get("http_url"):
        errors.append("http_url is required when certificate expiration monitoring is enabled")
    if (
        data.get("enable_cert_expiration")
        and data.get("http_url")
        and urlparse(str(data["http_url"])).scheme != "https"
    ):
        errors.append("Certificate expiration monitoring requires an HTTPS URL")

    # Basic Auth requires username if password is present
    if data.get("http_password") and not data.get("http_username"):
        errors.append("http_username is required when http_password is set")

    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))


def _row_to_target_public(row) -> TargetPublic:
    """Convert DB row to the public response model (no secrets exposed)."""
    return TargetPublic(**target_row_to_dict(row))


def _prepare_create_values(payload: TargetCreate) -> dict:
    values = payload.model_dump()
    http_url = _validate_http_url(values.get("http_url"))
    enable_http_check = values.get("enable_http_check", False)
    if "enable_cert_expiration" in payload.model_fields_set:
        enable_cert_expiration = values["enable_cert_expiration"]
    else:
        enable_cert_expiration = bool(enable_http_check and http_url and urlparse(http_url).scheme == "https")
    values_db = {
        "name": values["name"],
        "group_name": values.get("group"),
        "is_enabled": values.get("is_enabled", True),
        "interval_seconds": values["interval_seconds"],
        "retention_days": values["retention_days"],
        "enable_ping": values.get("enable_ping", False),
        "enable_http_check": enable_http_check,
        "http_prefer_head": values.get("http_prefer_head", True),
        "http_basic_auth_enabled": values.get("http_basic_auth_enabled", False),
        "http_port": values.get("http_port"),
        "http_success_codes": values.get("http_success_codes") or DEFAULT_HTTP_SUCCESS_CODES,
        "enable_cert_expiration": enable_cert_expiration,
        "enable_tcp_connect": values.get("enable_tcp_connect", False),
        "ping_host": _norm_host(values.get("ping_host")),
        "http_url": http_url,
        "http_username": values.get("http_username"),
        "http_password": _encrypt_password(values.get("http_password")),
        "cert_host": _norm_host(values.get("cert_host")),
        "cert_port": values.get("cert_port"),
        "ignore_cert_errors": values.get("ignore_cert_errors", False),
        "tcp_host": _norm_host(values.get("tcp_host")),
        "tcp_ports": _validate_tcp_ports(values.get("tcp_ports")),
        "host": _norm_host(values["host"]),
        "max_retries": values.get("max_retries", 5),
        "auto_close_alert": values.get("auto_close_alert", True),
        "sla_enabled": values.get("sla_enabled", False),
        "sla_availability_pct": values.get("sla_availability_pct"),
        "sla_response_time_ms": values.get("sla_response_time_ms"),
        "sla_ping_latency_ms": values.get("sla_ping_latency_ms"),
    }
    _validate_target_consistency(values_db)
    return values_db


@router.get("/profiles")
def list_profiles(_user=Depends(get_current_user)):
    """List available deterministic wizard profiles."""
    return [{"id": key, "label": label} for key, label in PROFILE_LABELS.items()]


@router.post("/preview")
def preview(payload: TargetWizardPreview, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Preview a profile and existing monitors without persisting or probing the network."""
    try:
        target, suggested_https = build_profile(
            payload.profile,
            payload.address,
            name=payload.name,
            group=payload.group,
            tcp_ports=payload.tcp_ports,
            overrides=payload.overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    values = _prepare_create_values(target)
    conflicts = classify_conflicts(values, sqlite_db.list_targets(conn))
    public_values = target.model_dump(
        mode="json", exclude={"http_password", "clear_http_password", "alert_recipient_ids"}
    )
    public_values.update(
        host=values["host"],
        ping_host=values["ping_host"],
        tcp_host=values["tcp_host"],
        cert_host=values["cert_host"],
        http_url=values["http_url"],
        tcp_ports=values["tcp_ports"],
        enable_cert_expiration=values["enable_cert_expiration"],
    )
    return {"values": public_values, "conflicts": conflicts, "suggested_https": suggested_https}


@router.get("", response_model=list[TargetPublic])
def list_all(conn=Depends(get_conn), _user=Depends(get_current_user)):
    """List all configured targets."""
    return [_row_to_target_public(r) for r in sqlite_db.list_targets(conn)]


@router.get("/{target_id}", response_model=TargetPublic)
def get_one(target_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
    """Get one target by id."""
    row = sqlite_db.get_target(conn, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    return _row_to_target_public(row)


@router.post("", response_model=TargetPublic, status_code=status.HTTP_201_CREATED)
def create(payload: TargetCreate, request: Request, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Create a new target."""
    values_db = _prepare_create_values(payload)
    recipient_ids = payload.alert_recipient_ids
    with sqlite_db.immediate_transaction(conn):
        for rid in set(recipient_ids):
            if sqlite_db.get_recipient(conn, rid) is None:
                raise HTTPException(status_code=422, detail=f"Recipient {rid} not found")
        _check_host_collision(conn, values_db)
        new_id = sqlite_db.create_target(conn, values_db, manage_transaction=False)
        if recipient_ids:
            sqlite_db.set_target_recipients(conn, new_id, list(dict.fromkeys(recipient_ids)), manage_transaction=False)
        notifications.notify_target_created(conn, target_id=new_id, name=values_db["name"], actor=_user)
    event_bus.publish("target.changed", {"target_id": new_id})

    client_ip = _get_client_ip(request)
    _log.info(
        "Target created: %s (id=%d)",
        values_db["name"],
        new_id,
        extra={
            "event": "target_created",
            "user": _get_username(_user),
            "target_id": new_id,
            "target_name": values_db["name"],
            "client_ip": client_ip,
        },
    )

    # Best-effort: let the scheduler measure the new target right away
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.request_refresh()

    row = sqlite_db.get_target(conn, new_id)
    return _row_to_target_public(row)


@router.patch("/{target_id}", response_model=TargetPublic)
def patch(target_id: int, payload: TargetUpdate, conn=Depends(get_conn), _user=Depends(require_admin)):
    with sqlite_db.immediate_transaction(conn):
        result = _patch_locked(target_id, payload, conn)
    event_bus.publish("target.changed", {"target_id": target_id})
    return result


def _patch_locked(target_id: int, payload: TargetUpdate, conn):
    """
    Update a target by id (partial update).

    Patch flow:
    1. Extract only fields that were explicitly set in the request
    2. Normalize hostname fields before persistence
    3. Merge patch with existing values to create "effective" target
    4. Validate merged state (semantic consistency check)
    5. Check for exact monitoring duplicates against other targets
    6. Persist only the changed fields

    The merge step is necessary because validation rules depend on
    field combinations (e.g., enable_http_check requires http_url).
    """
    existing = sqlite_db.get_target(conn, target_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Target not found")

    # Step 1: Extract explicitly set fields from payload
    patch_payload = payload.model_dump(exclude_unset=True)
    patch_db: dict = {}

    if "name" in patch_payload:
        patch_db["name"] = patch_payload["name"]
    if "group" in patch_payload:
        patch_db["group_name"] = patch_payload["group"]

    # Direct field mappings (no transformation needed)
    for k in (
        "is_enabled",
        "interval_seconds",
        "retention_days",
        "enable_ping",
        "enable_http_check",
        "enable_cert_expiration",
        "http_prefer_head",
        "http_basic_auth_enabled",
        "http_port",
        "http_success_codes",
        "enable_tcp_connect",
        "cert_port",
        "ignore_cert_errors",
        "max_retries",
        "auto_close_alert",
        "sla_enabled",
        "sla_availability_pct",
        "sla_response_time_ms",
        "sla_ping_latency_ms",
    ):
        if k in patch_payload:
            patch_db[k] = patch_payload[k]

    # Validate and normalize tcp_ports if provided
    if "tcp_ports" in patch_payload:
        patch_db["tcp_ports"] = _validate_tcp_ports(patch_payload["tcp_ports"])

    # Step 2: Normalize hostname fields before persistence
    if "host" in patch_payload:
        patch_db["host"] = _norm_host(patch_payload["host"])
    for k in ("ping_host", "cert_host", "tcp_host"):
        if k in patch_payload:
            patch_db[k] = _norm_host(patch_payload[k])
    if "http_username" in patch_payload:
        patch_db["http_username"] = patch_payload["http_username"]
    if "http_url" in patch_payload:
        patch_db["http_url"] = _validate_http_url(patch_payload["http_url"])

    if patch_payload.get("clear_http_password"):
        patch_db["http_password"] = None
        patch_db["http_username"] = None
        patch_db["http_basic_auth_enabled"] = False
    elif patch_payload.get("http_password"):
        patch_db["http_password"] = _encrypt_password(patch_payload["http_password"])

    # Step 3: Build complete effective target state for validation
    # Merges patch with existing values. Required because:
    # - Validation rules check field combinations
    # - Duplicate detection needs all enabled checks, not just patched ones
    # - Certificate validation needs the effective URL and check flags
    def _merge(key: str):
        """Get effective value: patched if set, else existing."""
        if key in patch_db:
            return patch_db[key]
        return existing[key]

    # Build complete effective state (not just patched fields)
    effective = {
        "name": _merge("name"),
        "host": _merge("host"),
        "is_enabled": _merge("is_enabled"),
        "enable_ping": patch_db.get("enable_ping", existing["enable_ping"]),
        "enable_http_check": patch_db.get(
            "enable_http_check",
            bool(existing["enable_http_check"]),
        ),
        "enable_tcp_connect": patch_db.get("enable_tcp_connect", existing["enable_tcp_connect"]),
        "ping_host": _merge("ping_host"),
        "http_url": _merge("http_url"),
        "http_username": _merge("http_username"),
        "http_password": _merge("http_password"),
        "cert_host": _merge("cert_host"),
        "tcp_host": _merge("tcp_host"),
    }

    current_cert_exp = bool(existing["enable_cert_expiration"])
    desired_cert_expiration = patch_db.get("enable_cert_expiration", current_cert_exp)
    if (
        "enable_cert_expiration" not in patch_db
        and ("http_url" in patch_db or "enable_http_check" in patch_db)
        and (
            not effective["enable_http_check"]
            or not effective["http_url"]
            or urlparse(str(effective["http_url"])).scheme != "https"
        )
    ):
        desired_cert_expiration = False
    effective["enable_cert_expiration"] = desired_cert_expiration
    if current_cert_exp != desired_cert_expiration:
        patch_db["enable_cert_expiration"] = desired_cert_expiration

    # Step 4: Validate effective state (semantic consistency)
    _validate_target_consistency(effective)

    # Step 5: Check for host collisions using COMPLETE effective state
    # This ensures collision detection sees all hosts, not just patched ones
    _check_host_collision(conn, effective, exclude_id=target_id)

    # Step 6: Persist only the changed fields
    if patch_db:
        sqlite_db.update_target(conn, target_id=target_id, patch=patch_db, manage_transaction=False)
    row = sqlite_db.get_target(conn, target_id)
    return _row_to_target_public(row)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(target_id: int, request: Request, conn=Depends(get_conn), user=Depends(require_admin)):
    """Delete a target and its cached status and metrics."""
    target = sqlite_db.get_target(conn, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    client_ip = _get_client_ip(request)
    target_name = target["name"]
    _log.info(
        "Target deleted: %s (id=%d)",
        target_name,
        target_id,
        extra={
            "event": "target_deleted",
            "user": _get_username(user),
            "target_id": target_id,
            "target_name": target_name,
            "client_ip": client_ip,
        },
    )

    sqlite_db.delete_target(conn, target_id)
    try:
        tsdb_db.delete_target(get_tsdb_dir(request), target_id)
    except Exception as e:  # noqa: BLE001 - best-effort/background boundary: log and continue so the request or task is not aborted
        _log.warning("Failed to delete TSDB data for target %d: %s", target_id, e)
    # Best-effort: notify scheduler
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.forget_target(target_id)
    else:
        _log.debug("Scheduler not available, target %d will be forgotten on next restart", target_id)
    event_bus.publish("target.changed", {"target_id": target_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{target_id}/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_metrics(target_id: int, request: Request, conn=Depends(get_conn), user=Depends(require_admin)):
    """Reset all metrics for a target. Deletes TSDB data and cached status, keeps the target itself."""
    target = sqlite_db.get_target(conn, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    client_ip = _get_client_ip(request)
    target_name = target["name"]
    _log.info(
        "Target metrics reset: %s (id=%d)",
        target_name,
        target_id,
        extra={
            "event": "target_metrics_reset",
            "user": _get_username(user),
            "target_id": target_id,
            "target_name": target_name,
            "client_ip": client_ip,
        },
    )

    # TSDB files cannot join the SQLite transaction: delete them last, inside it, so a failure
    # rolls the SQLite deletes back and the reset can simply be retried.
    tsdb_dir = get_tsdb_dir(request)
    with sqlite_db.transaction(conn):
        deleted_alerts = sqlite_db.delete_alerts_by_target(conn, target_id)
        sqlite_db.delete_target_status(conn, target_id)
        tsdb_db.delete_target(tsdb_dir, target_id)
        if (tsdb_dir / f"target_{target_id}").exists():
            raise HTTPException(status_code=500, detail="Failed to delete metric data; reset not applied")
    if deleted_alerts > 0:
        _log.info("Deleted %d alerts for target %d", deleted_alerts, target_id)

    event_bus.publish("target.changed", {"target_id": target_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Target Alert Recipients ───────────────────────────────────────────────


@router.get("/{target_id}/alert-users")
def get_target_alert_users(target_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
    """Get recipient IDs assigned to receive alerts for a target."""
    if sqlite_db.get_target(conn, target_id) is None:
        raise HTTPException(status_code=404, detail="Target not found")

    return sqlite_db.get_target_recipient_ids(conn, target_id)


# In-memory store for notification tasks (target_id -> task_id -> status)
# NOTE: In-memory only (not persistent). Safe because:
# - Tasks are short-lived (seconds to minutes)
# - UI polling tolerates loss on restart (falls back to timeout)
# - Avoids DB/Redis complexity for ephemeral progress tracking
# - Single-worker deployment assumption (multi-worker would need shared state)
_notification_tasks: dict[int, dict[str, dict]] = {}

# Strong references to fire-and-forget background tasks so the event loop does
# not garbage-collect them mid-execution (see RUF006 / asyncio docs).
_background_tasks: set[asyncio.Task] = set()
_NOTIFICATION_TASK_TTL_SECONDS = 300
MAX_ACTIVE_NOTIFICATION_TASKS = 4  # per worker; excess requests get 429 instead of queueing
_NOTIFICATION_RETRY_AFTER_SECONDS = 10
MAX_ALERT_RECIPIENTS = 200  # bounds per-request DB lookups and welcome-message fan-out

# Welcome tasks wait out an in-progress restore, but the wait must be bounded and cancellable:
# maintenance stays active until process restart, so an unbounded loop would spin forever.
_NOTIFICATION_MAINTENANCE_WAIT_SECONDS = 120.0
_NOTIFICATION_MAINTENANCE_POLL_SECONDS = 0.1

# Signalled on application shutdown so a waiting welcome task exits instead of spinning.
_notification_shutdown = threading.Event()


def signal_notification_shutdown() -> None:
    """Tell waiting welcome tasks to stop (application shutdown)."""
    _notification_shutdown.set()


def _mark_notification_deferred(target_id: int, task_id: str, reason: str) -> None:
    """Record a terminal, visible deferred outcome for a welcome task that could not run.

    An in-memory welcome task is not resumed after restart; surfacing a terminal ``deferred``
    status (and logging it) keeps the outcome observable instead of silently dropping it.
    """
    task_status = _notification_tasks.get(target_id, {}).get(task_id)
    if task_status is None:
        return
    now = time.monotonic()
    task_status["status"] = "deferred"
    task_status.setdefault("errors", []).append(reason)
    task_status["updated_at"] = now
    task_status["completed_at"] = now


def _cleanup_notification_tasks(
    target_id: int | None = None, *, max_age_seconds: int = _NOTIFICATION_TASK_TTL_SECONDS
) -> None:
    """Remove finished notification tasks older than TTL."""
    now = time.monotonic()
    target_ids = [target_id] if target_id is not None else list(_notification_tasks.keys())
    for tid in target_ids:
        tasks = _notification_tasks.get(tid)
        if not tasks:
            continue
        stale_ids = []
        for task_id, task in tasks.items():
            done_at = task.get("completed_at")
            if done_at is not None and (now - float(done_at)) > max_age_seconds:
                stale_ids.append(task_id)
        for task_id in stale_ids:
            tasks.pop(task_id, None)
        if not tasks:
            _notification_tasks.pop(tid, None)


async def _safe_notify_recipients_background(
    db_path: Path,
    target_id: int,
    task_id: str,
    target_name: str,
    recipient_ids: set[int],
) -> None:
    """Run sync notification worker safely and surface failures in task status."""
    try:
        await asyncio.to_thread(
            _notify_recipients_background_sync,
            db_path,
            target_id,
            task_id,
            target_name,
            recipient_ids,
        )
    except Exception as e:  # noqa: BLE001 - best-effort/background boundary: log and continue so the request or task is not aborted
        _log.exception("Background notification task %s failed", task_id)
        task_status = _notification_tasks.get(target_id, {}).get(task_id)
        if task_status is not None:
            task_status["status"] = "failed"
            task_status.setdefault("errors", []).append(str(e))
            task_status["updated_at"] = time.monotonic()
            task_status["completed_at"] = time.monotonic()


@router.put("/{target_id}/alert-users")
async def set_target_alert_users(
    target_id: int,
    request: Request,
    recipient_ids: Annotated[list[int], Field(max_length=MAX_ALERT_RECIPIENTS)],
    conn=Depends(get_conn),
    _user=Depends(require_admin),
):
    """Set recipient IDs for a target's alerts (replaces existing).

    Returns immediately after saving. If new recipients were added,
    a notification_task_id is returned to poll for notification status.
    """
    target = sqlite_db.get_target(conn, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    _cleanup_notification_tasks(target_id)

    # Validate all recipient IDs exist
    for rid in recipient_ids:
        if sqlite_db.get_recipient(conn, rid) is None:
            raise HTTPException(status_code=400, detail=f"Recipient {rid} not found")

    # Get previously assigned recipients to determine newly added ones
    existing_ids = set(sqlite_db.get_target_recipient_ids(conn, target_id))
    new_ids = set(recipient_ids) - existing_ids

    # Reject before touching the assignment so a 429 never leaves a half-applied request.
    if new_ids and len(_background_tasks) >= MAX_ACTIVE_NOTIFICATION_TASKS:
        raise HTTPException(
            status_code=429,
            detail="Too many notification tasks running; retry shortly",
            headers={"Retry-After": str(_NOTIFICATION_RETRY_AFTER_SECONDS)},
        )

    # Update the assignment (this is fast, do it synchronously)
    sqlite_db.set_target_recipients(conn, target_id, recipient_ids)

    # If new recipients, start background notification task
    notification_task_id: str | None = None
    if new_ids:
        target_name = target["name"]
        db_path = Path(request.app.state.db_path)
        notification_task_id = str(uuid.uuid4())

        # Initialize task status
        if target_id not in _notification_tasks:
            _notification_tasks[target_id] = {}
        _notification_tasks[target_id][notification_task_id] = {
            "status": "running",
            "total": len(new_ids),
            "success": 0,
            "queued": 0,  # Queued for later delivery (not a failure)
            "failed": 0,  # Permanently failed
            "errors": [],
            "updated_at": time.monotonic(),
            "completed_at": None,
        }

        # Start background task in thread pool (fire-and-forget, non-blocking)
        # This prevents blocking the FastAPI event loop during I/O-heavy notification sends
        _bg_task = asyncio.create_task(
            _safe_notify_recipients_background(
                db_path,
                target_id,
                notification_task_id,
                target_name,
                new_ids,
            )
        )
        _background_tasks.add(_bg_task)
        _bg_task.add_done_callback(_background_tasks.discard)

    return {
        "status": "ok",
        "recipient_ids": recipient_ids,
        "new_recipient_count": len(new_ids),
        "notification_task_id": notification_task_id,
    }


@router.get("/{target_id}/notification-status/{task_id}")
def get_notification_status(target_id: int, task_id: str, _user=Depends(get_current_user)):
    """Poll the status of a background notification task."""
    _cleanup_notification_tasks(target_id)

    if target_id not in _notification_tasks:
        raise HTTPException(status_code=404, detail="No notification tasks for this target")

    task_status = _notification_tasks[target_id].get(task_id)
    if task_status is None:
        raise HTTPException(status_code=404, detail="Notification task not found")

    # Clean up completed tasks after returning (keep for 5 minutes max via cleanup elsewhere)
    return task_status


def _resolve_welcome_signal_sender(conn, signal_backend) -> str | None:
    """Return the configured Signal sender validated against signal-cli, or None to skip the Signal welcome.

    The configured number is authoritative; another registered account is never substituted.
    """
    from ..utils.constants import SettingKeys

    configured = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
    if not configured:
        _log.warning("Signal welcome notification skipped: no Signal sender number is configured")
        return None
    try:
        return signal_backend.get_valid_sender(str(configured))
    except Exception as e:  # noqa: BLE001 - best-effort/background boundary: skip Signal, keep other channels
        _log.warning("Signal welcome notification skipped: sender not usable (%s)", type(e).__name__)
        return None


def _notify_recipients_background_sync(
    db_path: Path, target_id: int, task_id: str, target_name: str, recipient_ids: set[int]
) -> None:
    """Wait out restore, then enqueue welcome notices against the current data generation.

    The wait is bounded and cancellable: it stops on a stale generation (the restore replaced the
    data under this task), on application shutdown, or after ``_NOTIFICATION_MAINTENANCE_WAIT_SECONDS``.
    In each of those cases the task is marked ``deferred`` rather than looping forever or silently
    dropping an un-enqueued welcome.
    """
    generation = maintenance.current_generation()
    deadline = time.monotonic() + _NOTIFICATION_MAINTENANCE_WAIT_SECONDS
    while True:
        try:
            with maintenance.work_section(generation):
                _notify_recipients_background_in_section(db_path, target_id, task_id, target_name, recipient_ids)
            return
        except maintenance.StaleWorkError:
            # Data was replaced by a restore. The captured recipient set belongs to the pre-restore
            # snapshot, so do not blindly continue; surface a terminal deferred outcome.
            _log.warning(
                "Welcome notification task %s for target %d deferred: data generation changed during restore",
                task_id,
                target_id,
            )
            _mark_notification_deferred(target_id, task_id, "deferred: data generation changed during restore")
            return
        except maintenance.MaintenanceActiveError:
            # Restore must not wait for an unstarted welcome task. Wait, but bail out on shutdown or
            # once the bound is reached so the thread cannot spin until process restart.
            if _notification_shutdown.is_set():
                _log.warning(
                    "Welcome notification task %s for target %d deferred: shutdown during maintenance",
                    task_id,
                    target_id,
                )
                _mark_notification_deferred(target_id, task_id, "deferred: shutdown during maintenance")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log.warning(
                    "Welcome notification task %s for target %d deferred: maintenance still active after %.0fs",
                    task_id,
                    target_id,
                    _NOTIFICATION_MAINTENANCE_WAIT_SECONDS,
                )
                _mark_notification_deferred(target_id, task_id, "deferred: maintenance still active after wait timeout")
                return
            # Event.wait returns early (True) when shutdown is signalled, so the thread is cancellable.
            if _notification_shutdown.wait(min(_NOTIFICATION_MAINTENANCE_POLL_SECONDS, remaining)):
                continue


def _notify_recipients_background_in_section(
    db_path: Path, target_id: int, task_id: str, target_name: str, recipient_ids: set[int]
) -> None:
    """
    Background task to send welcome notifications to newly added recipients.
    Updates the in-memory task status as it progresses.

    IMPORTANT: This function runs in a thread pool via asyncio.to_thread()
    to prevent blocking the FastAPI event loop during I/O operations.
    All I/O here is synchronous/blocking (signal-cli subprocess, SMTP).
    """
    import html

    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.spooler import (
        ChannelType,
        NotificationType,
        get_spooler,
    )

    task_status = _notification_tasks.get(target_id, {}).get(task_id)
    if task_status is None:
        return

    message = f"📣 You were just added to the alerting group for '{target_name}'."
    email_message = f"📣 You were just added to the alerting group for '{target_name}'."

    conn = sqlite_db.connect(db_path)
    from ..utils.constants import SettingKeys

    target = sqlite_db.get_target(conn, target_id)
    if target is None:
        task_status["status"] = "completed"
        task_status["updated_at"] = time.monotonic()
        task_status["completed_at"] = time.monotonic()
        sqlite_db.close_connection(conn)
        return
    target_name = str(target["name"])
    system_url = sqlite_db.get_setting(conn, SettingKeys.SYSTEM_BASE_URL, None) or "#"
    github_url = "https://github.com/Gill-Bates/justup"

    # Get Signal backend and sender (if configured)
    signal_backend = get_signal_backend()
    signal_enabled = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_CHANNEL_ENABLED, False)
    signal_sender = _resolve_welcome_signal_sender(conn, signal_backend) if signal_enabled else None

    # Get SMTP enabled status (spooler handles the actual SMTP config)
    smtp_enabled = sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, False)

    try:
        spooler = get_spooler(lambda: sqlite_db.connect(db_path))

        # A later PUT may have removed recipients while this task was queued.
        assigned_ids = set(sqlite_db.get_target_recipient_ids(conn, target_id))
        for rid in recipient_ids:
            recipient = sqlite_db.get_recipient(conn, rid)
            if rid not in assigned_ids:
                task_status["success"] += 1
                continue
            if not recipient or not recipient["is_enabled"]:
                task_status["success"] += 1  # Skip counts as success
                continue

            recipient_name = recipient["name"]
            phone = recipient["phone"] or None
            email = recipient["email"] or None
            # ══════════════════════════════════════════════════════════════════════════
            # ARCHITECTURE: Signal goes through spooler for rate-limit protection.
            #
            # If queue is empty (idle), enqueue with immediate=True so it's sent
            # on next scheduler tick (within seconds). If queue has backlog, normal
            # queueing ensures FIFO order and prevents bursts.
            # ══════════════════════════════════════════════════════════════════════════
            if phone and signal_sender:
                # Check if Signal channel is idle (no pending messages)
                is_idle = spooler.is_channel_idle(ChannelType.SIGNAL)
                spooler.enqueue(
                    notification_type=NotificationType.WELCOME,
                    channel_type=ChannelType.SIGNAL,
                    recipient_id=rid,
                    recipient_name=recipient_name,
                    recipient_address=phone,
                    payload={
                        "message": message,
                        "sender": signal_sender,
                        "target_id": target_id,
                        "target_name": target_name,
                    },
                    immediate=is_idle,  # Process immediately if queue is empty
                )
                _log.info(
                    "Queued welcome notification via Signal for %s (immediate=%s)",
                    recipient_name,
                    is_idle,
                )
                task_status["queued"] += 1

            # Send via Email if configured
            if email and smtp_enabled:
                subject = f"🎉 justUp! – You were added: {target_name}"

                safe_recipient_name = html.escape(str(recipient_name or ""))
                safe_target_name = html.escape(str(target_name or ""))
                safe_message = html.escape(str(email_message or ""))
                safe_system_url = html.escape(str(system_url or "#"), quote=True)
                safe_github_url = html.escape(str(github_url or ""), quote=True)

                html_body = _build_welcome_email_html(
                    safe_recipient_name,
                    safe_target_name,
                    safe_message,
                    system_url=safe_system_url,
                    github_url=safe_github_url,
                )
                text_body = (
                    f"Hi {recipient_name},\n\n"
                    f"{email_message}\n\n"
                    f"From now on, you will receive notifications when the target '{target_name}' "
                    f"goes down or comes back up.\n\n"
                    "This message was sent automatically.\n\n"
                    f"justUp! © 2026 Gill-Bates · GitHub: {github_url}\n"
                )

                # ══════════════════════════════════════════════════════════════════════
                # ARCHITECTURE: Email goes through spooler (same as Signal).
                # Nothing leaves the system without going through the spooler.
                # ══════════════════════════════════════════════════════════════════════
                is_idle = spooler.is_channel_idle(ChannelType.EMAIL)
                spooler.enqueue(
                    notification_type=NotificationType.WELCOME,
                    channel_type=ChannelType.EMAIL,
                    recipient_id=rid,
                    recipient_name=recipient_name,
                    recipient_address=email,
                    payload={
                        "subject": subject,
                        "text_body": text_body,
                        "html_body": html_body,
                        "target_id": target_id,
                        "target_name": target_name,
                    },
                    immediate=is_idle,
                )
                _log.info(
                    "Queued welcome notification via Email for %s (immediate=%s)",
                    recipient_name,
                    is_idle,
                )
                task_status["queued"] += 1

            # Count as success if at least one channel succeeded immediately
            # (queued notifications will be delivered later)
            task_status["success"] += 1
            task_status["updated_at"] = time.monotonic()
    except Exception as e:  # noqa: BLE001 - best-effort/background boundary: log and continue so the request or task is not aborted
        _log.exception("Welcome notification task failed for target %d", target_id)
        task_status["status"] = "failed"
        task_status["errors"].append(str(e))
        task_status["updated_at"] = time.monotonic()
        task_status["completed_at"] = time.monotonic()
    finally:
        sqlite_db.close_connection(conn)

    # Determine final status
    # - completed: all sent immediately
    # - completed_with_queued: some queued for later (not an error)
    # - completed_with_errors: permanent failures (currently not possible, all failures are queued)
    if task_status["status"] != "failed":
        if task_status["queued"] > 0:
            task_status["status"] = "completed_with_queued"
        else:
            task_status["status"] = "completed"
        task_status["updated_at"] = time.monotonic()
        task_status["completed_at"] = time.monotonic()


def _build_welcome_email_html(
    recipient_name: str,
    target_name: str,
    message: str,
    *,
    system_url: str,
    github_url: str,
) -> str:
    """Build HTML email body for welcome notification.

    All arguments must already be HTML-escaped by the caller.
    """
    # Only http(s) URLs become a link; the "#" fallback or other schemes are omitted.
    system_link_html = ""
    if system_url.lower().startswith(("http://", "https://")):
        system_link_html = (
            f'<p style="margin:0 0 10px 0;"><a href="{system_url}" '
            'style="color:#0d6efd;text-decoration:underline;">Open justUp!</a></p>'
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <title>justUp! Notification</title>
  <style>
    @media (prefers-color-scheme: dark) {{
      body, .mail-bg {{
        background-color:#2b2d31 !important;
        color:#f9fafb !important;
      }}
      .mail-card {{
        background-color:#1f2937 !important;
        color:#f9fafb !important;
      }}
      .mail-label {{
        color:#9ca3af !important;
      }}
      .mail-divider {{
        border-top-color:#374151 !important;
      }}
      .mail-footer {{
        background-color:#111827 !important;
        color:#9ca3af !important;
      }}
      a {{
        color:#93c5fd !important;
      }}
    }}

    /* Outlook.com / Office 365 dark mode helpers */
    [data-ogsc] body, [data-ogsc] .mail-bg {{
      background-color:#2b2d31 !important;
      color:#f9fafb !important;
    }}
    [data-ogsc] .mail-card {{
      background-color:#1f2937 !important;
      color:#f9fafb !important;
    }}
    [data-ogsc] .mail-label {{
      color:#9ca3af !important;
    }}
    [data-ogsc] .mail-divider {{
      border-top-color:#374151 !important;
    }}
    [data-ogsc] .mail-footer {{
      background-color:#111827 !important;
      color:#9ca3af !important;
    }}
    [data-ogsc] a {{
      color:#93c5fd !important;
    }}
    @media only screen and (max-width: 600px) {{
      .stack td {{
        display:block !important;
        width:100% !important;
      }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background-color:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
         class="mail-bg"
         style="background-color:#f5f7fa;color:#1f2937;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
          <tr><td height="20" style="font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:16px;">
          <tr>
            <td align="center">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                     class="mail-card"
                     style="max-width:600px;background-color:#fafafa;border-radius:8px;overflow:hidden;color:#1f2937;">
                <tr>
                  <td style="height:6px;background-color:#0dcaf0;font-size:0;line-height:6px;">&nbsp;</td>
                </tr>
                <tr>
                  <td style="padding:16px 20px;font-size:18px;font-weight:bold;line-height:1.3;">
                    📣 Welcome to justUp!
                  </td>
                </tr>
                <tr>
                  <td style="padding:16px 20px;font-size:14px;line-height:1.6;">
                    <p style="margin:0 0 10px 0;">Hi {recipient_name},</p>
                    <p style="margin:0 0 10px 0;">{message}</p>
                    <p style="margin:0 0 10px 0;">
                      From now on, you will receive notifications when the target <strong>{target_name}</strong>
                      goes down or comes back up.
                    </p>
                    {system_link_html}
                    <hr class="mail-divider" style="border:none;border-top:1px solid #d1d5db;margin:16px 0;">
                    <p class="mail-label" style="margin:0;color:#9ca3af;font-size:12px;line-height:1.6;">
                      This message was sent automatically.
                    </p>
                  </td>
                </tr>
                <tr>
                  <td class="mail-footer"
                      style="padding:14px 16px;
                             background-color:#f1f3f6;
                             color:#9ca3af;
                             font-size:12px;
                             text-align:center;
                             line-height:1.6;">
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                      <tr>
                        <td style="text-align:left;vertical-align:middle;">
                          justUp! © 2026 Gill-Bates ·
                          <a href="{github_url}" style="color:#9ca3af;text-decoration:underline;">
                            GitHub
                          </a>
                        </td>
                        <td style="text-align:right;vertical-align:middle;">
                          <img src="https://raw.githubusercontent.com/Gill-Bates/justUp/f41b710f4d4a9bdbfc164f630338c478864631e0/static/img/justup_sw_email.png"
                               alt="justUp! Logo"
                               style="height:35px;width:auto;display:block;margin-left:auto;">
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
          <tr><td height="24" style="font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ─── Legacy User-Based Alerts (DEPRECATED) ─────────────────────────────────
# Old architecture: Users received alerts via notification_channels table
# New architecture: Recipients receive alerts (phone/email on recipient model)


@router.get("/{target_id}/recipients", deprecated=True)
def get_target_recipients_legacy(target_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
    """[DEPRECATED] Old user-based alert system. Use /alert-users for recipient-based."""
    if sqlite_db.get_target(conn, target_id) is None:
        raise HTTPException(status_code=404, detail="Target not found")
    users = sqlite_db.get_target_alert_users(conn, target_id)
    return [u["id"] for u in users]


@router.put("/{target_id}/recipients", deprecated=True)
def set_target_recipients_legacy(
    target_id: int, user_ids: list[int], conn=Depends(get_conn), _user=Depends(require_admin)
):
    """[DEPRECATED] Old user-based alert system. Use /alert-users for recipient-based."""
    if sqlite_db.get_target(conn, target_id) is None:
        raise HTTPException(status_code=404, detail="Target not found")

    # Validate all user IDs exist
    for uid in user_ids:
        if sqlite_db.get_user(conn, uid) is None:
            raise HTTPException(status_code=400, detail=f"User {uid} not found")

    sqlite_db.set_target_alert_users(conn, target_id, user_ids)
    return {"status": "ok", "user_ids": user_ids}


# ─── Test Alert ────────────────────────────────────────────────────────────


@router.post("/{target_id}/test-alert")
@limiter.limit("5/minute")  # Each call fans out to all recipients
async def trigger_test_alert(target_id: int, request: Request, conn=Depends(get_conn), _user=Depends(require_admin)):
    """
    Trigger a test alert for a target.

    Queues a test alert for all configured recipients via enabled channels
    (Email and/or Signal). This allows users to verify their notification
    setup is working correctly.

    All notifications go through the spooler.
    """
    from ..services.notifications.dispatcher import AlertPayload, DispatchResult, NotificationDispatcher

    # Get target
    target = sqlite_db.get_target(conn, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")

    # Check if target has any recipients configured
    recipient_ids = sqlite_db.get_target_recipient_ids(conn, target_id)
    if not recipient_ids:
        raise HTTPException(
            status_code=400,
            detail="No alert recipients configured for this target. Add recipients in the target settings.",
        )

    # Create test alert payload
    test_alert = AlertPayload(
        alert_id=f"test-{uuid.uuid4().hex[:8]}",
        target_id=target_id,
        target_name=target["name"],
        failed_service="TEST",
        status="test",
        started_at=datetime.now(UTC),
    )

    db_path = request.app.state.db_path

    def _dispatch() -> DispatchResult:
        # Own connection: the request-scoped connection must not cross threads.
        worker_conn = sqlite_db.connect(db_path)
        try:
            # Dispatcher enqueues to spooler — no direct sends
            dispatcher = NotificationDispatcher(
                conn=worker_conn,
                db_conn_factory=lambda: sqlite_db.connect(db_path),
            )
            return dispatcher.dispatch_alert(test_alert)
        finally:
            sqlite_db.close_connection(worker_conn)

    result = await run_in_threadpool(_dispatch)

    if result.channels_attempted == 0:
        raise HTTPException(status_code=409, detail="No enabled notification channels for this target")

    if result.channels_succeeded == 0:
        errors = [r.error for r in result.send_results if r.error]
        raise HTTPException(status_code=503, detail=f"Could not queue test alert: {'; '.join(errors[:3])}")

    failed = result.channels_attempted - result.channels_succeeded
    queue_ids = [r.queue_id for r in result.send_results if r.queue_id is not None]

    return {
        "status": "partial" if failed else "queued",
        "message": (
            f"Test alert queued for {result.users_notified} recipient(s) "
            f"via {result.channels_succeeded}/{result.channels_attempted} channel(s)"
        ),
        "recipients_queued": result.users_notified,
        "channels_attempted": result.channels_attempted,
        "channels_queued": result.channels_succeeded,
        "queue_ids": queue_ids,
    }
