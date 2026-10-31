#!/usr/bin/env python3
#
# app/api/recipients.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""API endpoints for managing notification recipients."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from html import escape
from threading import Lock
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import Field, StringConstraints

from ..db import sqlite as sqlite_db
from ..models.recipients import (
    RecipientCreate,
    RecipientPublic,
    RecipientResponse,
    RecipientTestChannelResult,
    RecipientTestRequest,
    RecipientTestResponse,
    RecipientTestStatus,
    RecipientUpdate,
    TargetBrief,
)
from ..utils.constants import SettingKeys
from ..utils.deps import get_conn
from ..utils.phone import mask_phone, normalize_phone
from ..utils.rate_limit import RATE_LIMIT_HEAVY, limiter
from .auth import get_current_user, require_admin

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/recipients", tags=["recipients"])

# Cache for Signal registration status: phone -> (is_registered, timestamp_monotonic)
# Protected by _SIGNAL_CACHE_LOCK for thread-safety across FastAPI workers and ThreadPoolExecutor
_SIGNAL_REGISTRATION_CACHE: dict[str, tuple[bool, float]] = {}
_SIGNAL_CACHE_LOCK = Lock()
_SIGNAL_CACHE_TTL = 86400.0  # 24 hours
_SIGNAL_CACHE_MAX_SIZE = 1000  # Prevent unbounded growth

# Bound the verify-signal request body (each number costs a signal-cli lookup)
_PhoneList = Annotated[list[Annotated[str, StringConstraints(max_length=32)]], Field(max_length=50)]


