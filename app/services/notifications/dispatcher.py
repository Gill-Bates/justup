#!/usr/bin/env python3
#
# app/services/notifications/dispatcher.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Notification Dispatcher - Alert Distribution Logic.

This module implements the alert dispatch model:
    Alert → Target → Recipients → Spooler (enqueue for delivery)

Key responsibilities:
- Resolve target → recipients mapping
- Pre-render messages (email HTML, Signal text) at dispatch time
- Enqueue each notification into the spooler (single delivery path)
- Error isolation per recipient
- Logging & telemetry

Architecture:
    ALL notifications go through the spooler. The dispatcher never sends
    directly. This ensures consistent visibility in the Spooler UI and
    automatic retry handling for transient failures.

Note: Recipients are NOT users. Users can log in, Recipients receive alerts.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ...db import sqlite as sqlite_db
from ...utils.constants import SettingKeys

_log = logging.getLogger(__name__)


# ─── Data Structures ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AlertPayload:
	"""Alert data to be sent via notification channels."""
	alert_id: str
	target_id: int
	target_name: str
	failed_service: str
	status: str  # 'open', 'resolved'
	started_at: datetime
	ended_at: datetime | None = None
	duration_seconds: int | None = None
	
	@property
	def is_resolved(self) -> bool:
		return self.status == 'resolved'
	
	@property
	def message_subject(self) -> str:
		"""Short subject line for notifications."""
		if self.is_resolved:
			return f"✅ {self.target_name} - {self.failed_service} RESOLVED"
		return f"🚨 {self.target_name} - {self.failed_service} DOWN"
	
	@property
	def message_body(self) -> str:
		"""Detailed message body."""
		lines = [
			f"Target: {self.target_name}",
			f"Service: {self.failed_service}",
			f"Status: {self.status.upper()}",
			f"Started: {self.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
		]
		
		if self.is_resolved and self.ended_at:
			lines.append(f"Ended: {self.ended_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
			if self.duration_seconds:
				minutes = self.duration_seconds // 60
				seconds = self.duration_seconds % 60
				lines.append(f"Duration: {minutes}m {seconds}s")
		
		return "\n".join(lines)


@dataclass
class SendResult:
	"""Result of sending a notification to a channel."""
	success: bool
	channel_type: str
	channel_address: str
	error: str | None = None
	skipped: bool = False
	
	def __str__(self) -> str:
		if self.success:
			return f"{self.channel_type}:{self.channel_address} ✓"
		return f"{self.channel_type}:{self.channel_address} ✗ ({self.error})"


@dataclass
class DispatchResult:
	"""Result of dispatching an alert to all channels."""
	alert_id: str
	target_id: int
	users_notified: int
	channels_attempted: int
	channels_succeeded: int
	send_results: list[SendResult]
	
	@property
	def success_rate(self) -> float:
		"""Percentage of successful sends."""
		if self.channels_attempted == 0:
			return 0.0
		return (self.channels_succeeded / self.channels_attempted) * 100
	
	@property
	def all_succeeded(self) -> bool:
		return self.channels_attempted > 0 and self.channels_succeeded == self.channels_attempted
	
	@property
	def any_succeeded(self) -> bool:
		return self.channels_succeeded > 0


@dataclass
class RecipientInfo:
	"""Internal representation of a recipient for dispatching.
	
	Recipients are notification targets (phone/email), NOT login users.
	"""
	id: int
	name: str
	phone: str | None  # E.164 format for Signal
	email: str | None
	is_enabled: bool


# ─── Dispatcher ─────────────────────────────────────────────────────────────────


class NotificationDispatcher:
	"""
	Central dispatcher for alert notifications.
	
	Resolves recipients for a target and enqueues all notifications
	into the spooler. The spooler is the ONLY component that actually sends.
	
	Note: Recipients are NOT users. Users can log in, Recipients receive alerts.
	"""
	
	def __init__(
		self,
		conn: sqlite3.Connection,
		db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
	):
		"""Initialize dispatcher.
		
		Args:
		    conn: SQLite connection for recipient lookups
		    db_conn_factory: Factory for spooler DB connections
		"""
		self.conn = conn
		self._db_conn_factory = db_conn_factory
	
	def _render_signal_message(self, alert: AlertPayload) -> str:
		"""Render Signal message from alert using configured template.
		
		Returns:
			Rendered Signal message text
		"""
		from .signal_template import DEFAULT_SIGNAL_TEMPLATE_DOWN, DEFAULT_SIGNAL_TEMPLATE_UP
		from .email_template import render_template, build_alert_context
		
		# Choose template based on alert status
		if alert.is_resolved:
			template = sqlite_db.get_setting(
				self.conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_UP, None
			) or DEFAULT_SIGNAL_TEMPLATE_UP
		else:
			template = sqlite_db.get_setting(
				self.conn, SettingKeys.SIGNAL_MESSAGE_TEMPLATE_DOWN, None
			) or DEFAULT_SIGNAL_TEMPLATE_DOWN
		
		# Build context (same as email, shared render_template)
		started_at_str = alert.started_at.isoformat() if alert.started_at else None
		resolved_at_str = alert.ended_at.isoformat() if alert.ended_at else None
		
		target_row = self.conn.execute(
			"SELECT name, host, group_name FROM targets WHERE id = ?",
			(alert.target_id,)
		).fetchone()
		
		target_data = {
			"name": target_row["name"] if target_row else alert.target_name,
			"host": target_row["host"] if target_row else "N/A",
			"group": target_row["group_name"] if target_row else "Default",
			"check_type": alert.failed_service or "N/A",
		}
		
		system_url = sqlite_db.get_setting(
			self.conn, SettingKeys.SYSTEM_BASE_URL, None
		) or "#"
		
		alert_data = {
			"status": "RESOLVED" if alert.is_resolved else "OPEN",
			"started_at": started_at_str,
			"resolved_at": resolved_at_str,
			"reason": alert.failed_service or "Unknown",
			"downtime_minutes": (alert.duration_seconds // 60) if alert.duration_seconds else 0,
			"system_url": system_url,
		}
		
		context = build_alert_context(alert_data, target_data)
		return render_template(template, context)
	
	def _render_email_content(self, alert: AlertPayload) -> tuple[str, str]:
		"""Render email subject and HTML body from alert using configured template.
		
		Returns:
			Tuple of (subject, html_body)
		"""
		from .email_template import render_template, build_alert_context
		from .email_template import DEFAULT_SUBJECT, DEFAULT_HTML_TEMPLATE
		
		# Load templates
		subject_template = sqlite_db.get_setting(
			self.conn, "smtp_mail_template_subject", None
		) or DEFAULT_SUBJECT
		html_template = sqlite_db.get_setting(
			self.conn, "smtp_mail_template_html", None
		) or DEFAULT_HTML_TEMPLATE
		
		# Build context
		started_at_str = alert.started_at.isoformat() if alert.started_at else None
		resolved_at_str = alert.ended_at.isoformat() if alert.ended_at else None
		
		# Load target info for context
		old_factory = self.conn.row_factory
		self.conn.row_factory = sqlite3.Row
		try:
			target_row = self.conn.execute(
				"""
				SELECT t.name, t.host, t.group_name, t.sla_enabled,
				       t.enable_ping, t.enable_http_check, t.enable_tcp_connect,
				       ts.uptime_percent
				FROM targets t
				LEFT JOIN target_status ts ON ts.target_id = t.id
				WHERE t.id = ?
				""",
				(alert.target_id,)
			).fetchone()
		finally:
			self.conn.row_factory = old_factory
		
		if target_row:
			target_name = target_row["name"] or alert.target_name
			target_host = target_row["host"] or "N/A"
			group_name = target_row["group_name"] or "Default"
			sla_enabled = bool(target_row["sla_enabled"])
			
			if str(alert.failed_service or "").upper().startswith("CERT"):
				check_type = "CERT"
			else:
				enabled_checks = []
				if target_row["enable_http_check"]:
					enabled_checks.append("HTTP")
				if target_row["enable_ping"]:
					enabled_checks.append("PING")
				if target_row["enable_tcp_connect"]:
					enabled_checks.append("TCP")
				check_type = " + ".join(enabled_checks) if enabled_checks else "N/A"
		else:
			target_name = alert.target_name
			target_host = "N/A"
			group_name = "Default"
			check_type = "N/A"
			sla_enabled = False
		
		target_data = {
			"name": target_name,
			"host": target_host,
			"group": group_name,
			"check_type": check_type,
		}
		
		# SLA data
		sla_threshold_float = None
		sla_actual = None
		if sla_enabled:
			sla_threshold = sqlite_db.get_setting(
				self.conn, SettingKeys.SLA_UPTIME_THRESHOLD, "99.9"
			)
			try:
				sla_threshold_float = float(sla_threshold)
			except (ValueError, TypeError):
				sla_threshold_float = 99.9
			if target_row and target_row["uptime_percent"] is not None:
				sla_actual = target_row["uptime_percent"]
		
		system_url = sqlite_db.get_setting(
			self.conn, SettingKeys.SYSTEM_BASE_URL, None
		) or "#"
		
		alert_data = {
			"status": "RESOLVED" if alert.is_resolved else "OPEN",
			"started_at": started_at_str,
			"resolved_at": resolved_at_str,
			"reason": alert.failed_service or "Unknown failure",
			"downtime_minutes": (alert.duration_seconds // 60) if alert.duration_seconds else 0,
			"sla_threshold": sla_threshold_float,
			"sla_actual": sla_actual,
			"system_url": system_url,
		}
		
		context = build_alert_context(alert_data, target_data)
		subject = render_template(subject_template, context)
		html_body = render_template(html_template, context)
		return subject, html_body
	
	def dispatch_alert(self, alert: AlertPayload) -> DispatchResult:
		"""
		Dispatch an alert by enqueuing notifications for all recipients.
		
		All notifications go through the spooler — nothing is sent directly.
		
		Flow:
		    1. Get recipients for target
		    2. Pre-render messages (Signal text, Email HTML)
		    3. Enqueue each recipient+channel into the spooler
		    4. Return dispatch result
		"""
		from .spooler import get_spooler, NotificationType, ChannelType
		
		_log.info(
			"Dispatching alert %s (target=%d, status=%s)",
			alert.alert_id,
			alert.target_id,
			alert.status,
		)
		
		# Step 1: Get recipients for this target
		recipient_rows = sqlite_db.get_target_recipients(self.conn, alert.target_id)
		
		if not recipient_rows:
			_log.warning("No recipients configured for target %d - no alerts sent", alert.target_id)
			return DispatchResult(
				alert_id=alert.alert_id,
				target_id=alert.target_id,
				users_notified=0,
				channels_attempted=0,
				channels_succeeded=0,
				send_results=[],
			)
		
		# Build RecipientInfo list
		recipients: list[RecipientInfo] = []
		for row in recipient_rows:
			recipients.append(RecipientInfo(
				id=row["id"],
				name=row["name"],
				phone=row["phone"],
				email=row["email"],
				is_enabled=bool(row["is_enabled"]) if row["is_enabled"] is not None else False,
			))
		
		# Step 2: Pre-render messages (once per alert, shared across recipients)
		signal_message = None
		signal_sender = None
		email_subject = None
		email_html = None
		
		# Check if Signal is enabled and get sender
		signal_enabled = sqlite_db.get_setting(
			self.conn, SettingKeys.SIGNAL_CHANNEL_ENABLED, False
		)
		has_signal_recipients = any(r.phone and r.is_enabled for r in recipients)
		
		if signal_enabled and has_signal_recipients:
			try:
				signal_message = self._render_signal_message(alert)
				signal_sender_number = sqlite_db.get_setting(
					self.conn, SettingKeys.SIGNAL_API_SENDER_NUMBER, None
				)
				if signal_sender_number:
					signal_sender = signal_sender_number
			except Exception as e:
				_log.error("Failed to render Signal message: %s", e)
		
		# Check if SMTP is enabled
		smtp_enabled = sqlite_db.get_setting(
			self.conn, SettingKeys.SMTP_ENABLED, False
		)
		has_email_recipients = any(r.email and r.is_enabled for r in recipients)
		
		if smtp_enabled and has_email_recipients:
			try:
				email_subject, email_html = self._render_email_content(alert)
			except Exception as e:
				_log.error("Failed to render email content: %s", e)
		
		# Step 3: Enqueue into spooler
		spooler = get_spooler(self._db_conn_factory)
		send_results: list[SendResult] = []
		recipients_notified = 0
		
		for recipient in recipients:
			if not recipient.is_enabled:
				_log.debug("Recipient %s is disabled, skipping", recipient.name)
				continue
			
			recipient_had_channel = False
			
			# Enqueue Signal notification
			if recipient.phone and signal_message and signal_sender:
				recipient_had_channel = True
				try:
					spooler.enqueue(
						notification_type=NotificationType.ALERT,
						channel_type=ChannelType.SIGNAL,
						recipient_id=recipient.id,
						recipient_name=recipient.name,
						recipient_address=recipient.phone,
						payload={
							"message": signal_message,
							"sender": signal_sender,
							"alert_id": alert.alert_id,
							"target_id": alert.target_id,
						},
						immediate=True,
						target_id=alert.target_id,
					)
					send_results.append(SendResult(
						success=True,
						channel_type="signal",
						channel_address=recipient.phone,
					))
					_log.info("Queued Signal alert for %s", recipient.name)
				except Exception as e:
					_log.error("Failed to enqueue Signal for %s: %s", recipient.name, e)
					send_results.append(SendResult(
						success=False,
						channel_type="signal",
						channel_address=recipient.phone,
						error=str(e),
					))
			
			# Enqueue Email notification
			if recipient.email and email_subject and email_html:
				recipient_had_channel = True
				try:
					spooler.enqueue(
						notification_type=NotificationType.ALERT,
						channel_type=ChannelType.EMAIL,
						recipient_id=recipient.id,
						recipient_name=recipient.name,
						recipient_address=recipient.email,
						payload={
							"subject": email_subject,
							"text_body": "",
							"html_body": email_html,
							"alert_id": alert.alert_id,
							"target_id": alert.target_id,
						},
						immediate=True,
						target_id=alert.target_id,
					)
					send_results.append(SendResult(
						success=True,
						channel_type="email",
						channel_address=recipient.email,
					))
					_log.info("Queued email alert for %s", recipient.name)
				except Exception as e:
					_log.error("Failed to enqueue email for %s: %s", recipient.name, e)
					send_results.append(SendResult(
						success=False,
						channel_type="email",
						channel_address=recipient.email,
						error=str(e),
					))
			
			if recipient_had_channel:
				recipients_notified += 1
			elif not recipient.phone and not recipient.email:
				_log.warning("Recipient %s has no contact methods configured", recipient.name)
		
		# Step 4: Aggregate results
		channels_attempted = sum(1 for r in send_results if not r.skipped)
		channels_succeeded = sum(1 for r in send_results if r.success and not r.skipped)
		
		result = DispatchResult(
			alert_id=alert.alert_id,
			target_id=alert.target_id,
			users_notified=recipients_notified,
			channels_attempted=channels_attempted,
			channels_succeeded=channels_succeeded,
			send_results=send_results,
		)
		
		_log.info(
			"Alert %s dispatched to spooler: %d recipients, %d channels queued",
			alert.alert_id,
			result.users_notified,
			result.channels_attempted,
		)
		
		return result
