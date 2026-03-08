#!/usr/bin/env python3
#
# app/db/sqlite_incidents.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Incident tracking operations."""

from __future__ import annotations

import sqlite3

from ..utils.time import utcnow
from .sqlite_runtime import transaction


def create_incident(conn: sqlite3.Connection, monitor_id: int, cause: str | None = None) -> int:
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
			"INSERT INTO incidents (monitor_id, started_at, cause) VALUES (?, ?, ?)",
			(monitor_id, now, cause),
		)
		return cur.lastrowid


def resolve_incident(conn: sqlite3.Connection, incident_id: int) -> None:
	now = utcnow()
	with transaction(conn):
		row = conn.execute(
			"SELECT started_at FROM incidents WHERE id = ?", (incident_id,)
		).fetchone()
		duration = None
		if row and row["started_at"]:
			duration = int((now - row["started_at"]).total_seconds())
		conn.execute(
			"UPDATE incidents SET resolved_at = ?, duration_seconds = ? WHERE id = ?",
			(now, duration, incident_id),
		)


def get_open_incident(conn: sqlite3.Connection, monitor_id: int) -> sqlite3.Row | None:
	cur = conn.execute(
		"SELECT * FROM incidents WHERE monitor_id = ? AND resolved_at IS NULL ORDER BY started_at DESC LIMIT 1",
		(monitor_id,),
	)
	return cur.fetchone()


def get_incidents_for_monitor(
	conn: sqlite3.Connection,
	monitor_id: int,
	limit: int = 50,
) -> list[sqlite3.Row]:
	cur = conn.execute(
		"SELECT * FROM incidents WHERE monitor_id = ? ORDER BY started_at DESC LIMIT ?",
		(monitor_id, limit),
	)
	return cur.fetchall()


def get_recent_incidents(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
	cur = conn.execute(
		"""
		SELECT i.*, m.name as monitor_name
		FROM incidents i
		JOIN monitors m ON i.monitor_id = m.id
		ORDER BY i.started_at DESC LIMIT ?
		""",
		(limit,),
	)
	return cur.fetchall()
