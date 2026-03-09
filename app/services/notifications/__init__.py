#!/usr/bin/env python3
#
# app/services/notifications/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Notification services - spooler-based alert distribution.

The notification system uses a spooler-first pattern:
- NotificationDispatcher: Resolves target → recipients, pre-renders messages, enqueues to spooler
- NotificationSpooler: Persistent SQLite queue — the single exit point for ALL notifications
- Signal/Email backends: Used only by the spooler's send callbacks (never called directly)

Architecture:
    ALL notifications go through the spooler. Nothing leaves the system
    without being enqueued first. This ensures visibility in the Spooler UI,
    automatic retry handling, and consistent rate limiting.
"""

from .signal_backend import SignalBackend, get_signal_backend
from .signal_types import SignalStatus
from .signal_errors import (
    SignalError,
    SignalUnavailable,
    SignalNotRegistered,
    SignalTimeout,
    SignalRecipientInvalid,
    SignalSendFailed,
)
from .dispatcher import NotificationDispatcher, AlertPayload, SendResult, RecipientInfo, DispatchResult
from .spooler import NotificationSpooler, NotificationType, ChannelType, get_spooler

__all__ = [
    # Signal backend (used by spooler callbacks only)
    "SignalBackend",
    "get_signal_backend",
    "SignalStatus",
    "SignalError",
    "SignalUnavailable",
    "SignalNotRegistered",
    "SignalTimeout",
    "SignalRecipientInvalid",
    "SignalSendFailed",
    # Dispatcher (resolves recipients, enqueues to spooler)
    "NotificationDispatcher",
    "AlertPayload",
    "SendResult",
    "RecipientInfo",
    "DispatchResult",
    # Spooler (single exit point for all notifications)
    "NotificationSpooler",
    "NotificationType",
    "ChannelType",
    "get_spooler",
]
