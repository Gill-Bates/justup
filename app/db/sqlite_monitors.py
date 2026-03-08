#!/usr/bin/env python3
#
# app/db/sqlite_monitors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Monitor CRUD operations."""

from __future__ import annotations

import json
import logging
import sqlite3

from ..utils.time import utcnow
from .sqlite_runtime import transaction

_log = logging.getLogger(__name__)


def get_all_monitors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	cur = conn.execute("SELECT * FROM monitors ORDER BY name")
	return cur.fetchall()


def get_active_monitors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	cur = conn.execute("SELECT * FROM monitors WHERE is_active = 1 ORDER BY name")
	return cur.fetchall()


def get_monitor_by_id(conn: sqlite3.Connection, monitor_id: int) -> sqlite3.Row | None:
	cur = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
	return cur.fetchone()


def create_monitor(
	conn: sqlite3.Connection,
	name: str,
	monitor_type: str = "http",
	url: str | None = None,
	hostname: str | None = None,
	port: int | None = None,
	method: str = "GET",
	expected_status_code: int = 200,
	keyword: str | None = None,
	timeout_seconds: int = 30,
	interval_seconds: int = 60,
	retries: int = 3,
	retry_interval_seconds: int = 10,
	verify_ssl: bool = True,
	follow_redirects: bool = True,
	max_redirects: int = 10,
	headers_json: str | None = None,
	body: str | None = None,
	description: str | None = None,
	tags: str | None = None,
	notification_group_id: int | None = None,
	created_by: int | None = None,
	is_active: bool = True,
) -> int:
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
			"""
			INSERT INTO monitors (
				name, monitor_type, url, hostname, port, method,
				expected_status_code, keyword, timeout_seconds, interval_seconds,
				retries, retry_interval_seconds, verify_ssl, follow_redirects,
				max_redirects, headers_json, body, description, tags,
				notification_group_id, created_by, is_active, created_at, updated_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				name, monitor_type, url, hostname, port, method,
				expected_status_code, keyword, timeout_seconds, interval_seconds,
				retries, retry_interval_seconds, int(verify_ssl), int(follow_redirects),
				max_redirects, headers_json, body, description, tags,
				notification_group_id, created_by, int(is_active), now, now,
			),
		)
		monitor_id = cur.lastrowid

		# Create initial status record
		conn.execute(
			"INSERT INTO monitor_status (monitor_id, status) VALUES (?, 'pending')",
			(monitor_id,),
		)

		return monitor_id


def update_monitor(
	conn: sqlite3.Connection,
	monitor_id: int,
	**kwargs,
) -> bool:
	allowed_fields = {
		"name", "monitor_type", "url", "hostname", "port", "method",
		"expected_status_code", "keyword", "keyword_match_type",
		"timeout_seconds", "interval_seconds", "retries", "retry_interval_seconds",
		"verify_ssl", "follow_redirects", "max_redirects", "headers_json",
		"body", "description", "tags", "notification_group_id", "is_active",
	}

	updates = []
	params = []
	for key, value in kwargs.items():
		if key not in allowed_fields:
			continue
		if isinstance(value, bool):
			value = int(value)
		updates.append(f"{key} = ?")
		params.append(value)

	if not updates:
		return False

	updates.append("updated_at = ?")
	params.append(utcnow())
	params.append(monitor_id)

	with transaction(conn):
		cur = conn.execute(
			f"UPDATE monitors SET {', '.join(updates)} WHERE id = ?",
			params,
		)
		return cur.rowcount > 0


def delete_monitor(conn: sqlite3.Connection, monitor_id: int) -> bool:
	with transaction(conn):
		cur = conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
		return cur.rowcount > 0


def get_monitor_status(conn: sqlite3.Connection, monitor_id: int) -> sqlite3.Row | None:
	cur = conn.execute("SELECT * FROM monitor_status WHERE monitor_id = ?", (monitor_id,))
	return cur.fetchone()


def get_all_monitor_statuses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	cur = conn.execute(
		"""
		SELECT m.*, ms.status, ms.last_check_at, ms.last_up_at, ms.last_down_at,
			   ms.last_response_time_ms, ms.last_status_code, ms.last_error,
			   ms.consecutive_failures, ms.consecutive_successes,
			   ms.uptime_pct_24h, ms.uptime_pct_7d, ms.uptime_pct_30d,
			   ms.cert_expiry_at, ms.cert_issuer
		FROM monitors m
		LEFT JOIN monitor_status ms ON m.id = ms.monitor_id
		ORDER BY m.name
		"""
	)
	return cur.fetchall()


def update_monitor_status(
	conn: sqlite3.Connection,
	monitor_id: int,
	status: str,
	response_time_ms: float | None = None,
	status_code: int | None = None,
	error: str | None = None,
	cert_expiry_at=None,
	cert_issuer: str | None = None,
) -> None:
	now = utcnow()
	with transaction(conn):
		existing = conn.execute(
			"SELECT * FROM monitor_status WHERE monitor_id = ?", (monitor_id,)
		).fetchone()

		if not existing:
			conn.execute(
				"""
				INSERT INTO monitor_status (
					monitor_id, status, last_check_at, last_response_time_ms,
					last_status_code, last_error, cert_expiry_at, cert_issuer
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
				""",
				(monitor_id, status, now, response_time_ms, status_code, error, cert_expiry_at, cert_issuer),
			)
			return

		if status == "up":
			conn.execute(
				"""
				UPDATE monitor_status SET
					status = ?, last_check_at = ?, last_up_at = ?,
					last_response_time_ms = ?, last_status_code = ?,
					last_error = NULL,
					consecutive_failures = 0,
					consecutive_successes = consecutive_successes + 1,
					cert_expiry_at = COALESCE(?, cert_expiry_at),
					cert_issuer = COALESCE(?, cert_issuer)
				WHERE monitor_id = ?
				""",
				(status, now, now, response_time_ms, status_code, cert_expiry_at, cert_issuer, monitor_id),
			)
		else:
			conn.execute(
				"""
				UPDATE monitor_status SET
					status = ?, last_check_at = ?, last_down_at = ?,
					last_response_time_ms = ?, last_status_code = ?,
					last_error = ?,
					consecutive_failures = consecutive_failures + 1,
					consecutive_successes = 0
				WHERE monitor_id = ?
				""",
				(status, now, now, response_time_ms, status_code, error, monitor_id),
			)
