#!/usr/bin/env python3
#
# app/db/sqlite_runtime.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite runtime helpers: adapters, connections, and transactions."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

UNSET: object = object()


def _adapt_datetime(value: datetime) -> str:
	if value.tzinfo is None:
		raise ValueError("Naive datetime not allowed in SQLite")
	return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _convert_datetime(value: bytes) -> datetime:
	s = value.decode("utf-8")
	if s.endswith("Z"):
		s = s[:-1] + "+00:00"
	try:
		dt = datetime.fromisoformat(s)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		return dt.astimezone(timezone.utc)
	except ValueError:
		_log.error("Corrupt timestamp in database: %r", value.decode("utf-8", errors="replace"))
		return datetime(1970, 1, 1, tzinfo=timezone.utc)


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("timestamp", _convert_datetime)


_OPEN_CONNECTIONS: set[sqlite3.Connection] = set()
_CONNECTIONS_LOCK = threading.Lock()


def connect(db_path: Path) -> sqlite3.Connection:
	db_path.parent.mkdir(parents=True, exist_ok=True)
	conn = sqlite3.connect(
		str(db_path),
		detect_types=sqlite3.PARSE_DECLTYPES,
		check_same_thread=False,
		timeout=30.0,
	)
	conn.row_factory = sqlite3.Row

	max_retries = 5
	for attempt in range(max_retries):
		try:
			cursor = conn.execute("PRAGMA journal_mode")
			current_mode = cursor.fetchone()[0].upper()
			cursor.close()
			if current_mode != "WAL":
				conn.execute("PRAGMA journal_mode=WAL")
			break
		except sqlite3.OperationalError as e:
			if "locked" in str(e).lower() and attempt < max_retries - 1:
				import time
				time.sleep(0.1 * (2 ** attempt))
			else:
				raise

	conn.execute("PRAGMA foreign_keys=ON")

	with _CONNECTIONS_LOCK:
		_OPEN_CONNECTIONS.add(conn)

	return conn


def close_connection(conn: sqlite3.Connection) -> None:
	with _CONNECTIONS_LOCK:
		_OPEN_CONNECTIONS.discard(conn)
	conn.close()


def close_all_connections() -> int:
	with _CONNECTIONS_LOCK:
		connections = list(_OPEN_CONNECTIONS)
		_OPEN_CONNECTIONS.clear()
	success_count = 0
	for conn in connections:
		try:
			conn.close()
			success_count += 1
		except Exception as e:
			_log.warning("Failed to close SQLite connection: %s", e)
	return success_count


def checkpoint_wal(db_path: Path, mode: str = "TRUNCATE") -> dict[str, int | str]:
	mode_upper = mode.strip().upper()
	if mode_upper not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
		mode_upper = "TRUNCATE"
	conn = None
	try:
		conn = sqlite3.connect(str(db_path), timeout=10.0)
		result = conn.execute(f"PRAGMA wal_checkpoint({mode_upper})").fetchone()
		return {"mode": mode_upper, "busy": result[0], "log_frames": result[1], "checkpointed_frames": result[2]}
	finally:
		if conn:
			conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False):
	if immediate:
		conn.execute("BEGIN IMMEDIATE")
	else:
		conn.execute("BEGIN")
	try:
		yield conn
		conn.commit()
	except BaseException:
		conn.rollback()
		raise
