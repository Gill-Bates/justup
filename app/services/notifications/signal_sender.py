#!/usr/bin/env python3
#
# app/services/notifications/signal_sender.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Signal notification sender for NotificationDispatcher.

Wraps the signal-cli backend to implement the NotificationSender protocol.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable

from ...db import sqlite as sqlite_db
from ...utils.constants import SettingKeys
from .dispatcher import AlertPayload, SendResult, RecipientInfo
from .signal_backend import get_signal_backend
from .signal_errors import (
	SignalNotRegistered,
	SignalRateLimited,
	SignalRecipientInvalid,
	SignalSendFailed,
	SignalTimeout,
	SignalUnavailable,
)

_log = logging.getLogger(__name__)

# Channel type constant
_CHANNEL_TYPE = "signal"


def _mask_phone(number: str) -> str:
	"""Mask phone number for GDPR-compliant logging."""
	if not number or len(number) < 6:
		return "***"
	return number[:3] + "****" + number[-2:]


class SignalSender:
	"""
	Signal notification sender for NotificationDispatcher.
	
	Implements the NotificationSender protocol for use with NotificationDispatcher.
	Wraps the blocking signal-cli backend via asyncio.to_thread().
	"""
	
	def __init__(self, db_conn_factory: Callable[[], sqlite3.Connection]):
		"""DB connection factory used to check if Signal is enabled."""
		self.db_conn_factory = db_conn_factory
		self._backend = get_signal_backend()
	
	@contextmanager
	def _db_session(self):
		"""Open a DB connection and guarantee close on exit.
		
		Yields:
			sqlite3.Connection that will be committed on success,
			rolled back on exception, and always closed.
			
		Note:
			sqlite3.Connection context manager only commits/rollbacks,
			it does NOT close the connection. Explicit close() prevents
			file descriptor leaks under high load.
		"""
		conn = self.db_conn_factory()
		try:
			yield conn
			conn.commit()
		except Exception:
			conn.rollback()
			raise
		finally:
			conn.close()
	
	def _read_signal_settings(self) -> tuple[bool, str | None]:
		"""Read Signal settings from DB (runs in thread pool).
		
		Returns:
			Tuple of (signal_enabled, rate_limit_until_iso)
		"""
		with self._db_session() as conn:
			signal_enabled = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_CHANNEL_ENABLED, False)
			rate_limit_until = sqlite_db.get_setting(conn, SettingKeys.SIGNAL_RATE_LIMIT_UNTIL, None)
			return signal_enabled, rate_limit_until
	
	def _persist_rate_limit(self, cooldown_until: datetime) -> None:
		"""Persist rate limit to DB (runs in thread pool).
		
		Args:
			cooldown_until: UTC datetime when rate limit expires
		"""
		with self._db_session() as conn:
			sqlite_db.set_setting(conn, SettingKeys.SIGNAL_RATE_LIMIT_UNTIL, cooldown_until.isoformat())
	
	async def send(self, recipient: RecipientInfo, alert: AlertPayload) -> SendResult:
		"""Send alert via Signal.

		Args:
			recipient: RecipientInfo with phone number
			alert: AlertPayload with alert details
			
		Returns:
			SendResult with success status and error details
		"""
		phone = recipient.phone
		
		# No phone configured for this recipient
		if not phone:
			return SendResult(
				success=False,
				channel_type=_CHANNEL_TYPE,
				channel_address="<no-phone>",
				skipped=True,
			)
		
		# Respect disabled recipients
		if not recipient.is_enabled:
			return SendResult(
				success=False,
				channel_type=_CHANNEL_TYPE,
				channel_address=phone,
				skipped=True,
			)
		
		# Check if Signal is enabled and not globally rate-limited
		try:
			# Run blocking DB I/O in thread pool to avoid blocking event loop
			signal_enabled, rate_limit_until = await asyncio.to_thread(
				self._read_signal_settings
			)
		except Exception as e:
			_log.error("Failed to read Signal settings: %s", e)
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=str(e))
		
		if not signal_enabled:
			_log.debug("Signal disabled, skipping message to %s", phone)
			return SendResult(
				success=False,
				channel_type=_CHANNEL_TYPE,
				channel_address=phone,
				skipped=True,
			)
		
		# Check global rate limit (persisted across restarts)
		if rate_limit_until:
			try:
				limit_dt = datetime.fromisoformat(rate_limit_until)
				now = datetime.now(timezone.utc)
				if now < limit_dt:
					remaining = int((limit_dt - now).total_seconds())
					_log.debug(
						"Signal globally rate-limited, skipping send to %s (%ds remaining)",
						_mask_phone(phone),
						remaining,
					)
					return SendResult(
						success=False,
						channel_type=_CHANNEL_TYPE,
						channel_address=phone,
						skipped=True,
						error=f"Signal rate-limited (retry in {remaining}s)",
					)
			except (ValueError, TypeError):
				pass  # Invalid timestamp, ignore
		
		# Get sender number from backend (validates against signal-cli accounts)
		try:
			# Backend's get_valid_sender validates against signal-cli accounts
			# and falls back to first available account if needed
			sender = await asyncio.to_thread(self._backend.get_valid_sender, None)
		except SignalNotRegistered:
			_log.warning("No Signal sender account registered, cannot send to %s", _mask_phone(phone))
			return SendResult(
				success=False,
				channel_type=_CHANNEL_TYPE,
				channel_address=phone,
				error="No sender account registered",
			)
		
		# Render message from alert
		message = alert.render_text()
		
		try:
			# Run synchronous signal-cli in thread pool to avoid blocking event loop
			await asyncio.to_thread(self._backend.send_message, phone, message, sender)
			_log.info("Signal message sent to %s", _mask_phone(phone))
			return SendResult(success=True, channel_type=_CHANNEL_TYPE, channel_address=phone)
			
		except SignalNotRegistered:
			error_msg = "Signal not registered"
			_log.warning("Signal to %s failed: %s", _mask_phone(phone), error_msg)
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=error_msg)
			
		except SignalRateLimited as e:
			# Set global rate limit to prevent all Signal sends for the cooldown period
			cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after_seconds)
			try:
				# Run blocking DB write in thread pool to avoid blocking event loop
				await asyncio.to_thread(self._persist_rate_limit, cooldown_until)
				_log.warning(
					"Signal rate-limited globally until %s (%ds cooldown)",
					cooldown_until.isoformat(),
					e.retry_after_seconds,
				)
			except Exception as db_err:
				_log.error("Failed to persist Signal rate limit: %s", db_err)
			
			error_msg = f"Rate limited (retry after {e.retry_after_seconds}s)"
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=error_msg)
			
		except SignalRecipientInvalid as e:
			error_msg = f"Invalid recipient: {e}"
			_log.warning("Signal to %s failed: %s", _mask_phone(phone), error_msg)
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=error_msg)
			
		except SignalUnavailable as e:
			error_msg = f"Signal unavailable: {e}"
			_log.error("Signal to %s failed: %s", _mask_phone(phone), error_msg)
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=error_msg)
			
		except SignalTimeout:
			error_msg = "Signal timeout"
			_log.error("Signal to %s failed: %s", _mask_phone(phone), error_msg)
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=error_msg)
			
		except SignalSendFailed as e:
			error_msg = f"Send failed: {e}"
			_log.error("Signal to %s failed: %s", _mask_phone(phone), error_msg)
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=error_msg)
			
		except Exception as e:
			error_msg = f"Unexpected error: {e}"
			_log.error("Signal to %s failed: %s", _mask_phone(phone), error_msg)
			return SendResult(success=False, channel_type=_CHANNEL_TYPE, channel_address=phone, error=error_msg)
