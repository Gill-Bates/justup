#!/usr/bin/env python3
#
# app/db/sqlite_schema.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite schema initialization and bootstrap routines for justUp."""

from __future__ import annotations

import logging
import sqlite3

from ..utils.crypto import hash_password
from ..utils.time import utcnow
from .sqlite_runtime import connect, transaction

_log = logging.getLogger(__name__)


def ensure_schema(db_path) -> None:
	"""Open a connection to *db_path*, apply the schema, and close."""
	from pathlib import Path
	conn = connect(Path(db_path))
	try:
		init_schema(conn)
	finally:
		conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
	"""Create the required database schema."""
	with transaction(conn, immediate=True):
		# Users table
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS users (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				username TEXT NOT NULL UNIQUE,
				password_hash TEXT NOT NULL,
				is_admin INTEGER NOT NULL DEFAULT 0,
				is_active INTEGER NOT NULL DEFAULT 1,
				otp_secret TEXT,
				otp_enabled INTEGER NOT NULL DEFAULT 0,
				otp_recovery_codes TEXT,
				auth_method TEXT DEFAULT 'password',
				passkey_enabled INTEGER DEFAULT 0,
				passkey_pending INTEGER DEFAULT 0,
				last_login_at timestamp,
				last_login_ip TEXT,
				created_at timestamp NOT NULL
			)
			"""
		)

		# Passkeys table (WebAuthn credentials)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS passkeys (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				user_id INTEGER NOT NULL,
				credential_id TEXT NOT NULL UNIQUE,
				public_key BLOB NOT NULL,
				sign_count INTEGER NOT NULL DEFAULT 0,
				device_name TEXT,
				transports TEXT,
				created_at TIMESTAMP NOT NULL,
				FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_passkeys_user_id ON passkeys(user_id)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_passkeys_credential_id ON passkeys(credential_id)")

		# Passkey challenges (ephemeral)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS passkey_challenges (
				challenge TEXT PRIMARY KEY,
				ceremony_type TEXT NOT NULL CHECK (ceremony_type IN ('registration', 'authentication')),
				user_id INTEGER,
				username TEXT,
				expires_at REAL NOT NULL,
				created_at REAL NOT NULL
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_passkey_challenges_expires ON passkey_challenges(expires_at)")

		# Auth tokens
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS auth_tokens (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				user_id INTEGER NOT NULL,
				token_hash TEXT NOT NULL UNIQUE,
				expires_at timestamp NOT NULL,
				max_expires_at timestamp NOT NULL,
				created_at timestamp NOT NULL,
				FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_expires_at ON auth_tokens(expires_at)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_id ON auth_tokens(user_id)")

		# Settings
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS settings (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL,
				updated_at timestamp NOT NULL
			)
			"""
		)

		# Login attempts (brute-force protection)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS login_attempts (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				ip_address TEXT NOT NULL UNIQUE,
				failed_count INTEGER NOT NULL DEFAULT 0,
				last_attempt_at timestamp NOT NULL,
				locked_until timestamp,
				created_at timestamp NOT NULL
			)
			"""
		)

		# =================================================================
		# Uptime Monitor - Core Tables
		# =================================================================

		# Monitors (HTTP, TCP, Ping, DNS, etc.)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS monitors (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL,
				monitor_type TEXT NOT NULL DEFAULT 'http',
				url TEXT,
				hostname TEXT,
				port INTEGER,
				method TEXT DEFAULT 'GET',
				expected_status_code INTEGER DEFAULT 200,
				keyword TEXT,
				keyword_match_type TEXT DEFAULT 'contains',
				headers_json TEXT,
				body TEXT,
				timeout_seconds INTEGER NOT NULL DEFAULT 30,
				interval_seconds INTEGER NOT NULL DEFAULT 60,
				retries INTEGER NOT NULL DEFAULT 3,
				retry_interval_seconds INTEGER NOT NULL DEFAULT 10,
				is_active INTEGER NOT NULL DEFAULT 1,
				verify_ssl INTEGER NOT NULL DEFAULT 1,
				follow_redirects INTEGER NOT NULL DEFAULT 1,
				max_redirects INTEGER NOT NULL DEFAULT 10,
				notification_group_id INTEGER,
				description TEXT,
				tags TEXT,
				created_by INTEGER,
				created_at timestamp NOT NULL,
				updated_at timestamp NOT NULL,
				FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
				FOREIGN KEY(notification_group_id) REFERENCES notification_groups(id) ON DELETE SET NULL
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_monitors_active ON monitors(is_active)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_monitors_type ON monitors(monitor_type)")

		# Monitor status (current state)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS monitor_status (
				monitor_id INTEGER PRIMARY KEY,
				status TEXT NOT NULL DEFAULT 'pending',
				last_check_at timestamp,
				last_up_at timestamp,
				last_down_at timestamp,
				last_response_time_ms REAL,
				last_status_code INTEGER,
				last_error TEXT,
				consecutive_failures INTEGER NOT NULL DEFAULT 0,
				consecutive_successes INTEGER NOT NULL DEFAULT 0,
				uptime_pct_24h REAL,
				uptime_pct_7d REAL,
				uptime_pct_30d REAL,
				cert_expiry_at timestamp,
				cert_issuer TEXT,
				FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
			)
			"""
		)

		# Incidents (downtime periods)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS incidents (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				monitor_id INTEGER NOT NULL,
				started_at timestamp NOT NULL,
				resolved_at timestamp,
				duration_seconds INTEGER,
				cause TEXT,
				FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_monitor ON incidents(monitor_id)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started_at)")

		# Notification channels
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS notification_channels (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL,
				channel_type TEXT NOT NULL,
				config_json TEXT NOT NULL,
				is_active INTEGER NOT NULL DEFAULT 1,
				created_at timestamp NOT NULL,
				updated_at timestamp NOT NULL
			)
			"""
		)

		# Notification groups (link monitors to channels)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS notification_groups (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL,
				created_at timestamp NOT NULL,
				updated_at timestamp NOT NULL
			)
			"""
		)

		# Group-channel mapping (many-to-many)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS notification_group_channels (
				group_id INTEGER NOT NULL,
				channel_id INTEGER NOT NULL,
				PRIMARY KEY (group_id, channel_id),
				FOREIGN KEY(group_id) REFERENCES notification_groups(id) ON DELETE CASCADE,
				FOREIGN KEY(channel_id) REFERENCES notification_channels(id) ON DELETE CASCADE
			)
			"""
		)

		# Status pages
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS status_pages (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				title TEXT NOT NULL,
				slug TEXT NOT NULL UNIQUE,
				description TEXT,
				is_public INTEGER NOT NULL DEFAULT 1,
				custom_css TEXT,
				show_powered_by INTEGER NOT NULL DEFAULT 1,
				created_at timestamp NOT NULL,
				updated_at timestamp NOT NULL
			)
			"""
		)
		conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_status_pages_slug ON status_pages(slug)")

		# Status page monitors (which monitors to show on which page)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS status_page_monitors (
				status_page_id INTEGER NOT NULL,
				monitor_id INTEGER NOT NULL,
				sort_order INTEGER NOT NULL DEFAULT 0,
				group_name TEXT,
				PRIMARY KEY (status_page_id, monitor_id),
				FOREIGN KEY(status_page_id) REFERENCES status_pages(id) ON DELETE CASCADE,
				FOREIGN KEY(monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
			)
			"""
		)

		# Factory defaults
		now = utcnow()
		conn.executemany(
			"""
			INSERT OR IGNORE INTO settings (key, value, updated_at)
			VALUES (?, ?, ?)
			""",
			[
				("gui_port", "8000", now),
				("gui_localhost_only", "false", now),
				("check_interval_default", "60", now),
				("tsdb_retention_days", "90", now),
				("enable_swagger", "0", now),
			],
		)


def ensure_default_admin(db_path) -> None:
	"""Open a connection to *db_path*, create a default admin if none exist."""
	from pathlib import Path
	conn = connect(Path(db_path))
	try:
		_create_default_admin(conn)
	finally:
		conn.close()


def _create_default_admin(conn: sqlite3.Connection) -> None:
	"""Create a default admin user if no users exist."""
	cur = conn.execute("SELECT COUNT(*) FROM users")
	if cur.fetchone()[0] > 0:
		return

	now = utcnow()
	password_hash = hash_password("admin")
	with transaction(conn):
		conn.execute(
			"""
			INSERT INTO users (username, password_hash, is_admin, is_active, created_at)
			VALUES (?, ?, 1, 1, ?)
			""",
			("admin", password_hash, now),
		)
	_log.info("Default admin user created (username: admin, password: admin)")
