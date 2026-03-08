#!/usr/bin/env python3
#
# app/db/sqlite_auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Auth-token handling and login-attempt lockout tracking."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from ..utils.crypto import hash_token
from ..utils.time import utcnow
from .sqlite_runtime import transaction


def create_auth_token(
	conn: sqlite3.Connection,
	user_id: int,
	token: str,
	expires_at,
	max_expires_at,
) -> int:
	now = utcnow()
	token_hash = hash_token(token)
	with transaction(conn):
		cur = conn.execute(
			"""
			INSERT INTO auth_tokens (user_id, token_hash, expires_at, max_expires_at, created_at)
			VALUES (?, ?, ?, ?, ?)
			""",
			(user_id, token_hash, expires_at, max_expires_at, now),
		)
		return cur.lastrowid


def get_user_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
	token_hash = hash_token(token)
	now = utcnow()
	cur = conn.execute(
		"""
		SELECT u.* FROM users u
		JOIN auth_tokens t ON u.id = t.user_id
		WHERE t.token_hash = ? AND t.expires_at > ? AND u.is_active = 1
		""",
		(token_hash, now),
	)
	return cur.fetchone()


def refresh_auth_token(conn: sqlite3.Connection, token: str, hours: int = 1) -> None:
	token_hash = hash_token(token)
	now = utcnow()
	new_expires = now + timedelta(hours=hours)
	with transaction(conn):
		conn.execute(
			"UPDATE auth_tokens SET expires_at = MIN(?, max_expires_at) WHERE token_hash = ?",
			(new_expires, token_hash),
		)


def delete_auth_token(conn: sqlite3.Connection, token: str) -> None:
	token_hash = hash_token(token)
	with transaction(conn):
		conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))


def delete_expired_tokens(conn: sqlite3.Connection) -> int:
	now = utcnow()
	with transaction(conn):
		cur = conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now,))
		return cur.rowcount


def delete_user_tokens(conn: sqlite3.Connection, user_id: int) -> None:
	with transaction(conn):
		conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))


# Login attempt tracking

MIN_FAILURES_FOR_LOCKOUT = 3
BASE_LOCKOUT_SECONDS = 15
MAX_LOCKOUT_SECONDS = 86400


def _calculate_lockout_seconds(failed_count: int) -> int:
	if failed_count < MIN_FAILURES_FOR_LOCKOUT:
		return 0
	exponent = failed_count - MIN_FAILURES_FOR_LOCKOUT
	if exponent > 16:
		return MAX_LOCKOUT_SECONDS
	lockout = BASE_LOCKOUT_SECONDS * (2 ** exponent)
	return min(lockout, MAX_LOCKOUT_SECONDS)


def is_ip_locked(conn: sqlite3.Connection, ip_address: str) -> tuple[bool, int]:
	cur = conn.execute(
		"SELECT failed_count, locked_until FROM login_attempts WHERE ip_address = ?",
		(ip_address,),
	)
	row = cur.fetchone()
	if not row:
		return (False, 0)
	locked_until = row["locked_until"]
	if not locked_until:
		return (False, 0)
	now = utcnow()
	if now < locked_until:
		remaining = int((locked_until - now).total_seconds())
		return (True, remaining)
	return (False, 0)


def record_failed_login(conn: sqlite3.Connection, ip_address: str) -> None:
	now = utcnow()
	with transaction(conn, immediate=True):
		cur = conn.execute(
			"SELECT failed_count FROM login_attempts WHERE ip_address = ?",
			(ip_address,),
		)
		row = cur.fetchone()
		current_count = row["failed_count"] if row else 0
		new_count = current_count + 1
		lockout_seconds = _calculate_lockout_seconds(new_count)
		locked_until = now + timedelta(seconds=lockout_seconds) if lockout_seconds > 0 else None
		conn.execute(
			"""
			INSERT INTO login_attempts (ip_address, failed_count, last_attempt_at, created_at, locked_until)
			VALUES (?, ?, ?, ?, ?)
			ON CONFLICT(ip_address) DO UPDATE SET
				failed_count = ?,
				last_attempt_at = excluded.last_attempt_at,
				locked_until = excluded.locked_until
			""",
			(ip_address, new_count, now, now, locked_until, new_count),
		)


def clear_login_attempts(conn: sqlite3.Connection, ip_address: str) -> None:
	with transaction(conn):
		conn.execute("DELETE FROM login_attempts WHERE ip_address = ?", (ip_address,))
