#!/usr/bin/env python3
#
# app/api/recipients.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""API endpoints for managing notification recipients."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Mapping, Sequence
from html import escape
from threading import BoundedSemaphore, Event, Lock
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import Field, StringConstraints, ValidationError

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
from ..utils.rate_limit import RATE_LIMIT_HEAVY, rate_limit
from ..utils.smtp_readiness import SmtpReadiness, smtp_readiness
from .auth import get_current_user, require_admin

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/recipients", tags=["recipients"])


class _SignalFlight:
    """One in-progress forced lookup; concurrent callers for the same number wait for its result."""

    __slots__ = ("done", "error", "result")

    def __init__(self) -> None:
        self.done = Event()
        self.result: bool | None = None
        self.error: HTTPException | None = None


# Cache for Signal registration status: phone -> (is_registered, timestamp_monotonic)
# _SIGNAL_CACHE_LOCK makes access thread-safe within one worker process (request threads and the thread
# pool). It is not shared between processes: every worker process keeps its own cache.
_SIGNAL_REGISTRATION_CACHE: dict[str, tuple[bool, float]] = {}
_SIGNAL_CACHE_LOCK = Lock()
_SIGNAL_CACHE_TTL = 86400.0
_SIGNAL_CACHE_MAX_SIZE = 1000

# Explicit "re-check" (force_refresh) bypasses the cache once. Like the cache, the flight table and the
# slot limit live per worker process: the guarantees below hold within one process only.
_SIGNAL_FORCE_RATE_LIMIT = "5/minute"
_SIGNAL_FORCE_MAX_CONCURRENT = 2  # signal-cli lookups running at once across all recipients
_SIGNAL_FORCE_MIN_INTERVAL = 10.0  # a number refreshed this recently is answered from the cache
_SIGNAL_FLIGHT_WAIT = 30.0
_SIGNAL_FORCE_SLOTS = BoundedSemaphore(_SIGNAL_FORCE_MAX_CONCURRENT)
_SIGNAL_FLIGHT_LOCK = Lock()
_SIGNAL_FLIGHTS: dict[str, _SignalFlight] = {}

# Bound the verify-signal request body (each number costs a signal-cli lookup)
_PhoneList = Annotated[list[Annotated[str, StringConstraints(max_length=32)]], Field(max_length=50)]


