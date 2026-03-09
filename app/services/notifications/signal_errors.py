#!/usr/bin/env python3
#
# app/services/notifications/signal_errors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Signal-specific exception classes.

These exceptions are raised by SignalBackend implementations
and mapped to HTTP status codes in the API layer:

    SignalUnavailable -> 503 Service Unavailable
    SignalNotRegistered -> 409 Conflict
    SignalTimeout -> 503 Service Unavailable
    SignalRecipientInvalid -> 400 Bad Request
"""

from __future__ import annotations

__all__ = [
    "SignalError",
    "SignalUnavailable",
    "SignalNotRegistered",
    "SignalTimeout",
    "SignalRateLimited",
    "SignalRecipientInvalid",
    "SignalSendFailed",
]


class SignalError(Exception):
    """Base exception for all Signal-related errors."""
    pass


class SignalUnavailable(SignalError):
    """signal-cli binary not found or not executable."""
    pass


class SignalNotRegistered(SignalError):
    """No Signal account is registered/linked."""
    pass


class SignalTimeout(SignalError):
    """signal-cli subprocess timed out."""
    pass


class SignalRateLimited(SignalError):
    """
    Signal rejected the message due to rate limiting.

    This is a temporary condition and requires a long cooldown.
    """
    retry_after_seconds: int = 3600  # 1 hour minimum


class SignalRecipientInvalid(SignalError):
    """Invalid recipient phone number."""
    pass


class SignalSendFailed(SignalError):
    """Message sending failed."""
    pass
