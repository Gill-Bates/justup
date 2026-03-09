#!/usr/bin/env python3
#
# app/services/notifications/email_sender.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Email notification sender via SMTP.

SMTP is an optional notification channel. All configuration is stored in the
database settings table - no environment variables.

Configuration required:
- smtp_enabled: Must be True to send emails
- smtp_host: SMTP server hostname (required)
- smtp_port: SMTP server port (defaults to 587 for compatibility)
- smtp_from: Sender email address (required)
- smtp_user/smtp_password: Optional authentication credentials
- smtp_use_tls: Whether to use STARTTLS (default: False)
  NOTE: This is STARTTLS (upgrade plaintext to TLS), not implicit TLS/SMTPS

Bulk sending:
- When multiple recipients need the same alert, a single email is sent
  with all recipients in BCC for privacy and efficiency.
"""

from __future__ import annotations

import logging
import smtplib
import sqlite3
import ssl
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

from ...db import sqlite as sqlite_db
from ...utils.constants import SettingKeys
from .dispatcher import AlertPayload, SendResult, RecipientInfo

_log = logging.getLogger(__name__)

# Default group name when target has no group assigned
DEFAULT_GROUP_NAME = "Default"


@dataclass
class SMTPConfig:
	"""SMTP configuration loaded from database."""
	enabled: bool
	host: str | None
	port: int
	user: str | None
	password: str | None
	use_tls: bool
	from_address: str | None
	subject_template: str
	html_template: str


@dataclass
class EmailContent:
	"""Rendered email content ready to send."""
	subject: str
	html_body: str


class EmailSender:
	"""
	Email notification sender via SMTP.
	
	Reads all configuration from database at send-time (no restart required).
	Requires smtp_enabled=True and smtp_host to be configured.
	
	Implements the NotificationSender protocol for use with NotificationDispatcher.
	
	Bulk sending:
	- send_bulk() sends one email with all recipients in BCC
	- More efficient (1 SMTP connection) and privacy-preserving
	"""
	
	def __init__(self, db_conn_factory: Callable[[], sqlite3.Connection]):
		"""DB connection factory used to read SMTP settings at send-time."""
		self.db_conn_factory = db_conn_factory
	
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
	
	# ─────────────────────────────────────────────────────────────────────────
	# Private Helper Methods (DRY: shared by send() and send_bulk())
	# ─────────────────────────────────────────────────────────────────────────
	
	def _load_smtp_config(self, conn: sqlite3.Connection) -> SMTPConfig:
		"""Load SMTP configuration from database.
		
		Args:
			conn: Active database connection
			
		Returns:
			SMTPConfig with all settings loaded
		"""
		from .email_template import DEFAULT_SUBJECT, DEFAULT_HTML_TEMPLATE
		from ...utils.crypto import decrypt_secret
		
		smtp_enabled = sqlite_db.get_setting(conn, SettingKeys.SMTP_ENABLED, False)
		smtp_host = sqlite_db.get_setting(conn, SettingKeys.SMTP_HOST, None)
		smtp_port = sqlite_db.get_setting(conn, SettingKeys.SMTP_PORT, None)
		smtp_user = sqlite_db.get_setting(conn, SettingKeys.SMTP_USER, None)
		smtp_password = sqlite_db.get_setting(conn, SettingKeys.SMTP_PASSWORD, None)
		smtp_use_tls = sqlite_db.get_setting(conn, SettingKeys.SMTP_USE_TLS, False)
		from_address = sqlite_db.get_setting(conn, SettingKeys.SMTP_FROM, None)
		
		# Load email templates (fallback to defaults)
		subject_template = sqlite_db.get_setting(conn, "smtp_mail_template_subject", None) or DEFAULT_SUBJECT
		html_template = sqlite_db.get_setting(conn, "smtp_mail_template_html", None) or DEFAULT_HTML_TEMPLATE
		
		# Normalize port - default to 587 (STARTTLS) for compatibility
		# Port is technically required, but we provide a sensible default
		try:
			port = int(smtp_port) if smtp_port else 587
		except (ValueError, TypeError):
			port = 587
		
		# Decrypt SMTP password if encrypted
		if smtp_password:
			try:
				smtp_password = decrypt_secret(smtp_password)
			except Exception as decrypt_err:
				_log.warning("Using legacy plaintext SMTP password (decrypt failed: %s)", decrypt_err)
		
		return SMTPConfig(
			enabled=smtp_enabled,
			host=smtp_host,
			port=port,
			user=smtp_user,
			password=smtp_password,
			use_tls=smtp_use_tls,
			from_address=from_address,
			subject_template=subject_template,
			html_template=html_template,
		)
	
	def _build_email_context(self, conn: sqlite3.Connection, alert: AlertPayload) -> dict:
		"""Build template context from alert and target data.
		
		Args:
			conn: Active database connection
			alert: AlertPayload with alert details
			
		Returns:
			Context dict for template rendering
		"""
		from .email_template import build_alert_context
		
		# Use Row factory for named column access
		conn.row_factory = sqlite3.Row
		try:
			# Load full target info from database (including sla_enabled)
			target_row = conn.execute(
				"""
				SELECT
					t.name,
					t.host,
					t.group_name,
					t.sla_enabled,
					t.enable_ping,
					t.enable_http_check,
					t.enable_tcp_connect,
					t.enable_cert_expiration,
					ts.uptime_percent
				FROM targets t
				LEFT JOIN target_status ts ON ts.target_id = t.id
				WHERE t.id = ?
				""",
				(alert.target_id,)
			).fetchone()
		finally:
			conn.row_factory = None  # Reset to avoid side effects
		
		if target_row:
			target_name = target_row["name"] or alert.target_name or "Unknown"
			target_host = target_row["host"] or "N/A"
			group_name = target_row["group_name"] or DEFAULT_GROUP_NAME
			sla_enabled = bool(target_row["sla_enabled"])

			# Prefer an explicit check label for CERT alerts.
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
			target_name = alert.target_name or "Unknown"
			target_host = "N/A"
			group_name = DEFAULT_GROUP_NAME
			check_type = "N/A"
			sla_enabled = False
		
		target_data = {
			"name": target_name,
			"host": target_host,
			"group": group_name,
			"check_type": check_type,
		}
		
		# Format timestamps
		started_at_str = alert.started_at.isoformat() if alert.started_at else None
		resolved_at_str = alert.ended_at.isoformat() if alert.ended_at else None
		
		# Only include SLA data if SLA is enabled for this target
		sla_threshold_float = None
		sla_actual = None
		
		if sla_enabled:
			# Get SLA threshold from settings (default 99.9%)
			sla_threshold = sqlite_db.get_setting(conn, SettingKeys.SLA_UPTIME_THRESHOLD, "99.9")
			try:
				sla_threshold_float = float(sla_threshold)
			except (ValueError, TypeError):
				sla_threshold_float = 99.9
			
			# Get actual uptime from target (already loaded in first query)
			if target_row and target_row["uptime_percent"] is not None:
				sla_actual = target_row["uptime_percent"]
		
		# Get system URL from settings
		system_url = sqlite_db.get_setting(conn, SettingKeys.SYSTEM_BASE_URL, None) or "#"
		
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
		
		return build_alert_context(alert_data, target_data)
	
	def _render_email(self, config: SMTPConfig, context: dict) -> EmailContent:
		"""Render email subject and body from templates.
		
		Args:
			config: SMTPConfig with templates
			context: Template context dict
			
		Returns:
			EmailContent with rendered subject and HTML body
		"""
		from .email_template import render_template
		
		subject = render_template(config.subject_template, context)
		html_body = render_template(config.html_template, context)
		
		return EmailContent(subject=subject, html_body=html_body)
	
	def _validate_config(self, config: SMTPConfig) -> str | None:
		"""Validate SMTP configuration completeness.
		
		Assumes config.enabled has already been checked by caller.
		
		Args:
			config: SMTPConfig to validate
			
		Returns:
			Error message if invalid, None if valid
		"""
		if not config.host:
			return "SMTP host not configured"
		if not config.from_address:
			return "SMTP from address not configured"
		return None
	
	def _prepare_email_sync(
		self,
		recipient_email: str,
		alert: AlertPayload,
	) -> tuple[SMTPConfig, EmailContent]:
		"""Load config, build context, render email (runs in thread pool).
		
		Args:
			recipient_email: Email address for logging purposes
			alert: AlertPayload with alert details
			
		Returns:
			Tuple of (SMTPConfig, EmailContent)
		"""
		with self._db_session() as conn:
			config = self._load_smtp_config(conn)
			context = self._build_email_context(conn, alert)
			content = self._render_email(config, context)
			return config, content
	
	def _prepare_bulk_email_sync(
		self,
		alert: AlertPayload,
	) -> tuple[SMTPConfig, EmailContent]:
		"""Load config, build context, render email for bulk send (runs in thread pool).
		
		Args:
			alert: AlertPayload with alert details
			
		Returns:
			Tuple of (SMTPConfig, EmailContent)
		"""
		with self._db_session() as conn:
			config = self._load_smtp_config(conn)
			context = self._build_email_context(conn, alert)
			content = self._render_email(config, context)
			return config, content
	
	def _load_smtp_config_sync(self) -> SMTPConfig:
		"""Load SMTP config from DB (runs in thread pool).
		
		Returns:
			SMTPConfig loaded from database
		"""
		with self._db_session() as conn:
			return self._load_smtp_config(conn)
	
	def _send_smtp_sync(self, msg: EmailMessage, config: SMTPConfig) -> None:
		"""Synchronous SMTP send (runs in thread pool).
		
		Args:
			msg: Composed EmailMessage to send
			config: SMTPConfig with connection details
		"""
		with smtplib.SMTP(config.host, config.port, timeout=15) as server:
			if config.use_tls:
				# Use secure SSL context for STARTTLS
				context = ssl.create_default_context()
				server.starttls(context=context)
			if config.user and config.password:
				server.login(config.user, config.password)
			server.send_message(msg)
	
	# ─────────────────────────────────────────────────────────────────────────
	# Public API
	# ─────────────────────────────────────────────────────────────────────────
	
	async def send_bulk(self, recipients: list[RecipientInfo], alert: AlertPayload) -> list[SendResult]:
		"""Send a single alert email to multiple recipients via BCC.
		
		This is more efficient than sending individual emails:
		- Single SMTP connection
		- Single email composition
		- Recipients don't see each other's addresses (BCC)
		
		Args:
			recipients: List of RecipientInfo with email addresses
			alert: AlertPayload with alert details
			
		Returns:
			List of SendResult for each original recipient (including skipped)
		"""
		# Build result list to track all original recipients (index-based)
		results: list[SendResult] = []
		valid_indices: list[int] = []
		valid_recipients: list[RecipientInfo] = []
		
		# Separate valid from skipped recipients
		for i, r in enumerate(recipients):
			addr = r.email or "<no-email>"
			if not r.email or not r.is_enabled:
				results.append(SendResult(
					success=False,
					channel_type="email",
					channel_address=addr,
					skipped=True,
				))
			else:
				results.append(None)  # Placeholder, filled after send
				valid_indices.append(i)
				valid_recipients.append(r)
		
		# If no valid recipients, return early
		if not valid_recipients:
			return results
		
		email_addresses = [r.email for r in valid_recipients]
		
		# Load config and build email (runs in thread pool to avoid blocking event loop)
		try:
			config, content = await asyncio.to_thread(
				self._prepare_bulk_email_sync, alert
			)
			
			# Check if SMTP is enabled
			if not config.enabled:
				_log.debug("SMTP disabled, skipping bulk email")
				for idx in valid_indices:
					addr = recipients[idx].email
					results[idx] = SendResult(
						success=False, channel_type="email",
						channel_address=addr, skipped=True
					)
				return results
			
			# Validate config
			error = self._validate_config(config)
			if error:
				_log.warning("SMTP config invalid: %s", error)
				for idx in valid_indices:
					addr = recipients[idx].email
					results[idx] = SendResult(
						success=False, channel_type="email",
						channel_address=addr, error=error
					)
				return results
			
		except Exception as e:
			_log.error("Failed to prepare bulk email: %s", e)
			for idx in valid_indices:
				addr = recipients[idx].email
				results[idx] = SendResult(
					success=False, channel_type="email",
					channel_address=addr, error=str(e)
				)
			return results
		
		# Compose message with BCC
		msg = EmailMessage()
		msg["Subject"] = content.subject
		msg["From"] = config.from_address
		msg["To"] = config.from_address  # Send to self, recipients in BCC
		msg["Bcc"] = ", ".join(email_addresses)
		msg.set_content(content.html_body, subtype="html")
		
		# Send via thread pool
		try:
			await asyncio.to_thread(self._send_smtp_sync, msg, config)
			_log.info("Bulk email sent to %d recipients via BCC", len(email_addresses))
			for idx in valid_indices:
				addr = recipients[idx].email
				results[idx] = SendResult(
					success=True, channel_type="email", channel_address=addr
				)
		
		except smtplib.SMTPAuthenticationError as e:
			error_msg = f"SMTP auth failed (code {e.smtp_code})"
			_log.error("Bulk email failed: %s", error_msg)
			for idx in valid_indices:
				addr = recipients[idx].email
				results[idx] = SendResult(
					success=False, channel_type="email",
					channel_address=addr, error=error_msg
				)
		
		except smtplib.SMTPRecipientsRefused as e:
			# SMTPRecipientsRefused means the ENTIRE send was rejected.
			# No recipient received the email.
			refused_details = {
				addr: f"Refused (code {info[0]}): {info[1].decode(errors='replace')}"
				for addr, info in e.recipients.items()
			}
			_log.error("Bulk email refused: %s", refused_details)
			for idx in valid_indices:
				addr = recipients[idx].email
				error_detail = refused_details.get(addr, "All recipients refused by server")
				results[idx] = SendResult(
					success=False, channel_type="email",
					channel_address=addr, error=error_detail
				)
		
		except smtplib.SMTPException as e:
			error_msg = f"SMTP error: {str(e)}"
			_log.error("Bulk email failed: %s", error_msg)
			for idx in valid_indices:
				addr = recipients[idx].email
				results[idx] = SendResult(
					success=False, channel_type="email",
					channel_address=addr, error=error_msg
				)
		
		except TimeoutError:
			error_msg = f"Timeout connecting to {config.host}:{config.port}"
			_log.error("Bulk email failed: %s", error_msg)
			for idx in valid_indices:
				addr = recipients[idx].email
				results[idx] = SendResult(
					success=False, channel_type="email",
					channel_address=addr, error=error_msg
				)
		
		except OSError as e:
			error_msg = f"Network error: {str(e)}"
			_log.error("Bulk email failed: %s", error_msg)
			for idx in valid_indices:
				addr = recipients[idx].email
				results[idx] = SendResult(
					success=False, channel_type="email",
					channel_address=addr, error=error_msg
				)
		
		except Exception as e:
			error_msg = f"Unexpected error: {str(e)}"
			_log.error("Bulk email failed: %s", error_msg)
			for idx in valid_indices:
				addr = recipients[idx].email
				results[idx] = SendResult(
					success=False, channel_type="email",
					channel_address=addr, error=error_msg
				)
		
		return results
	
	async def send(self, recipient: RecipientInfo, alert: AlertPayload) -> SendResult:
		"""Send alert via email using SMTP settings from DB.

		Args:
			recipient: RecipientInfo with email address
			alert: AlertPayload with alert details
			
		Returns:
			SendResult with success status and error details
		"""
		email_address = recipient.email
		
		# No email configured for this recipient
		if not email_address:
			return SendResult(
				success=False,
				channel_type="email",
				channel_address="<no-email>",
				skipped=True,
			)
		
		# Respect disabled recipients
		if not recipient.is_enabled:
			return SendResult(
				success=False,
				channel_type="email",
				channel_address=email_address,
				skipped=True,
			)

		# Load config, build context, and render email (runs in thread pool)
		try:
			config, content = await asyncio.to_thread(
				self._prepare_email_sync, email_address, alert
			)
			
			# Check if SMTP is enabled
			if not config.enabled:
				_log.debug("SMTP disabled, skipping email to %s", email_address)
				return SendResult(
					success=False,
					channel_type="email",
					channel_address=email_address,
					skipped=True,
				)
			
			# Validate config
			error = self._validate_config(config)
			if error:
				_log.warning("SMTP config invalid for %s: %s", email_address, error)
				return SendResult(
					success=False,
					channel_type="email",
					channel_address=email_address,
					error=error,
				)
			
		except Exception as e:
			_log.error("Failed to prepare email to %s: %s", email_address, e)
			return SendResult(
				success=False, channel_type="email",
				channel_address=email_address, error=str(e)
			)

		# Compose message
		msg = EmailMessage()
		msg["Subject"] = content.subject
		msg["From"] = config.from_address
		msg["To"] = email_address
		msg.set_content(content.html_body, subtype="html")

		# Send via thread pool
		try:
			await asyncio.to_thread(self._send_smtp_sync, msg, config)
			_log.info("Email sent to %s", email_address)
			return SendResult(success=True, channel_type="email", channel_address=email_address)
		
		except smtplib.SMTPAuthenticationError as e:
			error_msg = f"SMTP auth failed (code {e.smtp_code})"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except smtplib.SMTPRecipientsRefused:
			error_msg = f"Recipient refused: {email_address}"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except smtplib.SMTPSenderRefused as e:
			error_msg = f"Sender rejected (code {e.smtp_code})"
			_log.error("Email to %s failed: %s - %s", email_address, error_msg, e.smtp_error)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except smtplib.SMTPDataError as e:
			error_msg = f"SMTP data error (code {e.smtp_code})"
			_log.error("Email to %s failed: %s - %s", email_address, error_msg, e.smtp_error)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except smtplib.SMTPConnectError:
			error_msg = f"Cannot connect to {config.host}:{config.port}"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except smtplib.SMTPServerDisconnected:
			error_msg = "SMTP server disconnected"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except smtplib.SMTPException as e:
			error_msg = f"SMTP error: {str(e)}"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except TimeoutError:
			error_msg = f"Timeout connecting to {config.host}:{config.port}"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except OSError as e:
			error_msg = f"Network error: {str(e)}"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)
		
		except Exception as e:
			error_msg = f"Unexpected error: {str(e)}"
			_log.error("Email to %s failed: %s", email_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=email_address, error=error_msg)

	async def send_custom(
		self,
		to_address: str,
		subject: str,
		html_body: str,
		text_body: str | None = None,
	) -> SendResult:
		"""Send a custom email (not alert-based) using global SMTP settings.
		
		This uses the same SMTP infrastructure as alert notifications,
		ensuring consistent delivery behavior.
		
		Args:
			to_address: Recipient email address
			subject: Email subject line
			html_body: HTML email body
			text_body: Optional plain text fallback (auto-generated if None)
			
		Returns:
			SendResult with success status and error details
		"""
		if not to_address:
			return SendResult(
				success=False,
				channel_type="email",
				channel_address="<no-email>",
				skipped=True,
			)
		
		# Load SMTP config from database (runs in thread pool)
		try:
			config = await asyncio.to_thread(self._load_smtp_config_sync)
			
			if not config.enabled:
				_log.debug("SMTP disabled, skipping custom email to %s", to_address)
				return SendResult(
					success=False,
					channel_type="email",
					channel_address=to_address,
					skipped=True,
				)
			
			error = self._validate_config(config)
			if error:
				_log.warning("SMTP config invalid: %s", error)
				return SendResult(
					success=False,
					channel_type="email",
					channel_address=to_address,
					error=error,
				)
		except Exception as e:
			_log.error("Failed to load SMTP config: %s", e)
			return SendResult(
				success=False,
				channel_type="email",
				channel_address=to_address,
				error=str(e),
			)
		
		# Compose message
		msg = EmailMessage()
		msg["Subject"] = subject
		msg["From"] = config.from_address
		msg["To"] = to_address
		
		if text_body:
			msg.set_content(text_body)
			msg.add_alternative(html_body, subtype="html")
		else:
			msg.set_content(html_body, subtype="html")
		
		# Send via thread pool
		try:
			await asyncio.to_thread(self._send_smtp_sync, msg, config)
			_log.info("Custom email sent to %s", to_address)
			return SendResult(success=True, channel_type="email", channel_address=to_address)
		
		except smtplib.SMTPAuthenticationError as e:
			error_msg = f"SMTP auth failed (code {e.smtp_code})"
			_log.error("Custom email to %s failed: %s", to_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=to_address, error=error_msg)
		
		except smtplib.SMTPRecipientsRefused:
			error_msg = f"Recipient refused: {to_address}"
			_log.error("Custom email to %s failed: %s", to_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=to_address, error=error_msg)
		
		except smtplib.SMTPException as e:
			error_msg = f"SMTP error: {str(e)}"
			_log.error("Custom email to %s failed: %s", to_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=to_address, error=error_msg)
		
		except TimeoutError:
			error_msg = f"Timeout connecting to {config.host}:{config.port}"
			_log.error("Custom email to %s failed: %s", to_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=to_address, error=error_msg)
		
		except Exception as e:
			error_msg = f"Unexpected error: {str(e)}"
			_log.error("Custom email to %s failed: %s", to_address, error_msg)
			return SendResult(success=False, channel_type="email", channel_address=to_address, error=error_msg)