def _normalize_phone_or_400(phone: str) -> str:
    """Normalize to E.164 or raise HTTP 400."""
    try:
        return normalize_phone(phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid phone number: {e}") from e


_SIGNAL_LOOKUP_RETRY_DELAYS = (0.5, 1.5)  # backoff; signal-cli may answer a first lookup falsely
_SIGNAL_ERROR_DETAIL_MAX = 120


def _short_error(exc: BaseException) -> str:
    """Single-line, truncated signal-cli error text for logs and API warnings (numbers masked by the backend)."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:_SIGNAL_ERROR_DETAIL_MAX]


def _signal_registration_lookup(phone: str, *, force_refresh: bool = False) -> tuple[bool | None, str | None]:
    """Check if phone is registered with Signal: (status, error).

    status is True/False for a definite answer and None when the check failed or is unavailable; error then
    carries the reason. A negative answer is re-checked and cached only when all lookups agree; a transient
    failure is retried with backoff. Answers are cached for 24h (thread-safe).
    """
    now = time.monotonic()
    masked = mask_phone(phone)
    if not force_refresh:
        with _SIGNAL_CACHE_LOCK:
            cached = _SIGNAL_REGISTRATION_CACHE.get(phone)
        if cached is not None and (now - cached[1]) < _SIGNAL_CACHE_TTL:
            return cached[0], None

    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.signal_errors import SignalNotRegistered, SignalTimeout, SignalUnavailable

    try:
        backend = get_signal_backend()
        accounts = backend.list_accounts()
        if not accounts:
            _log.info("Signal check for %s skipped: no Signal account registered", masked)
            return None, None
        sender = accounts[0]
    except (SignalUnavailable, SignalTimeout) as exc:
        _log.warning("Signal check for %s failed: %s", masked, _short_error(exc))
        return None, _short_error(exc)

    last_error: str | None = None
    # An explicit re-check is one lookup per click (rate limited); the user repeats it when in doubt.
    attempts = 1 if force_refresh else len(_SIGNAL_LOOKUP_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        if attempt:
            time.sleep(_SIGNAL_LOOKUP_RETRY_DELAYS[attempt - 1])
        try:
            is_registered = backend.check_user_registered(phone, sender=sender)
        except (SignalUnavailable, SignalTimeout) as exc:
            last_error = _short_error(exc)
            _log.warning("Signal check for %s failed (attempt %d): %s", masked, attempt + 1, last_error)
            continue
        except SignalNotRegistered as exc:
            _log.warning("Signal check for %s failed: sender not registered (%s)", masked, _short_error(exc))
            return None, _short_error(exc)
        except Exception as exc:  # noqa: BLE001 - Signal-check boundary: any failure degrades to unknown registration
            _log.error("Signal check for %s failed: %s", masked, type(exc).__name__)
            return None, type(exc).__name__
        if is_registered:
            with _SIGNAL_CACHE_LOCK:
                if len(_SIGNAL_REGISTRATION_CACHE) >= _SIGNAL_CACHE_MAX_SIZE:
                    oldest_key = min(_SIGNAL_REGISTRATION_CACHE, key=lambda k: _SIGNAL_REGISTRATION_CACHE[k][1])
                    del _SIGNAL_REGISTRATION_CACHE[oldest_key]
                _SIGNAL_REGISTRATION_CACHE[phone] = (True, time.monotonic())
            _log.info("Signal check for %s: registered", masked)
            return True, None
        _log.info("Signal check for %s: reported not registered (attempt %d)", masked, attempt + 1)
        last_error = None
    if last_error is not None:
        return None, last_error
    # Cached only after every lookup agreed; the explicit re-check bypasses it.
    with _SIGNAL_CACHE_LOCK:
        _SIGNAL_REGISTRATION_CACHE[phone] = (False, time.monotonic())
    _log.warning("Signal check for %s: not registered after %d lookup(s)", masked, attempts)
    return False, None


def _signal_registration_status(phone: str, *, force_refresh: bool = False) -> bool | None:
    """Registration of phone: True, False, or None when the check failed or is unavailable."""
    return _signal_registration_lookup(phone, force_refresh=force_refresh)[0]


def _cached_signal_age(phone: str) -> tuple[bool, float] | None:
    """Return (value, age in seconds) of a cache entry, or None."""
    with _SIGNAL_CACHE_LOCK:
        entry = _SIGNAL_REGISTRATION_CACHE.get(phone)
    return None if entry is None else (entry[0], time.monotonic() - entry[1])


def _force_signal_registration(phone: str) -> bool | None:
    """Re-check one number against signal-cli, bypassing the cache.

    Concurrent calls for the same number share one lookup (single flight); at most
    ``_SIGNAL_FORCE_MAX_CONCURRENT`` lookups run at once and a number refreshed within
    ``_SIGNAL_FORCE_MIN_INTERVAL`` is answered from the cache. Raises HTTP 429 when busy.
    """
    with _SIGNAL_FLIGHT_LOCK:
        flight = _SIGNAL_FLIGHTS.get(phone)
        leader = flight is None
        if flight is None:
            flight = _SIGNAL_FLIGHTS[phone] = _SignalFlight()

    if not leader:
        if not flight.done.wait(_SIGNAL_FLIGHT_WAIT):
            raise _signal_busy()
        if flight.error is not None:
            raise flight.error
        return flight.result

    try:
        cached = _cached_signal_age(phone)
        if cached is not None and cached[1] < _SIGNAL_FORCE_MIN_INTERVAL:
            flight.result = cached[0]
        elif not _SIGNAL_FORCE_SLOTS.acquire(blocking=False):
            raise _signal_busy()
        else:
            try:
                flight.result = _signal_registration_status(phone, force_refresh=True)
            finally:
                _SIGNAL_FORCE_SLOTS.release()
        return flight.result
    except HTTPException as exc:
        flight.error = exc
        raise
    finally:
        with _SIGNAL_FLIGHT_LOCK:
            _SIGNAL_FLIGHTS.pop(phone, None)
        flight.done.set()


def _signal_busy() -> HTTPException:
    return HTTPException(
        status_code=429, detail="Signal check is busy. Try again shortly.", headers={"Retry-After": "5"}
    )


def _check_signal_registration(phone: str) -> str | None:
    """Warning text if phone is not (verifiably) registered with Signal, else None.

    A definite negative and a failed check are reported differently; a failure never reads as "not registered".
    """
    status_, error = _signal_registration_lookup(phone)
    if status_ is True or (status_ is None and error is None):
        return None
    if status_ is False:
        return (
            f"The provided phone number ({mask_phone(phone)}) is not registered with Signal. "
            "Signal notifications will fail."
        )
    return f"Could not verify whether {mask_phone(phone)} is registered with Signal ({error}). Try again later."


def _require_signal_account() -> None:
    """Reject enabling Signal while the sender account is missing or not registered (same status as /signal/status)."""
    from ..services.notifications.signal_backend import get_signal_backend
    from ..services.notifications.signal_errors import SignalTimeout, SignalUnavailable

    detail = "Signal account is not registered. Register it under Settings > Signal first."
    try:
        st = get_signal_backend().status()
    except (SignalUnavailable, SignalTimeout) as exc:
        raise HTTPException(status_code=409, detail=detail) from exc
    if not (st.reachable and st.registered):
        raise HTTPException(status_code=409, detail=detail)


def _row_to_public(row: Mapping[str, Any], targets: Sequence[Mapping[str, Any]] | None = None) -> RecipientPublic:
    return RecipientPublic(
        id=int(row["id"]),
        name=row["name"],
        phone=row["phone"] or None,
        email=row["email"] or None,
        is_enabled=bool(row["is_enabled"]),
        enable_for_all_targets=bool(row["enable_for_all_targets"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        is_user_linked=row["user_id"] is not None,
        targets=[TargetBrief(id=t["id"], name=t["name"]) for t in (targets or [])],
    )


@router.get("", response_model=list[RecipientPublic])
def list_recipients(conn=Depends(get_conn), _user=Depends(get_current_user)):
    """List all alert recipients."""
    rows = sqlite_db.list_recipients(conn)
    targets_by_recipient = sqlite_db.get_targets_by_recipient(conn)
    result: list[RecipientPublic] = []
    for r in rows:
        if r["user_id"] is None and not (r["phone"] or r["email"]):
            # A standalone legacy row without a usable channel must not break the whole list. A user's recipient
            # may legitimately have none and stays visible.
            _log.warning("Skipping recipient id=%s: stored contact data is invalid", r["id"])
            continue
        try:
            result.append(_row_to_public(r, targets_by_recipient.get(r["id"])))
        except ValidationError:
            _log.warning("Skipping recipient id=%s: stored contact data is invalid", r["id"])
    return result


@router.post("/verify-signal", dependencies=[Depends(rate_limit(RATE_LIMIT_HEAVY))])
def verify_signal_numbers(
    phones: _PhoneList,
    _conn=Depends(get_conn),
    _user=Depends(require_admin),
):
    """Verify which phone numbers are registered with Signal.

    Numbers are checked sequentially.
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
    phones_to_check: list[str] = []

    for phone in phones:
        if not phone:
            continue

        try:
            normalized = normalize_phone(phone)
        except ValueError:
            # Invalid phone format -> track separately
            invalid_phones.append(phone)
            continue

        if normalized not in phones_to_check:
            phones_to_check.append(normalized)

    # Sequential on purpose: signal-cli serializes on a file lock, so threads would only queue.
    # _signal_registration_status answers cached numbers without a backend call and maps failures to None.
    for normalized in phones_to_check:
        results[normalized] = _signal_registration_status(normalized)

    return {"results": results, "invalid": invalid_phones}


@router.post(
    "/{recipient_id}/recheck-signal",
    dependencies=[Depends(rate_limit(_SIGNAL_FORCE_RATE_LIMIT))],
)
def recheck_signal(recipient_id: int, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Re-check one recipient's Signal registration, bypassing the 24h cache once.

    The number is read from the stored recipient (never from the URL or body) so it
    stays out of access logs. ``registered`` is null when the check is unavailable.
    """
    row = sqlite_db.get_recipient(conn, recipient_id)
    if not row:
        raise HTTPException(status_code=404, detail="Recipient not found")
    if not row["phone"]:
        raise HTTPException(status_code=400, detail="Recipient has no Signal number")
    try:
        phone = normalize_phone(row["phone"])
    except ValueError:
        return {"registered": None}
    try:
        return {"registered": _force_signal_registration(phone)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - Signal boundary: report unknown, never the internal text
        _log.warning("Signal re-check for %s failed: %s", mask_phone(phone), type(exc).__name__)
        return {"registered": None}


def _raise_integrity_conflict(exc: sqlite3.IntegrityError) -> NoReturn:
    """Map a database constraint violation to its HTTP answer instead of letting it end as a 500.

    A unique-index violation is the duplicate-phone conflict of the pre-check; a CHECK violation means the
    submitted final state has no usable contact channel (the checks are the database side of the same rule).
    """
    message = str(exc)
    if "UNIQUE constraint failed" in message:
        raise HTTPException(status_code=409, detail="Phone number already exists") from exc
    if "CHECK constraint failed" in message:
        raise HTTPException(
            status_code=422,
            detail="A recipient needs a phone number or an email address, and only one with a channel can be enabled",
        ) from exc
    raise exc


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

    if not payload.phone and not payload.email:
        raise HTTPException(status_code=400, detail="At least one contact method (phone or email) is required")

    normalized_phone = None
    if payload.phone:
        normalized_phone = _normalize_phone_or_400(payload.phone)

        existing = sqlite_db.get_recipient_by_phone(conn, normalized_phone)
        if existing:
            raise HTTPException(status_code=409, detail="Phone number already exists")

        _require_signal_account()
        signal_warning = _check_signal_registration(normalized_phone)
        if signal_warning:
            warnings.append(signal_warning)

    try:
        new_id = sqlite_db.create_recipient(
            conn,
            name=payload.name,
            phone=normalized_phone,
            email=payload.email,
            is_enabled=payload.is_enabled,
            enable_for_all_targets=payload.enable_for_all_targets,
        )
    except sqlite_db.DuplicatePhoneError as exc:
        # The pre-check above is racy (signal lookup in between); the write itself re-checks atomically.
        raise HTTPException(status_code=409, detail="Phone number already exists") from exc
    except sqlite3.IntegrityError as exc:
        _raise_integrity_conflict(exc)
    row = sqlite_db.get_recipient(conn, new_id)
    recipient = _row_to_public(row, sqlite_db.get_recipient_targets(conn, new_id))
    return RecipientResponse(recipient=recipient, warnings=warnings)


@router.patch("/{recipient_id}", response_model=RecipientResponse)
def update_recipient(recipient_id: int, payload: RecipientUpdate, conn=Depends(get_conn), _user=Depends(require_admin)):
    """Update an alert recipient.

    The final state is validated against the row as read under the write lock: a parallel request that
    removed another channel in the meantime is seen, not overwritten with a stale decision.
    """
    warnings: list[str] = []
    update_data = payload.model_dump(exclude_unset=True)
    # Outside the write lock: an early 404 and the slow Signal lookup, which only produces a warning.
    known = sqlite_db.get_recipient(conn, recipient_id)
    if not known:
        raise HTTPException(status_code=404, detail="Recipient not found")
    normalized_phone = _normalize_phone_or_400(payload.phone) if update_data.get("phone") else None
    if normalized_phone is not None and normalized_phone != known["phone"]:
        # A phone already saved stays editable while the account is broken; only newly enabling Signal is refused.
        _require_signal_account()
        signal_warning = _check_signal_registration(normalized_phone)
        if signal_warning:
            warnings.append(signal_warning)
    try:
        with sqlite_db.transaction(conn, immediate=True):
            existing = sqlite_db.get_recipient(conn, recipient_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Recipient not found")
            clear_phone = False
            clear_email = False
            if "phone" in update_data:
                if normalized_phone is not None:
                    if normalized_phone != existing["phone"]:
                        dup = sqlite_db.get_recipient_by_phone(conn, normalized_phone)
                        if dup and dup["id"] != recipient_id:
                            raise HTTPException(status_code=409, detail="Phone number already exists")
                    update_data["phone"] = normalized_phone
                else:
                    # Phone is being cleared (null or empty string)
                    clear_phone = True
                    del update_data["phone"]
            if "email" in update_data and not payload.email:
                clear_email = True
                del update_data["email"]
            final_phone = None if clear_phone else update_data.get("phone", existing["phone"])
            final_email = None if clear_email else update_data.get("email", existing["email"])
            if not final_phone and not final_email:
                if existing["user_id"] is None:
                    raise HTTPException(
                        status_code=400, detail="At least one contact method (phone or email) is required"
                    )
                # An application user's recipient may have no channel, but then it must not stay enabled.
                if update_data.get("is_enabled", bool(existing["is_enabled"])):
                    raise HTTPException(
                        status_code=422,
                        detail="A recipient without phone number and email address cannot be enabled. "
                        "Switch it off or keep a channel.",
                    )
            if update_data or clear_phone or clear_email:
                sqlite_db.update_recipient(
                    conn, recipient_id, clear_phone=clear_phone, clear_email=clear_email, **update_data
                )
    except sqlite_db.DuplicatePhoneError as exc:
        raise HTTPException(status_code=409, detail="Phone number already exists") from exc
    except sqlite3.IntegrityError as exc:
        _raise_integrity_conflict(exc)
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

    if existing["user_id"] is not None:
        raise HTTPException(
            status_code=409,
            detail="This recipient belongs to an application user. Delete the user to remove it.",
        )
    try:
        sqlite_db.delete_recipient(conn, recipient_id)
    except sqlite_db.UserBoundRecipientError as exc:  # linked between the check above and the delete
        raise HTTPException(
            status_code=409,
            detail="This recipient belongs to an application user. Delete the user to remove it.",
        ) from exc
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
    if smtp_readiness(conn) is not SmtpReadiness.READY:
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
        RecipientTestCooldownError,
        SignalQueueLimitError,
        get_spooler,
    )

    recipient_id = int(row["id"])
    address = row["phone"] if channel == "signal" else row["email"]
    if not address:
        raise _TestRefusedError(400, f"This recipient has no {_TEST_CHANNEL_LABELS[channel]} address saved.")
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
            recipient_test_cooldown_seconds=RECIPIENT_TEST_COOLDOWN_SECONDS,
        )
    except RecipientTestCooldownError:
        raise _TestRefusedError(
            429, f"A {_TEST_CHANNEL_LABELS[channel]} test was just sent. Wait a moment before trying again."
        ) from None
    except SignalQueueLimitError:
        raise _TestRefusedError(429, "Signal test-message limit reached. Try again later.") from None
    except Exception as exc:  # noqa: BLE001 - queue boundary: report a generic failure, never the internal text
        _log.warning("Recipient test enqueue failed (recipient_id=%d): %s", recipient_id, type(exc).__name__)
        raise _TestRefusedError(503, "The notification queue is unavailable.") from None


@router.post(
    "/{recipient_id}/test",
    response_model=RecipientTestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit(RATE_LIMIT_HEAVY))],
)
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
    if not channels:
        raise HTTPException(status_code=400, detail="Recipient has no configured channels")
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


@router.get(
    "/{recipient_id}/test/{queue_id}",
    response_model=RecipientTestStatus,
    dependencies=[Depends(rate_limit("60/minute"))],
)
def get_recipient_test_status(
    recipient_id: int,
    queue_id: int,
    conn=Depends(get_conn),
    _admin=Depends(require_admin),
):
    """Report the delivery state of a queued test message (admin only).

    A test is never retried: after the first failed attempt it is closed as failed, so the
    admin gets one clear result instead of a message that arrives hours later.
    """
    from ..services.notifications.spooler import TEST_FINAL_SQL, UNCERTAIN_MARKER

    item = conn.execute(
        f"""
        SELECT channel_type, status, ({TEST_FINAL_SQL}) AS final, last_error FROM notification_queue
        WHERE id = ? AND recipient_id = ? AND notification_type = 'test'
        """,  # noqa: S608 - constant predicate, no user input
        (queue_id, recipient_id),
    ).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Test message not found")
    channel = item["channel_type"]
    label = _TEST_CHANNEL_LABELS.get(channel, channel)
    if item["status"] == "sent":
        return RecipientTestStatus(channel=channel, state="sent", message=f"{label} test message sent.")
    if item["status"] == "failed" or item["final"]:
        if UNCERTAIN_MARKER in (item["last_error"] or ""):
            message = f"{label} delivery could not be confirmed. Check the recipient before retrying."
        else:
            message = (
                f"{label} delivery failed. Check the {_SERVICE_LABELS.get(channel, label)} settings and server log."
            )
        return RecipientTestStatus(channel=channel, state="failed", message=message)
    return RecipientTestStatus(channel=channel, state="pending", message=f"Waiting for {label} delivery…")
