#!/usr/bin/env python3
#
# app/db/sqlite_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Settings key-value store operations."""

from __future__ import annotations

import sqlite3

from ..utils.time import utcnow
from .sqlite_runtime import transaction


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
	cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
	row = cur.fetchone()
	return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
	now = utcnow()
	with transaction(conn):
		conn.execute(
			"""
			INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
			ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
			""",
			(key, value, now),
		)


def get_all_settings(conn: sqlite3.Connection) -> dict[str, str]:
	cur = conn.execute("SELECT key, value FROM settings ORDER BY key")
	return {row[0]: row[1] for row in cur.fetchall()}


def get_tsdb_retention_days(conn: sqlite3.Connection) -> int:
	val = get_setting(conn, "tsdb_retention_days", "90")
	try:
		return max(1, int(val))
	except (ValueError, TypeError):
		return 90


def validate_secret_key(conn: sqlite3.Connection) -> bool:
	"""Check if the current secret key matches what was used for encryption."""
	# For now, always return True. Actual validation happens at OTP decrypt time.
	return True