def _normalize_phone_or_400(phone: str) -> str:
    """Normalize to E.164 or raise HTTP 400."""
    try:
        return normalize_phone(phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid phone number: {e}") from e


def _signal_registration_status(phone: str) -> bool | None:
    """Check if phone is registered with Signal.

    Centralized low-level function used by both warning generation and
    verification API. Caches results for 24h using time.monotonic().
    Thread-safe via _SIGNAL_CACHE_LOCK.

    Args:
            phone: E.164 formatted phone number

    Returns:
            True if registered, False if not registered, None if check unavailable
    """
    now = time.monotonic()

    # Check cache first (thread-safe read)
    with _SIGNAL_CACHE_LOCK:
        if phone in _SIGNAL_REGISTRATION_CACHE:
            is_registered, cached_at = _SIGNAL_REGISTRATION_CACHE[phone]
            if (now - cached_at) < _SIGNAL_CACHE_TTL:
                return is_registered

    try:
        from ..services.notifications.signal_backend import get_signal_backend
        from ..services.notifications.signal_errors import (
            SignalNotRegistered,
            SignalUnavailable,
        )

        backend = get_signal_backend()
        accounts = backend.list_accounts()
        if not accounts:
            _log.debug("No Signal accounts registered, skipping registration check")
            return None  # Signal not configured

        sender = accounts[0]
        is_registered = backend.check_user_registered(phone, sender=sender)

        # Cache the result (thread-safe write with LRU eviction)
        with _SIGNAL_CACHE_LOCK:
            if len(_SIGNAL_REGISTRATION_CACHE) >= _SIGNAL_CACHE_MAX_SIZE:
                # Remove oldest entry
                oldest_key = min(_SIGNAL_REGISTRATION_CACHE, key=lambda k: _SIGNAL_REGISTRATION_CACHE[k][1])
                del _SIGNAL_REGISTRATION_CACHE[oldest_key]
            _SIGNAL_REGISTRATION_CACHE[phone] = (is_registered, now)

        return is_registered

    except SignalNotRegistered:
        _log.debug("No Signal sender configured")
        return None
    except SignalUnavailable:
        _log.debug("Signal backend unavailable")
        return None
    except Exception as e:  # noqa: BLE001 - Signal-check boundary: any failure degrades to unknown registration
        _log.debug("Error checking Signal registration: %s", e)
        return None


def _check_signal_registration(phone: str) -> str | None:
    """Check if phone is registered with Signal, return warning if not.

    Uses _signal_registration_status() for actual check.

    Returns:
            Warning message if not registered, None if registered or check unavailable
    """
    registration_status = _signal_registration_status(phone)

    # None = check unavailable, True = registered -> no warning
    if registration_status is None or registration_status is True:
        return None

    # status is False -> not registered
    return (
        f"The provided phone number ({mask_phone(phone)}) is not registered with Signal. "
        "Signal notifications will fail."
    )


def _row_to_public(row: Mapping[str, Any], targets: Sequence[Mapping[str, Any]] | None = None) -> RecipientPublic:
    return RecipientPublic(
        id=int(row["id"]),
        name=row["name"],
        phone=row["phone"] or None,
        email=row["email"] or None,
        is_enabled=bool(row["is_enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        targets=[TargetBrief(id=t["id"], name=t["name"]) for t in (targets or [])],
    )


@router.get("", response_model=list[RecipientPublic])
def list_recipients(conn=Depends(get_conn), _user=Depends(get_current_user)):
    """List all alert recipients."""
    rows = sqlite_db.list_recipients(conn)
    targets_by_recipient = sqlite_db.get_targets_by_recipient(conn)
    return [_row_to_public(r, targets_by_recipient.get(r["id"])) for r in rows]


def _is_phone_probably_registered(phone: str) -> bool | None:
    """Check if a phone is likely registered with Signal.

    Wrapper around _signal_registration_status() for tri-state API.

    Args:
            phone: E.164 formatted phone number

    Returns:
            True if registered, False if not registered, None if check unavailable
    """
    return _signal_registration_status(phone)


@router.post("/verify-signal")
@limiter.limit(RATE_LIMIT_HEAVY)
def verify_signal_numbers(
    request: Request,  # noqa: ARG001 - required by slowapi
    phones: _PhoneList,
    _conn=Depends(get_conn),
    _user=Depends(require_admin),
):
    """Verify which phone numbers are registered with Signal.

    Checks are run in parallel (up to 3 concurrent) to minimize latency.
    Cached results are returned immediately.

    Args:
            phones: List of phone numbers to check (will be normalized to E.164)

    Returns:
            Dict with:
            - "results": mapping of normalized E.164 phone numbers to registration status
            - "invalid": list of phone numbers that could not be normalized
    """
    results: dict[str, bool | None] = {}
    invalid_phones: list[str] = []
    phones_to_check: list[str] = []  # normalized phones to check

    for phone in phones:
        if not phone:
            continue

        # Normalize to E.164 for consistent cache keys
        try:
            normalized = normalize_phone(phone)
        except ValueError:
            # Invalid phone format -> track separately
            invalid_phones.append(phone)
            continue

        # Check cache first (fast path, thread-safe)
        with _SIGNAL_CACHE_LOCK:
            if normalized in _SIGNAL_REGISTRATION_CACHE:
                is_registered, cached_at = _SIGNAL_REGISTRATION_CACHE[normalized]
                if (time.monotonic() - cached_at) < _SIGNAL_CACHE_TTL:
                    results[normalized] = is_registered
                    continue

        if normalized not in phones_to_check:
            phones_to_check.append(normalized)

    # No uncached phones -> return immediately
    if not phones_to_check:
        return {"results": results, "invalid": invalid_phones}

    # Check uncached phones sequentially.
    # signal-cli uses file-level locking, so parallel threads just queue up
    # behind the global process lock.  Sequential is simpler and avoids
    # thread-pool timeout issues.
    for normalized in phones_to_check:
        try:
            is_registered = _is_phone_probably_registered(normalized)
            results[normalized] = is_registered
        except Exception as e:  # noqa: BLE001 - Signal-check boundary: any failure degrades to unknown registration
            _log.warning("Signal check failed for %s: %s", mask_phone(normalized), e)
            results[normalized] = None

    return {"results": results, "invalid": invalid_phones}


@router.get("/{recipient_id}", response_model=RecipientPublic)
def get_recipient(recipient_id: int, conn=Depends(get_conn), _user=Depends(get_current_user)):
    """Get a single recipient."""
    row = sqlite_db.get_recipient(conn, recipient_id)
    if not row:
        raise HTTPException(status_code=404, detail="Recipient not found")
    targets = sqlite_db.get_recipient_targets(conn, recipient_id)
    return _row_to_public(row, targets)


@router.post("", response_model=RecipientResponse, status_code=status.HTTP_201_CREATED)
def create_recipient(payload: RecipientCreate, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Create a new alert recipient."""
    warnings: list[str] = []

    # Validate at least one channel
    if not payload.phone and not payload.email:
        raise HTTPException(status_code=400, detail="At least one contact method (phone or email) is required")

    # Normalize phone to E.164 format if provided
    normalized_phone = None
    if payload.phone:
        normalized_phone = _normalize_phone_or_400(payload.phone)

        # Check for duplicate phone
        existing = sqlite_db.get_recipient_by_phone(conn, normalized_phone)
        if existing:
            raise HTTPException(status_code=400, detail="Phone number already exists")

        # Check if phone is registered with Signal
        signal_warning = _check_signal_registration(normalized_phone)
        if signal_warning:
            warnings.append(signal_warning)

    new_id = sqlite_db.create_recipient(
        conn,
        name=payload.name,
        phone=normalized_phone,
        email=payload.email,
        is_enabled=payload.is_enabled,
    )
    row = sqlite_db.get_recipient(conn, new_id)
    recipient = _row_to_public(row, [])
    return RecipientResponse(recipient=recipient, warnings=warnings)


@router.patch("/{recipient_id}", response_model=RecipientResponse)
def update_recipient(recipient_id: int, payload: RecipientUpdate, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Update an alert recipient."""
    warnings: list[str] = []
    clear_phone = False
    clear_email = False

    existing = sqlite_db.get_recipient(conn, recipient_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Recipient not found")

    update_data = payload.model_dump(exclude_unset=True)

    # Normalize phone if provided
    if "phone" in update_data:
        if payload.phone:  # Not empty string or None
            normalized_phone = _normalize_phone_or_400(payload.phone)
            # Check phone uniqueness if changing
            if normalized_phone != existing["phone"]:
                dup = sqlite_db.get_recipient_by_phone(conn, normalized_phone)
                if dup and dup["id"] != recipient_id:
                    raise HTTPException(status_code=400, detail="Phone number already exists")

                # Check if new phone is registered with Signal
                signal_warning = _check_signal_registration(normalized_phone)
                if signal_warning:
                    warnings.append(signal_warning)
            update_data["phone"] = normalized_phone
        else:
            # Phone is being cleared (null or empty string)
            clear_phone = True
            del update_data["phone"]

    # Handle email clearing
    if "email" in update_data and not payload.email:  # Empty string or None means clear
        clear_email = True
        del update_data["email"]

    # Validate at least one channel after update
    final_phone = None if clear_phone else update_data.get("phone", existing["phone"])
    final_email = None if clear_email else update_data.get("email", existing["email"])
    if not final_phone and not final_email:
        raise HTTPException(status_code=400, detail="At least one contact method (phone or email) is required")

    if update_data or clear_phone or clear_email:
        sqlite_db.update_recipient(conn, recipient_id, clear_phone=clear_phone, clear_email=clear_email, **update_data)

    row = sqlite_db.get_recipient(conn, recipient_id)
    targets = sqlite_db.get_recipient_targets(conn, recipient_id)
    recipient = _row_to_public(row, targets)
    return RecipientResponse(recipient=recipient, warnings=warnings)


@router.delete("/{recipient_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipient(recipient_id: int, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Delete an alert recipient."""
    existing = sqlite_db.get_recipient(conn, recipient_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Recipient not found")

    sqlite_db.delete_recipient(conn, recipient_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── Test messages ────────────────────────────────────────────────────────────
#
# Test messages use the notification spooler like every other notification, so SMTP/Signal I/O
# runs in the spooler worker (outside any maintenance section) with its timeouts. The POST only
# validates and enqueues; the client polls the status endpoint for the delivery result.

RECIPIENT_TEST_COOLDOWN_SECONDS = 30  # per recipient and channel, on top of the per-IP rate limit
_TEST_CHANNEL_LABELS = {"signal": "Signal", "email": "Email"}
_SERVICE_LABELS = {"signal": "Signal", "email": "SMTP"}


class _TestRefusedError(Exception):
    """A test message cannot be queued; carries a client-safe message and HTTP-like code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _test_cooldown_active(conn, recipient_id: int, channel: str) -> bool:
    cutoff = (datetime.now(UTC) - timedelta(seconds=RECIPIENT_TEST_COOLDOWN_SECONDS)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM notification_queue
        WHERE notification_type = 'test' AND recipient_id = ? AND channel_type = ? AND created_at >= ?
        LIMIT 1
        """,
        (recipient_id, channel, cutoff),
    ).fetchone()
    return row is not None


def _build_signal_test_payload(conn, name: str) -> dict[str, str]:
    """Resolve the Signal sender (signal-cli is the authority) and build the test message."""
    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.signal_errors import SignalNotRegistered, SignalUnavailable

    configured = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None)
    try:
        sender = get_signal_backend().get_valid_sender(configured)
    except SignalNotRegistered:
        raise _TestRefusedError(409, "Signal is not connected. Link a Signal account in Settings first.") from None
    except SignalUnavailable:
        raise _TestRefusedError(503, "Signal service is unavailable.") from None
    except Exception as exc:  # noqa: BLE001 - Signal boundary: report a generic failure, never the internal text
        _log.warning("Signal sender lookup for recipient test failed: %s", type(exc).__name__)
        raise _TestRefusedError(503, "Signal service is unavailable.") from None
    message = f"🔔 justUp test message\n\nThis is a test for the recipient “{name}”. No action is required."
    return {"message": message, "sender": sender}


def _build_email_test_payload(conn, name: str) -> dict[str, str]:
    enabled = sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, False)
    host = sqlite_db.get_setting(conn, SettingKeys.SMTP_HOST, None)
    sender = sqlite_db.get_setting(conn, SettingKeys.SMTP_FROM, None)
    if not enabled or not host or not sender:
        raise _TestRefusedError(409, "Email is not configured. Enable and complete the SMTP settings first.")
    text = f"This is a justUp test message for the recipient “{name}”. No action is required."
    html = (
        "<p><strong>justUp test message</strong></p>"
        f"<p>This is a test for the recipient “{escape(name)}”. No action is required.</p>"
    )
    return {"subject": "[justUp] Test message", "text_body": text, "html_body": html}


def _queue_recipient_test(conn, request: Request, row: Mapping[str, Any], channel: str) -> int:
    """Validate the saved channel and enqueue one test message; returns the queue id."""
    from ..services.notifications.spooler import (
        ChannelType,
        NotificationType,
        SignalQueueLimitError,
        get_spooler,
    )

    recipient_id = int(row["id"])
    address = row["phone"] if channel == "signal" else row["email"]
    if not address:
        raise _TestRefusedError(400, f"This recipient has no {_TEST_CHANNEL_LABELS[channel]} address saved.")
    if _test_cooldown_active(conn, recipient_id, channel):
        raise _TestRefusedError(
            429, f"A {_TEST_CHANNEL_LABELS[channel]} test was just sent. Wait a moment before trying again."
        )
    if channel == "signal":
        payload = _build_signal_test_payload(conn, row["name"])
    else:
        payload = _build_email_test_payload(conn, row["name"])
    try:
        db_path = request.app.state.db_path
        spooler = get_spooler(lambda: sqlite_db.connect(db_path))
        return spooler.enqueue(
            notification_type=NotificationType.TEST,
            channel_type=ChannelType(channel),
            recipient_id=recipient_id,
            recipient_name=row["name"],
            recipient_address=address,
            payload=payload,
            immediate=True,
        )
    except SignalQueueLimitError:
        raise _TestRefusedError(429, "Signal test-message limit reached. Try again later.") from None
    except Exception as exc:  # noqa: BLE001 - queue boundary: report a generic failure, never the internal text
        _log.warning("Recipient test enqueue failed (recipient_id=%d): %s", recipient_id, type(exc).__name__)
        raise _TestRefusedError(503, "The notification queue is unavailable.") from None


@router.post("/{recipient_id}/test", response_model=RecipientTestResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(RATE_LIMIT_HEAVY)
def send_recipient_test(
    request: Request,
    recipient_id: int,
    payload: RecipientTestRequest | None = None,
    conn=Depends(get_conn),
    _admin=Depends(require_admin),
):
    """Queue a test message to the recipient's SAVED channels (admin only).

    The saved configuration is used on purpose, so the test proves what alerts will use.
    Disabled recipients can be tested: it lets an admin verify channels before enabling.
    One channel failing with ``channel=all`` does not block the other; if nothing could be
    queued, the first failure is returned as the HTTP error.
    """
    row = sqlite_db.get_recipient(conn, recipient_id)
    if not row:
        raise HTTPException(status_code=404, detail="Recipient not found")
    requested = (payload or RecipientTestRequest()).channel
    if requested == "all":
        channels = [c for c, key in (("signal", "phone"), ("email", "email")) if row[key]]
    else:
        channels = [requested]
    results: list[RecipientTestChannelResult] = []
    for channel in channels:
        try:
            queue_id = _queue_recipient_test(conn, request, row, channel)
        except _TestRefusedError as refused:
            results.append(
                RecipientTestChannelResult(
                    channel=channel, state="error", message=refused.message, status_code=refused.status_code
                )
            )
            continue
        _log.info("Recipient test queued: recipient_id=%d channel=%s queue_id=%d", recipient_id, channel, queue_id)
        results.append(
            RecipientTestChannelResult(
                channel=channel,
                state="queued",
                queue_id=queue_id,
                message=f"{_TEST_CHANNEL_LABELS[channel]} test message queued.",
            )
        )
    if not any(r.state == "queued" for r in results):
        first = results[0]
        raise HTTPException(status_code=first.status_code or 400, detail=first.message)
    return RecipientTestResponse(results=results)


@router.get("/{recipient_id}/test/{queue_id}", response_model=RecipientTestStatus)
@limiter.limit("60/minute")
def get_recipient_test_status(
    request: Request,
    recipient_id: int,
    queue_id: int,
    conn=Depends(get_conn),
    _admin=Depends(require_admin),
):
    """Report the delivery state of a queued test message (admin only).

    A test is never retried: after the first failed attempt it is closed as failed, so the
    admin gets one clear result instead of a message that arrives hours later.
    """
    from ..services.notifications.spooler import UNCERTAIN_MARKER, get_spooler

    def _load():
        return conn.execute(
            """
            SELECT channel_type, status, retry_count, last_error FROM notification_queue
            WHERE id = ? AND recipient_id = ? AND notification_type = 'test'
            """,
            (queue_id, recipient_id),
        ).fetchone()

    item = _load()
    if not item:
        raise HTTPException(status_code=404, detail="Test message not found")
    channel = item["channel_type"]
    attempt_error = item["last_error"] or ""  # closing the row below replaces last_error
    if item["status"] in ("pending", "processing") and item["retry_count"] > 0:
        try:
            db_path = request.app.state.db_path
            get_spooler(lambda: sqlite_db.connect(db_path)).mark_failed_permanent(
                queue_id, "Test message is not retried"
            )
        except Exception as exc:  # noqa: BLE001 - best effort: the status below still reflects the stored row
            _log.warning("Closing failed recipient test %d failed: %s", queue_id, type(exc).__name__)
        item = _load() or item
    label = _TEST_CHANNEL_LABELS.get(channel, channel)
    if item["status"] == "sent":
        return RecipientTestStatus(channel=channel, state="sent", message=f"{label} test message sent.")
    if item["status"] == "failed" or item["retry_count"] > 0:
        if UNCERTAIN_MARKER in attempt_error or UNCERTAIN_MARKER in (item["last_error"] or ""):
            message = f"{label} delivery could not be confirmed. Check the recipient before retrying."
        else:
            message = (
                f"{label} delivery failed. Check the {_SERVICE_LABELS.get(channel, label)} settings and server log."
            )
        return RecipientTestStatus(channel=channel, state="failed", message=message)
    return RecipientTestStatus(channel=channel, state="pending", message=f"Waiting for {label} delivery…")
