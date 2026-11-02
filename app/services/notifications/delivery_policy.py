#!/usr/bin/env python3
#
# app/services/notifications/delivery_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Delivery policy of the notification spooler: failure classes, retry timing, pacing, cooldowns.

One place decides how a failed send is treated, so SMTP and Signal share the same rules:

- TRANSIENT: network or provider hiccup. Retried with exponential backoff until the message
  validity ends; it never consumes the small budget of non-transient failures.
- RATE_LIMITED: the provider asked us to slow down. The whole channel (Signal: sender) pauses
  until the cooldown ends; waiting is not a failure and consumes no budget.
- LIMITED: not fixable by the network coming back (configuration, TLS policy, unknown errors).
  Retried only a few times, so an administrator can still repair it, then finalized.
- PERMANENT: the message itself can never be delivered (invalid recipient, rejected payload).

A TLS or certificate problem is classified, never worked around: nothing is ever sent unencrypted
because a retry failed.
"""

from __future__ import annotations

import random
import smtplib
import ssl
from dataclasses import dataclass
from enum import StrEnum

from .signal_errors import (
    SignalNotInstalled,
    SignalNotRegistered,
    SignalRateLimited,
    SignalRecipientInvalid,
    SignalSendFailed,
    SignalTimeout,
    SignalUnavailable,
)
from .smtp_transport import (
    SMTPAcceptanceUnknown,
    SMTPConfigurationError,
    SMTPEncryptionError,
    SMTPEnvelopeError,
    SMTPNotAccepted,
    SMTPPartialRefusal,
)

# ─── Retry timing ───────────────────────────────────────────────────────────────

BASE_DELAY_SECONDS = 60
# 15 min keeps the first delivery after an outage close to the recovery, at ~4 attempts/hour.
MAX_DELAY_SECONDS = 900
JITTER_FACTOR = 0.25

# A queued message stays deliverable for 24 h: long enough for a full-day provider or uplink
# outage, short enough that nobody reads a day-old "target down" as current.
MESSAGE_VALIDITY_SECONDS = 24 * 3600

# Failures that more waiting cannot fix (configuration, unknown errors): retries after the first.
# With the backoff above this leaves an administrator about 30 minutes to repair the setting.
MAX_RETRIES = 5

# ─── Pacing and cooldown ────────────────────────────────────────────────────────

# A cooldown given by the provider is honoured up to this cap.
MAX_COOLDOWN_SECONDS = 24 * 3600
# SMTP has no Retry-After: a conservative pause after a throttling answer (421/450/451/452).
SMTP_RATE_LIMIT_COOLDOWN_SECONDS = 900
SIGNAL_RATE_LIMIT_FALLBACK_SECONDS = 3600
# A channel that must still wait longer than this is skipped for the run instead of blocking it.
MAX_PACING_WAIT_SECONDS = 3.0


@dataclass(frozen=True)
class ChannelPolicy:
    """Send pacing of one channel: minimum gap between send attempts and attempts per spooler run."""

    min_interval_seconds: float
    batch_limit: int


# Email: 2.5 s keeps below 30 mails/min, the strictest common submission limit (Microsoft 365).
# The batch of 4 bounds one run to 7.5 s of pacing, so the scheduler tick is not held up.
# Signal: one attempt per 10 s, the pace the 10 s scheduler tick with one job per run has always
# produced, now enforced across runs and restarts.
CHANNEL_POLICIES: dict[str, ChannelPolicy] = {
    "email": ChannelPolicy(min_interval_seconds=2.5, batch_limit=4),
    "signal": ChannelPolicy(min_interval_seconds=10.0, batch_limit=1),
}


def channel_key(channel: str, sender: str | None = None) -> str:
    """State key of a send path: the channel, plus the sending account for Signal."""
    return f"signal:{(sender or '').strip()}" if channel == "signal" else channel


def channel_of_key(key: str) -> str:
    return key.partition(":")[0]


def backoff_delay_seconds(attempt: int) -> float:
    """Delay before retry ``attempt`` (0-based): 1, 2, 4, 8, 15, 15 ... min, ±25 % jitter, never below the base."""
    delay = min(BASE_DELAY_SECONDS * (2 ** min(attempt, 16)), MAX_DELAY_SECONDS)
    jitter = delay * JITTER_FACTOR * (random.random() * 2 - 1)  # noqa: S311 - retry jitter, not security-sensitive
    return max(BASE_DELAY_SECONDS, delay + jitter)


# ─── Failure classification ─────────────────────────────────────────────────────


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    LIMITED = "limited"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class Failure:
    """Classified send failure. ``uncertain``: the provider may already have accepted the message."""

    kind: FailureKind
    reason: str
    cooldown_seconds: int | None = None
    uncertain: bool = False


_RATE_WORDS = ("rate", "throttl", "too many", "limit", "quota", "slow down", "exceed")
_SMTP_THROTTLE_CODES = frozenset({421, 450, 451, 452})


def _reason(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _smtp_response_failure(code: int, text: str, reason: str) -> Failure:
    if code >= 500:
        return Failure(FailureKind.PERMANENT, reason)
    if code in _SMTP_THROTTLE_CODES and any(word in text.lower() for word in _RATE_WORDS):
        return Failure(FailureKind.RATE_LIMITED, reason, SMTP_RATE_LIMIT_COOLDOWN_SECONDS)
    return Failure(FailureKind.TRANSIENT, reason)


def classify_failure(exc: BaseException) -> Failure:
    """Map one send exception to its failure class (the single place that decides this)."""
    reason = _reason(exc)

    if isinstance(exc, SignalRateLimited):
        seconds = exc.retry_after_seconds if exc.retry_after_seconds > 0 else SIGNAL_RATE_LIMIT_FALLBACK_SECONDS
        return Failure(FailureKind.RATE_LIMITED, reason, min(seconds, MAX_COOLDOWN_SECONDS))
    if isinstance(exc, SignalRecipientInvalid | SignalNotRegistered):
        return Failure(FailureKind.PERMANENT, reason)
    if isinstance(exc, SignalNotInstalled):
        return Failure(FailureKind.LIMITED, reason)
    if isinstance(exc, SignalTimeout):
        return Failure(FailureKind.TRANSIENT, reason, uncertain=True)
    if isinstance(exc, SignalUnavailable | SignalSendFailed):
        return Failure(FailureKind.TRANSIENT, reason)

    # SMTP errors subclass OSError, so they are checked before the generic network branch.
    if isinstance(exc, SMTPConfigurationError | SMTPEncryptionError | smtplib.SMTPNotSupportedError):
        return Failure(FailureKind.LIMITED, reason)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return Failure(FailureKind.LIMITED, reason)
    if isinstance(exc, SMTPPartialRefusal | SMTPEnvelopeError):
        # Partial acceptance (a retry would duplicate) or a malformed envelope (a retry cannot fix it): final.
        return Failure(FailureKind.PERMANENT, reason)
    if isinstance(exc, SMTPAcceptanceUnknown):
        return Failure(FailureKind.TRANSIENT, reason, uncertain=True)
    if isinstance(exc, SMTPNotAccepted):
        return Failure(FailureKind.TRANSIENT, reason)
    # A server that refuses the session itself (e.g. 554 at connect or HELO) is a setup/provider problem,
    # not a verdict on this message.
    if isinstance(exc, smtplib.SMTPConnectError | smtplib.SMTPHeloError) and exc.smtp_code >= 500:
        return Failure(FailureKind.LIMITED, reason)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _msg in exc.recipients.values()]
        return Failure(FailureKind.PERMANENT if codes and min(codes) >= 500 else FailureKind.TRANSIENT, reason)
    if isinstance(exc, smtplib.SMTPSenderRefused) and exc.smtp_code >= 500:
        return Failure(FailureKind.LIMITED, reason)
    if isinstance(exc, smtplib.SMTPResponseException):
        text = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        return _smtp_response_failure(exc.smtp_code, text, reason)
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return Failure(FailureKind.TRANSIENT, reason, uncertain=True)
    if isinstance(exc, smtplib.SMTPException):
        return Failure(FailureKind.TRANSIENT, reason)

    # ssl.SSLError is an OSError too: a certificate or protocol problem needs a fix, not a retry storm.
    if isinstance(exc, ssl.SSLError):
        return Failure(FailureKind.LIMITED, reason)
    if isinstance(exc, TimeoutError | ConnectionResetError | BrokenPipeError):
        return Failure(FailureKind.TRANSIENT, reason, uncertain=True)
    if isinstance(exc, OSError):
        return Failure(FailureKind.TRANSIENT, reason)
    return Failure(FailureKind.LIMITED, reason)
