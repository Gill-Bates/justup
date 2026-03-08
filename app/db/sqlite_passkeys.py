#!/usr/bin/env python3
#
# app/db/sqlite_passkeys.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Passkey (WebAuthn credential) database operations."""

from __future__ import annotations

import logging
import sqlite3

from ..utils.time import utcnow
from .sqlite_runtime import transaction

_log = logging.getLogger(__name__)


def get_passkeys_for_user(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
	cur = conn.execute(
		"""
		SELECT id, user_id, credential_id, public_key, sign_count,
			   device_name, transports, created_at
		FROM passkeys WHERE user_id = ? ORDER BY created_at DESC
		""",
		(user_id,),
	)
	return cur.fetchall()


def get_passkey_by_credential_id(conn: sqlite3.Connection, credential_id: str) -> sqlite3.Row | None:
	cur = conn.execute(
		"""
		SELECT p.id, p.user_id, p.credential_id, p.public_key, p.sign_count,
			   p.device_name, p.transports, p.created_at,
			   u.username, u.is_active, u.auth_method
		FROM passkeys p
		JOIN users u ON p.user_id = u.id
		WHERE p.credential_id = ?
		""",
		(credential_id,),
	)
	return cur.fetchone()


def get_passkey_by_id(conn: sqlite3.Connection, passkey_id: int) -> sqlite3.Row | None:
	cur = conn.execute(
		"SELECT * FROM passkeys WHERE id = ?",
		(passkey_id,),
	)
	return cur.fetchone()


def create_passkey(
	conn: sqlite3.Connection,
	user_id: int,
	credential_id: str,
	public_key: bytes,
	sign_count: int,
	device_name: str | None = None,
	transports: str | None = None,
) -> int:
	now = utcnow()
	with transaction(conn):
		try:
			cur = conn.execute(
				"""
				INSERT INTO passkeys (user_id, credential_id, public_key, sign_count,
									  device_name, transports, created_at)
				VALUES (?, ?, ?, ?, ?, ?, ?)
				""",
				(user_id, credential_id, public_key, sign_count, device_name, transports, now),
			)
		except sqlite3.IntegrityError as e:
			raise ValueError("Credential ID already registered") from e
		return cur.lastrowid


def update_passkey_sign_count(conn: sqlite3.Connection, passkey_id: int, new_sign_count: int) -> None:
	with transaction(conn):
		if new_sign_count == 0:
			return
		cur = conn.execute(
			"UPDATE passkeys SET sign_count = ? WHERE id = ? AND sign_count < ?",
			(new_sign_count, passkey_id, new_sign_count),
		)
		if cur.rowcount == 0:
			existing = conn.execute("SELECT sign_count FROM passkeys WHERE id = ?", (passkey_id,)).fetchone()
			if existing is None:
				raise ValueError(f"Passkey {passkey_id} not found")
			raise ValueError(f"Sign count regression blocked for passkey {passkey_id}")


def delete_passkey(conn: sqlite3.Connection, passkey_id: int, user_id: int) -> bool:
	with transaction(conn):
		existing = conn.execute("SELECT user_id FROM passkeys WHERE id = ?", (passkey_id,)).fetchone()
		if existing is None:
			return False
		if existing[0] != user_id:
			return False
		conn.execute("DELETE FROM passkeys WHERE id = ?", (passkey_id,))
		return True


def count_user_passkeys(conn: sqlite3.Connection, user_id: int) -> int:
	cur = conn.execute("SELECT COUNT(*) FROM passkeys WHERE user_id = ?", (user_id,))
	return cur.fetchone()[0]


def any_passkeys_exist(conn: sqlite3.Connection) -> bool:
	cur = conn.execute("SELECT EXISTS(SELECT 1 FROM passkeys)")
	return bool(cur.fetchone()[0])


def get_credential_ids_for_user(conn: sqlite3.Connection, user_id: int) -> list[str]:
	cur = conn.execute("SELECT credential_id FROM passkeys WHERE user_id = ?", (user_id,))
	return [row[0] for row in cur.fetchall()]
