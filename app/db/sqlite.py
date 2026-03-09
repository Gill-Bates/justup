#!/usr/bin/env python3
#
# app/db/sqlite.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite database access layer and schema initialization."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..utils.time import utcnow

_log = logging.getLogger(__name__)


def _adapt_datetime(value: datetime) -> str:
	if value.tzinfo is None:
		raise ValueError("Naive datetime not allowed in SQLite")
	# Start Measure 3: ISO-8601, always Z, always UTC
	return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _convert_datetime(value: bytes) -> datetime:
	s = value.decode("utf-8")
	# Measure 4: Defensive reading
	if s.endswith("Z"):
		s = s[:-1] + "+00:00"
	try:
		dt = datetime.fromisoformat(s)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		return dt.astimezone(timezone.utc)
	except ValueError:
		# Fallback if corrupt - log warning for debugging
		_log.warning(
			"Corrupt timestamp in database: %r — returning epoch-0. "
			"This may cause incorrect calculations.",
			value.decode("utf-8", errors="replace"),
		)
		return datetime.fromtimestamp(0, tz=timezone.utc)


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("timestamp", _convert_datetime)


# ---------------------------------------------------------------------------
# Connection Registry (for graceful shutdown)
# ---------------------------------------------------------------------------

_OPEN_CONNECTIONS: set[sqlite3.Connection] = set()
_CONNECTIONS_LOCK = threading.Lock()
_CONNECTIONS_OPENED = 0  # Total connections opened (cumulative)
_CONNECTIONS_CLOSED = 0  # Total connections closed (cumulative)


def connect(db_path: Path) -> sqlite3.Connection:
	"""
	Create a SQLite connection configured for this application.
	
	Thread Safety:
	    Connections use check_same_thread=False but are designed to be
	    short-lived and not shared across concurrent writers. Each request
	    should use its own connection or use a connection pool.
	
	Configuration:
	    - WAL mode for concurrent reads during writes
	    - Foreign keys enforced
	    - Automatic datetime conversion (UTC-only)
	"""
	global _CONNECTIONS_OPENED
	db_path.parent.mkdir(parents=True, exist_ok=True)
	conn = sqlite3.connect(
		str(db_path),
		detect_types=sqlite3.PARSE_DECLTYPES,
		check_same_thread=False,
	)
	conn.row_factory = sqlite3.Row
	conn.execute("PRAGMA journal_mode=WAL")
	conn.execute("PRAGMA foreign_keys=ON")
	
	# Track connection for graceful shutdown
	with _CONNECTIONS_LOCK:
		_OPEN_CONNECTIONS.add(conn)
		_CONNECTIONS_OPENED += 1
	
	return conn


def close_connection(conn: sqlite3.Connection) -> None:
	"""Close a connection and remove it from the registry."""
	global _CONNECTIONS_CLOSED
	with _CONNECTIONS_LOCK:
		was_tracked = conn in _OPEN_CONNECTIONS
		_OPEN_CONNECTIONS.discard(conn)
		if was_tracked:
			_CONNECTIONS_CLOSED += 1
	try:
		conn.close()
	except Exception as e:
		_log.debug("Failed to close SQLite connection: %s", e)


def close_all_connections() -> int:
	"""
	Close all tracked connections for graceful shutdown.
	
	Returns:
		Number of connections successfully closed.
	"""
	global _CONNECTIONS_CLOSED
	with _CONNECTIONS_LOCK:
		connections = list(_OPEN_CONNECTIONS)
		_OPEN_CONNECTIONS.clear()
		# Count ALL as "closed" for stats (they're no longer tracked)
		_CONNECTIONS_CLOSED += len(connections)
	
	success_count = 0
	for conn in connections:
		try:
			conn.close()
			success_count += 1
		except Exception as e:
			_log.warning("Failed to close SQLite connection during shutdown: %s", e)
	
	if success_count < len(connections):
		_log.warning(
			"Closed %d/%d connections during shutdown",
			success_count, len(connections)
		)
	
	return success_count


def get_connection_stats() -> dict[str, int]:
	"""
	Get connection statistics for monitoring/debugging.
	
	Returns:
		Dict with 'currently_open', 'total_opened', 'total_closed'.
	"""
	with _CONNECTIONS_LOCK:
		return {
			"currently_open": len(_OPEN_CONNECTIONS),
			"total_opened": _CONNECTIONS_OPENED,
			"total_closed": _CONNECTIONS_CLOSED,
		}


@contextmanager
def transaction(conn: sqlite3.Connection):
	"""Transaction context manager that commits or rolls back on error.
	
	Note: This relies on Python's sqlite3 implicit transaction behavior rather
	than issuing an explicit BEGIN. The isolation level varies by Python version.
	For more predictable write-locking, consider adding explicit BEGIN IMMEDIATE.
	
	Thread Safety Warning:
	    With check_same_thread=False and no connection pooling, if multiple
	    threads share a connection, they can interleave transaction() calls,
	    causing one thread's commit() to persist another thread's incomplete
	    writes. Each request should use its own connection.
	"""
	try:
		yield
		conn.commit()
	except Exception:
		conn.rollback()
		raise


def init_schema(conn: sqlite3.Connection) -> None:
	"""Create or migrate the required database schema."""
	with transaction(conn):
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS users (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				username TEXT NOT NULL UNIQUE,
				password_hash TEXT NOT NULL,
				is_admin INTEGER NOT NULL DEFAULT 0,
				is_active INTEGER NOT NULL DEFAULT 1,
				can_close_alerts INTEGER NOT NULL DEFAULT 0,
				last_login_at timestamp,
				last_login_ip TEXT,
				created_at timestamp NOT NULL
			)
			"""
		)

		# Note: Column migrations are handled in app/utils/migration.py
		# The schema above is the baseline; migrations add new columns.

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
		# Index for token expiry cleanup and user cascade
		conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_expires_at ON auth_tokens(expires_at)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_id ON auth_tokens(user_id)")
		
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS settings (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL,
				updated_at timestamp NOT NULL
			)
			"""
		)

		conn.execute(
			"""
				CREATE TABLE IF NOT EXISTS targets (
					id INTEGER PRIMARY KEY AUTOINCREMENT,
					name TEXT NOT NULL,
					group_name TEXT,
					is_enabled INTEGER NOT NULL DEFAULT 1,
					interval_seconds INTEGER NOT NULL,
					retention_days INTEGER NOT NULL,
					enable_ping INTEGER NOT NULL,
					enable_http_check INTEGER NOT NULL,
					enable_cert_expiration INTEGER NOT NULL,
					enable_tcp_connect INTEGER NOT NULL DEFAULT 0,
					host TEXT NOT NULL DEFAULT '',
					ping_host TEXT,
					http_url TEXT,
					http_username TEXT,
					http_password TEXT,
					http_prefer_head INTEGER NOT NULL DEFAULT 1,
					http_basic_auth_enabled INTEGER NOT NULL DEFAULT 0,
					http_success_codes TEXT DEFAULT '200-399',
					http_port INTEGER,
					cert_host TEXT,
					cert_port INTEGER,
					ignore_cert_errors INTEGER NOT NULL DEFAULT 0,
					tcp_host TEXT,
					tcp_ports TEXT DEFAULT '80',
					max_retries INTEGER NOT NULL DEFAULT 5,
					ping_timeout_minutes INTEGER NOT NULL DEFAULT 5,
					auto_close_alert INTEGER NOT NULL DEFAULT 1,
					sla_enabled INTEGER NOT NULL DEFAULT 0,
					sla_availability_pct REAL,
					sla_response_time_ms REAL,
					sla_ping_latency_ms REAL,
					created_at timestamp NOT NULL,
					updated_at timestamp NOT NULL
				)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_targets_group_name ON targets(group_name, name)")

		# Groups table
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS groups (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL UNIQUE,
				description TEXT,
				created_at timestamp NOT NULL,
				updated_at timestamp NOT NULL
			)
			"""
		)
		
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
		# Note: ip_address UNIQUE constraint creates an implicit index
		
		# Current status cache - updated by scheduler after each check
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS target_status (
				target_id INTEGER PRIMARY KEY,
				ping_up INTEGER,
				ping_latency_ms REAL,
				ping_checked_at timestamp,
				http_up INTEGER,
				http_status INTEGER,
				http_latency_ms REAL,
				http_checked_at timestamp,
				tcp_up INTEGER,
				tcp_latency_ms REAL,
				tcp_checked_at timestamp,
				tcp_port_status TEXT,
				cert_days_left INTEGER,
				cert_checked_at timestamp,
				uptime_percent REAL,
				uptime_percent_30d REAL,
				overall_status TEXT NOT NULL DEFAULT 'unknown',
				last_down_at timestamp,
				updated_at timestamp NOT NULL,
				FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
			)
			"""
		)
		
		# Scheduler run tracking for idempotency and crash recovery
		# NOTE: Uses TEXT for timestamps instead of 'timestamp' type. This bypasses
		# the UTC-enforcing converter, so these columns return raw strings instead
		# of datetime objects. The same applies to target_locks, pop_locations, and
		# pop_import_runs tables. Consider migrating to 'timestamp' type for consistency.
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS scheduler_runs (
				run_id TEXT PRIMARY KEY,
				started_at TEXT NOT NULL,
				heartbeat_at TEXT NOT NULL
			)
			"""
		)
		
		# Target check locks to prevent duplicate checks across restarts
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS target_locks (
				target_id INTEGER PRIMARY KEY,
				run_id TEXT NOT NULL,
				locked_at TEXT NOT NULL,
				expires_at TEXT NOT NULL,
				FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_target_locks_expires ON target_locks(expires_at)")

		# Many-to-many: targets <-> users (who gets alerted)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS target_alert_users (
				target_id INTEGER NOT NULL,
				user_id INTEGER NOT NULL,
				PRIMARY KEY (target_id, user_id),
				FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE,
				FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_target_alert_users_target ON target_alert_users(target_id)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_target_alert_users_user ON target_alert_users(user_id)")

		# Alert recipients table (phone contacts for Signal notifications + email)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS recipients (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL,
				phone TEXT,
				email TEXT,
				is_enabled INTEGER NOT NULL DEFAULT 1,
				created_at timestamp NOT NULL,
				updated_at timestamp NOT NULL,
				CHECK (phone IS NOT NULL OR email IS NOT NULL)
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_recipients_phone ON recipients(phone)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_recipients_email ON recipients(email)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_recipients_enabled ON recipients(is_enabled)")
		
		# Many-to-many: targets <-> recipients (which phone numbers get alerted)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS target_recipients (
				target_id INTEGER NOT NULL,
				recipient_id INTEGER NOT NULL,
				PRIMARY KEY (target_id, recipient_id),
				FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE,
				FOREIGN KEY(recipient_id) REFERENCES recipients(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_target_recipients_target ON target_recipients(target_id)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_target_recipients_recipient ON target_recipients(recipient_id)")

		# Downtime alerts table - tracks all downtime events with UUID
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS alerts (
				id TEXT PRIMARY KEY,
				target_id INTEGER NOT NULL,
				target_name TEXT NOT NULL,
				failed_service TEXT NOT NULL,
				status TEXT NOT NULL DEFAULT 'open',
				started_at timestamp NOT NULL,
				ended_at timestamp,
				duration_seconds INTEGER,
				recipients_notified TEXT,
				is_acknowledged INTEGER NOT NULL DEFAULT 0,
				acknowledged_by TEXT,
				acknowledged_at timestamp,
				created_at timestamp NOT NULL,
				updated_at timestamp NOT NULL,
				FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_target ON alerts(target_id)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_started_at ON alerts(started_at)")

		# Monitor quality time series (Quality Gate feature)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS monitor_quality (
				ts timestamp PRIMARY KEY,
				score REAL,
				median_latency_ms REAL,
				success_ratio REAL NOT NULL,
				state TEXT NOT NULL
			)
			"""
		)

		# Monitor quality latest snapshot (for fast gating decisions)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS monitor_quality_latest (
				id INTEGER PRIMARY KEY CHECK (id = 1),
				score REAL,
				state TEXT NOT NULL,
				updated_at timestamp NOT NULL
			)
			"""
		)

		# Report jobs for async PDF generation
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS report_jobs (
				job_id TEXT PRIMARY KEY,
				target_id INTEGER NOT NULL,
				status TEXT NOT NULL DEFAULT 'pending',
				progress INTEGER DEFAULT 0,
				filename TEXT,
				error TEXT,
				created_at timestamp NOT NULL,
				completed_at timestamp,
				expires_at timestamp,
				FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_report_jobs_status ON report_jobs(status)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_report_jobs_expires ON report_jobs(expires_at)")

		# POP locations for hostname-based geolocation (Single Source of Truth)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS pop_locations (
				id              INTEGER PRIMARY KEY AUTOINCREMENT,
				canonical_code  TEXT NOT NULL CHECK(length(canonical_code) <= 8),
				alias_code      TEXT NOT NULL CHECK(length(alias_code) <= 8),
				city            TEXT NOT NULL CHECK(length(city) <= 64),
				country         TEXT NOT NULL CHECK(length(country) <= 5),
				lat             REAL NOT NULL CHECK(lat BETWEEN -90 AND 90),
				lon             REAL NOT NULL CHECK(lon BETWEEN -180 AND 180),
				source          TEXT NOT NULL,
				confidence      INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100),
				priority        INTEGER NOT NULL CHECK(priority >= 0),
				created_at      TEXT NOT NULL,
				updated_at      TEXT NOT NULL,
				last_seen_at    TEXT,
				import_run_id   TEXT,
				retired         INTEGER NOT NULL DEFAULT 0 CHECK(retired IN (0, 1)),
				UNIQUE(alias_code, canonical_code)
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_alias ON pop_locations(alias_code)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_alias_prio ON pop_locations(alias_code, priority, confidence)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_canonical ON pop_locations(canonical_code)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_retired ON pop_locations(retired)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_source ON pop_locations(source)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_import_run ON pop_locations(import_run_id)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_source_retired ON pop_locations(source, retired)")

		# Candidates for learning new POP aliases (review queue)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS pop_alias_candidates (
				id              INTEGER PRIMARY KEY AUTOINCREMENT,
				alias_code      TEXT NOT NULL UNIQUE,
				canonical_guess TEXT,
				city_guess      TEXT,
				country_guess   TEXT,
				seen_count      INTEGER NOT NULL DEFAULT 1,
				last_seen       TEXT NOT NULL,
				reviewed        INTEGER NOT NULL DEFAULT 0,
				created_at      TEXT NOT NULL
			)
			"""
		)
	# Note: alias_code UNIQUE constraint creates an implicit index
		# POP import run tracking (delta-based refresh)
		conn.execute(
			"""
			CREATE TABLE IF NOT EXISTS pop_import_runs (
				run_id          TEXT PRIMARY KEY,
				source          TEXT NOT NULL,
				started_at      TEXT NOT NULL,
				finished_at     TEXT,
				status          TEXT NOT NULL DEFAULT 'running',
				records_processed INTEGER DEFAULT 0,
				records_inserted INTEGER DEFAULT 0,
				records_updated INTEGER DEFAULT 0,
				records_retired INTEGER DEFAULT 0,
				error_message   TEXT
			)
			"""
		)
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_runs_source ON pop_import_runs(source)")
		conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_runs_status ON pop_import_runs(status)")
	


def set_setting(conn: sqlite3.Connection, key: str, value: Any, *, auto_commit: bool = True) -> None:
	"""Persist a setting value and invalidate the in-process settings cache.
	
	Args:
		conn: Database connection
		key: Setting key
		value: Setting value (will be JSON-serialized)
		auto_commit: If True, wraps in transaction (default). Set False when calling within existing transaction.
	"""
	payload = json.dumps(value)
	
	def _execute():
		conn.execute(
			"""
			INSERT INTO settings(key, value, updated_at)
			VALUES(?, ?, ?)
			ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
			""",
			(key, payload, utcnow()),
		)
	
	if auto_commit:
		with transaction(conn):
			_execute()
	else:
		_execute()
	
	# Auto-invalidate cache for this key
	# Note: Use getattr for .value to handle both str and StrEnum (Cython rejects StrEnum subclass)
	from ..utils.settings_cache import invalidate_settings_cache
	key_str = getattr(key, 'value', key) if hasattr(key, 'value') else str(key)
	invalidate_settings_cache(key_str)


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
	"""Read a setting value from the database."""
	row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
	if not row:
		return default
	return json.loads(row["value"])


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
	"""Delete a setting key from the database."""
	with transaction(conn):
		conn.execute("DELETE FROM settings WHERE key=?", (key,))


# ─── Target Status Cache ────────────────────────────────────────────────────

# Allowed columns for update_target (validation allowlist)
_ALLOWED_TARGET_COLUMNS = frozenset({
	"name",
	"group_name",
	"is_enabled",
	"interval_seconds",
	"retention_days",
	"enable_ping",
	"enable_http_check",
	"enable_cert_expiration",
	"enable_tcp_connect",
	"host",
	"ping_host",
	"http_url",
	"http_username",
	"http_password",
	"http_prefer_head",
	"http_basic_auth_enabled",
	"http_success_codes",
	"http_port",
	"cert_host",
	"cert_port",
	"ignore_cert_errors",
	"tcp_host",
	"tcp_ports",
	"max_retries",
	"auto_close_alert",
	"ping_timeout_minutes",
	"sla_enabled",
	"sla_availability_pct",
	"sla_response_time_ms",
	"sla_ping_latency_ms",
})

def update_target_status(
	conn: sqlite3.Connection,
	target_id: int,
	*,
	ping_up: Optional[int] = None,
	ping_latency_ms: Optional[float] = None,
	http_up: Optional[int] = None,
	http_status: Optional[int] = None,
	http_latency_ms: Optional[float] = None,
	tcp_up: Optional[int] = None,
	tcp_latency_ms: Optional[float] = None,
	tcp_port_status: Optional[str] = None,
	cert_days_left: Optional[int] = None,
) -> None:
	"""
	Update target status cache after a check.
	
	Only updates the fields that are provided (not None).
	Calculates overall_status conservatively based on enabled availability checks.
	
	Optimized to minimize DB round-trips:
	  1. UPSERT status fields in a single statement
	  2. Fetch target config once (needed for overall_status calculation)
	  3. Single UPDATE for overall_status with RETURNING to avoid re-fetch
	"""
	now = utcnow()
	
	with transaction(conn):
		# Fetch target config inside transaction to prevent TOCTOU race
		target = get_target(conn, target_id)
		if not target:
			return  # Target doesn't exist, nothing to update
		
		# Build dynamic update based on what's provided
		updates = ["updated_at = ?"]
		params: list[Any] = [now]
		
		# Track new values for overall_status calculation (avoid re-fetch)
		new_ping_up = ping_up
		new_http_up = http_up
		new_tcp_up = tcp_up
		
		if ping_up is not None:
			updates.extend(["ping_up = ?", "ping_checked_at = ?"])
			params.extend([ping_up, now])
			if ping_latency_ms is not None:
				updates.append("ping_latency_ms = ?")
				params.append(ping_latency_ms)
	
		if http_up is not None:
			updates.extend(["http_up = ?", "http_checked_at = ?"])
			params.extend([http_up, now])
			if http_status is not None:
				updates.append("http_status = ?")
				params.append(http_status)
			if http_latency_ms is not None:
				updates.append("http_latency_ms = ?")
				params.append(http_latency_ms)
		
		if tcp_up is not None:
			updates.extend(["tcp_up = ?", "tcp_checked_at = ?"])
			params.extend([tcp_up, now])
			if tcp_latency_ms is not None:
				updates.append("tcp_latency_ms = ?")
				params.append(tcp_latency_ms)
		
		if tcp_port_status is not None:
			updates.append("tcp_port_status = ?")
			params.append(tcp_port_status)
		
		if cert_days_left is not None:
			updates.extend(["cert_days_left = ?", "cert_checked_at = ?"])
			params.extend([cert_days_left, now])
		
			# UPSERT: ensure row exists and update fields in one statement
			conn.execute(
			"""
			INSERT INTO target_status(target_id, overall_status, updated_at)
			VALUES (?, 'unknown', ?)
			ON CONFLICT(target_id) DO NOTHING
			""",
			(target_id, now),
		)
		
		# Update provided fields and fetch current status in one go
		if len(updates) > 1:  # More than just updated_at
			params.append(target_id)
			conn.execute(
				f"UPDATE target_status SET {', '.join(updates)} WHERE target_id = ?",
				params,
			)
		
		# Fetch current status row (only one SELECT, after updates applied)
		status_row = conn.execute(
			"SELECT overall_status, ping_up, http_up, tcp_up FROM target_status WHERE target_id = ?",
			(target_id,)
		).fetchone()
		
		if status_row:
			# Use new values if provided, otherwise use existing DB values
			effective_ping = new_ping_up if new_ping_up is not None else status_row["ping_up"]
			effective_http = new_http_up if new_http_up is not None else status_row["http_up"]
			effective_tcp = new_tcp_up if new_tcp_up is not None else status_row["tcp_up"]
			
			overall = _calculate_overall_status_fast(
				enable_http=_is_http_check_enabled(target),
				enable_ping=target["enable_ping"],
				enable_tcp=target["enable_tcp_connect"],
				http_up=effective_http,
				ping_up=effective_ping,
				tcp_up=effective_tcp,
			)
			
			was_up = status_row["overall_status"] in ("up", "unknown")
			is_down = overall == "down"
			
			update_sql = "UPDATE target_status SET overall_status = ?"
			update_params: list[Any] = [overall]
			
			# Track last_down_at transition
			if was_up and is_down:
				update_sql += ", last_down_at = ?"
				update_params.append(now)
			
			update_sql += " WHERE target_id = ?"
			update_params.append(target_id)
			conn.execute(update_sql, update_params)


def _is_http_check_enabled(target: sqlite3.Row) -> bool:
	"""Check if HTTP check is enabled."""
	return bool(target["enable_http_check"])


def _calculate_overall_status_fast(
	*,
	enable_http: bool,
	enable_ping: bool,
	enable_tcp: bool,
	http_up: Optional[int],
	ping_up: Optional[int],
	tcp_up: Optional[int],
) -> str:
	"""Calculate overall status from pre-fetched values (no DB access)."""
	checks: list[Optional[int]] = []
	if enable_http:
		checks.append(http_up)
	if enable_ping:
		checks.append(ping_up)
	if enable_tcp:
		checks.append(tcp_up)
	
	if not checks:
		return "unknown"
	if any(v is None for v in checks):
		return "unknown"
	if any(v != 1 for v in checks):
		return "down"
	return "up"


# DEPRECATED: _calculate_overall_status replaced by _calculate_overall_status_fast
# Keeping for reference only - no active call sites remain.
def _calculate_overall_status(target: sqlite3.Row, status: sqlite3.Row) -> str:
	"""DEPRECATED: Use _calculate_overall_status_fast instead.
	
	Calculate overall status based on enabled availability checks.
	Policy (documented): if any enabled check is down -> down.
	If any enabled check has no data -> unknown.
	"""
	checks: list[Optional[int]] = []
	if bool(target["enable_http_check"]):
		checks.append(status["http_up"])
	if target["enable_ping"]:
		checks.append(status["ping_up"])
	if target["enable_tcp_connect"]:
		checks.append(status["tcp_up"])

	if not checks:
		return "unknown"
	if any(v is None for v in checks):
		return "unknown"
	if any(v != 1 for v in checks):
		return "down"
	return "up"


def set_target_status_snapshot(
	conn: sqlite3.Connection,
	target_id: int,
	*,
	ping_up: Optional[int],
	ping_latency_ms: Optional[float],
	http_up: Optional[int],
	http_status: Optional[int],
	http_latency_ms: Optional[float],
	tcp_up: Optional[int],
	tcp_latency_ms: Optional[float],
	tcp_port_status: Optional[str] = None,
	cert_days_left: Optional[int],
	uptime_percent: Optional[float],
	uptime_percent_30d: Optional[float] = None,
	overall_status: str,
) -> None:
	"""Atomically update the full status snapshot for a target.

	This prevents mixed states from partial per-metric updates.
	The scheduler is the source of truth for the status semantics.
	"""
	now = utcnow()
	with transaction(conn):
		# Ensure row exists
		conn.execute(
			"""
			INSERT INTO target_status(target_id, overall_status, updated_at)
			VALUES (?, 'unknown', ?)
			ON CONFLICT(target_id) DO NOTHING
			""",
			(target_id, now),
		)

		prev = conn.execute(
			"SELECT overall_status, last_down_at, cert_days_left, cert_checked_at FROM target_status WHERE target_id = ?",
			(target_id,),
		).fetchone()
		prev_overall = prev["overall_status"] if prev else "unknown"
		last_down_at = prev["last_down_at"] if prev else None
		if prev_overall in ("up", "unknown") and overall_status == "down":
			last_down_at = now

		# Preserve last known cert_days_left if current check failed (None)
		# This prevents the badge from flickering to grey on transient failures
		effective_cert_days = cert_days_left
		effective_cert_checked_at = now if cert_days_left is not None else None
		if cert_days_left is None and prev:
			effective_cert_days = prev["cert_days_left"]
			effective_cert_checked_at = prev["cert_checked_at"]

		conn.execute(
			"""
			UPDATE target_status
			SET
				ping_up = ?,
				ping_latency_ms = ?,
				ping_checked_at = ?,
				http_up = ?,
				http_status = ?,
				http_latency_ms = ?,
				http_checked_at = ?,
				tcp_up = ?,
				tcp_latency_ms = ?,
				tcp_checked_at = ?,
				tcp_port_status = ?,
				cert_days_left = ?,
				cert_checked_at = ?,
				uptime_percent = ?,
				uptime_percent_30d = ?,
				overall_status = ?,
				last_down_at = ?,
				updated_at = ?
			WHERE target_id = ?
			""",
			(
				ping_up,
				ping_latency_ms,
				now if ping_up is not None else None,
				http_up,
				http_status,
				http_latency_ms,
				now if http_up is not None else None,
				tcp_up,
				tcp_latency_ms,
				now if tcp_up is not None else None,
				tcp_port_status,
				effective_cert_days,
				effective_cert_checked_at,
				uptime_percent,
				uptime_percent_30d,
				overall_status,
				last_down_at,
				now,
				target_id,
			),
		)


def get_target_status(conn: sqlite3.Connection, target_id: int) -> Optional[sqlite3.Row]:
	"""Get cached status for a target."""
	return conn.execute(
		"SELECT * FROM target_status WHERE target_id = ?", (target_id,)
	).fetchone()


def get_all_target_statuses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""Get cached status for all targets."""
	return list(conn.execute("SELECT * FROM target_status ORDER BY target_id").fetchall())


def delete_target_status(conn: sqlite3.Connection, target_id: int) -> None:
	"""Delete cached status for a target."""
	with transaction(conn):
		conn.execute("DELETE FROM target_status WHERE target_id = ?", (target_id,))


def cleanup_expired_tokens(conn: sqlite3.Connection) -> None:
	"""Delete expired authentication tokens."""
	with transaction(conn):
		conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (utcnow(),))


def create_user(
	conn: sqlite3.Connection,
	*,
	username: str,
	password_hash: str,
	is_admin: bool,
	can_close_alerts: bool = False,
) -> int:
	"""Create a user and return the new user id."""
	with transaction(conn):
		# Admins always have can_close_alerts permission
		can_close_value = 1 if is_admin else (1 if can_close_alerts else 0)
		cur = conn.execute(
			"INSERT INTO users(username, password_hash, is_admin, can_close_alerts, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?)",
			(username.lower(), password_hash, 1 if is_admin else 0, can_close_value, 1, utcnow()),
		)
		return int(cur.lastrowid)


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
	"""Get a user row by username (case-insensitive)."""
	return conn.execute("SELECT * FROM users WHERE username = ?", (username.lower(),)).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
	"""Get a user row by id."""
	return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
	"""Alias for get_user_by_id for consistency."""
	return get_user_by_id(conn, user_id)


def get_all_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""Get all users ordered by username."""
	return conn.execute("SELECT * FROM users ORDER BY username").fetchall()


def delete_user(conn: sqlite3.Connection, user_id: int) -> bool:
	"""Delete a user. Associated tokens and alert assignments are
	automatically removed via ON DELETE CASCADE.
	
	Returns True if user was deleted, False if not found.
	"""
	with transaction(conn):
		cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
		return cursor.rowcount > 0


def get_first_admin(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
	"""Get the first admin user (used as fallback when auth is disabled)."""
	return conn.execute("SELECT * FROM users WHERE is_admin = 1 AND is_active = 1 ORDER BY id LIMIT 1").fetchone()


def update_user(
	conn: sqlite3.Connection,
	user_id: int,
	*,
	username: str,
	is_admin: bool,
	can_close_alerts: bool = False,
) -> None:
	"""Update a user's basic fields."""
	with transaction(conn):
		# Admins always have can_close_alerts permission
		can_close_value = 1 if is_admin else (1 if can_close_alerts else 0)
		conn.execute(
			"UPDATE users SET username = ?, is_admin = ?, can_close_alerts = ? WHERE id = ?",
			(username.lower(), 1 if is_admin else 0, can_close_value, user_id),
		)


def set_user_active(conn: sqlite3.Connection, user_id: int, *, is_active: bool) -> None:
	"""Activate/deactivate a user."""
	with transaction(conn):
		conn.execute(
			"UPDATE users SET is_active = ? WHERE id = ?",
			(1 if is_active else 0, user_id),
		)


def update_user_password(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
	"""Update a user's password hash."""
	with transaction(conn):
		conn.execute(
			"UPDATE users SET password_hash = ? WHERE id = ?",
			(password_hash, user_id),
		)


def update_user_last_login(conn: sqlite3.Connection, user_id: int, ip_address: str | None = None) -> None:
	"""Update the last_login_at timestamp and IP address for a user."""
	with transaction(conn):
		conn.execute(
			"UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
			(utcnow(), ip_address, user_id),
		)


def issue_token(
	conn: sqlite3.Connection, 
	*, 
	user_id: int, 
	token_hash: str, 
	expires_at: datetime,
	max_expires_at: datetime
) -> None:
	"""Persist a new authentication token hash for a user."""
	with transaction(conn):
		conn.execute(
			"INSERT INTO auth_tokens(user_id, token_hash, expires_at, max_expires_at, created_at) VALUES(?, ?, ?, ?, ?)",
			(user_id, token_hash, expires_at, max_expires_at, utcnow()),
		)


def revoke_token(conn: sqlite3.Connection, *, token_hash: str) -> bool:
	"""Revoke a token by its hash.
	
	Returns True if a token was actually deleted, False if not found.
	"""
	with transaction(conn):
		cursor = conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
		return cursor.rowcount > 0


def revoke_tokens_for_user(conn: sqlite3.Connection, user_id: int) -> int:
	"""Revoke all tokens for a given user (e.g., after password change).
	
	Returns the number of tokens revoked.
	"""
	with transaction(conn):
		cursor = conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
		return cursor.rowcount


def revoke_all_tokens(conn: sqlite3.Connection) -> int:
	"""Revoke all authentication tokens (e.g., for security incident response).
	
	Returns the number of tokens revoked.
	"""
	with transaction(conn):
		cursor = conn.execute("DELETE FROM auth_tokens")
		return cursor.rowcount


def get_user_for_token(conn: sqlite3.Connection, *, token_hash: str) -> Optional[sqlite3.Row]:
	"""Resolve a token hash to a user row, returning None if invalid/expired.
	
	Implements sliding session renewal: On successful validation, extends expires_at
	by 6 hours (but never beyond max_expires_at which is 24h from creation).
	
	All operations run in a single transaction to prevent race conditions.
	"""
	now = utcnow()
	
	with transaction(conn):
		# Clean up expired tokens
		conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now,))
		
		# Single query: get user + token info
		row = conn.execute(
			"""
			SELECT u.*, t.max_expires_at, t.expires_at as token_expires_at
			FROM auth_tokens t
			JOIN users u ON u.id = t.user_id
			WHERE t.token_hash = ? 
			  AND t.expires_at > ? 
			  AND t.max_expires_at > ?
			  AND u.is_active = 1
			""",
			(token_hash, now, now),
		).fetchone()
		
		if row is None:
			return None
		
		# Sliding renewal within same transaction
		new_expires_at = min(
			now + timedelta(hours=6),
			row["max_expires_at"]
		)
		conn.execute(
			"UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ?",
			(new_expires_at, token_hash)
		)
	
	return row


# ─── Target Alert Users ─────────────────────────────────────────────────────────


def set_target_alert_users(conn: sqlite3.Connection, target_id: int, user_ids: list[int]) -> None:
	"""Set which users get alerts for a target (replaces existing)."""
	with transaction(conn):
		conn.execute("DELETE FROM target_alert_users WHERE target_id = ?", (target_id,))
		if user_ids:
			conn.executemany(
				"INSERT INTO target_alert_users(target_id, user_id) VALUES (?, ?)",
				[(target_id, user_id) for user_id in user_ids],
			)


def get_target_alert_users(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
	"""Get all users that should receive alerts for a target."""
	return list(
		conn.execute(
			"""
			SELECT u.* 
			FROM users u
			JOIN target_alert_users tau ON tau.user_id = u.id
			WHERE tau.target_id = ?
			ORDER BY u.username
			""",
			(target_id,),
		).fetchall()
	)


def get_user_alert_targets(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
	"""Get all targets that will alert a user."""
	return list(
		conn.execute(
			"""
			SELECT t.*
			FROM targets t
			JOIN target_alert_users tau ON tau.target_id = t.id
			WHERE tau.user_id = ?
			ORDER BY t.group_name, t.name
			""",
			(user_id,),
		).fetchall()
	)


def list_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""List targets ordered by group/name/id."""
	rows = conn.execute("SELECT * FROM targets ORDER BY group_name, name, id").fetchall()
	return list(rows)


def list_targets_paginated(
	conn: sqlite3.Connection,
	page: int = 1,
	page_size: int = 50
) -> tuple[list[sqlite3.Row], int]:
	"""List targets with pagination. Returns (rows, total_count)."""
	offset = (page - 1) * page_size
	
	# Get total count
	total = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
	
	# Get page items - sort groups first, NULL groups (ungrouped) last
	rows = conn.execute(
		"""
		SELECT * FROM targets
		ORDER BY CASE WHEN group_name IS NULL THEN 1 ELSE 0 END, group_name, name, id
		LIMIT ? OFFSET ?
		""",
		(page_size, offset)
	).fetchall()
	
	return list(rows), total


def get_target(conn: sqlite3.Connection, target_id: int) -> Optional[sqlite3.Row]:
	"""Get a target row by id."""
	return conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()


def create_target(conn: sqlite3.Connection, values: dict[str, Any]) -> int:
	"""Create a target and return the new target id."""
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
				"""
				INSERT INTO targets(
					name, group_name, is_enabled,
					interval_seconds, retention_days,
					enable_ping, enable_http_check, enable_cert_expiration, enable_tcp_connect,
					host, ping_host, http_url, http_username, http_password, http_prefer_head, http_basic_auth_enabled,
					http_success_codes, http_port,
					cert_host, cert_port, ignore_cert_errors, tcp_host, tcp_ports,
					max_retries, ping_timeout_minutes,
					auto_close_alert,
					sla_enabled, sla_availability_pct, sla_response_time_ms, sla_ping_latency_ms,
					created_at, updated_at
				) VALUES(
					?, ?, ?,
					?, ?,
					?, ?, ?, ?,
					?, ?, ?, ?, ?, ?, ?,
					?, ?,
					?, ?, ?, ?, ?,
					?, ?,
					?,
					?, ?, ?, ?,
					?, ?
				)
				""",
			(
				values["name"],
				values.get("group_name"),
				1 if values.get("is_enabled", True) else 0,
					int(values["interval_seconds"]),
					int(values["retention_days"]),
					1 if values.get("enable_ping", False) else 0,
					1 if values.get("enable_http_check", False) else 0,
					1 if values.get("enable_cert_expiration", False) else 0,
					1 if values.get("enable_tcp_connect", False) else 0,
					values["host"],
					values.get("ping_host"),
					values.get("http_url"),
					values.get("http_username"),
					values.get("http_password"),
					1 if values.get("http_prefer_head", True) else 0,
					1 if values.get("http_basic_auth_enabled", False) else 0,
					values.get("http_success_codes"),
					int(values["http_port"]) if values.get("http_port") else None,
					values.get("cert_host"),
					int(values["cert_port"]) if values.get("cert_port") else None,
					1 if values.get("ignore_cert_errors", False) else 0,
					values.get("tcp_host"),
					str(values.get("tcp_ports") or "80"),
					int(values.get("max_retries", 5)),
				int(values.get("ping_timeout_minutes", 5)),
				1 if values.get("auto_close_alert", True) else 0,
				1 if values.get("sla_enabled", False) else 0,
				values.get("sla_availability_pct"),
				values.get("sla_response_time_ms"),
				values.get("sla_ping_latency_ms"),
				now,
				now,
			),
		)
		return int(cur.lastrowid)


def update_target(conn: sqlite3.Connection, *, target_id: int, patch: dict[str, Any]) -> None:
	"""Apply a partial update to a target."""
	if not patch:
		return
	
	cols: list[str] = []
	params: list[Any] = []
	for k, v in patch.items():
		if k not in _ALLOWED_TARGET_COLUMNS:
			continue
		# Defense in depth: validate column name is a simple identifier
		if not k.isidentifier():
			raise ValueError(f"Invalid column name: {k!r}")
		if k in {"is_enabled", "enable_ping", "enable_http_check", "enable_cert_expiration", "enable_tcp_connect", "ignore_cert_errors", "sla_enabled", "http_prefer_head", "http_basic_auth_enabled", "auto_close_alert"} and v is not None:
			v = 1 if bool(v) else 0
		cols.append(f'"{k}" = ?')  # Quoted identifier
		params.append(v)
	cols.append("updated_at = ?")
	params.append(utcnow())
	params.append(target_id)
	with transaction(conn):
		conn.execute(f"UPDATE targets SET {', '.join(cols)} WHERE id = ?", params)


def delete_target(conn: sqlite3.Connection, target_id: int) -> None:
	"""Delete a target by id."""
	with transaction(conn):
		conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))


# ─────────────────────────────────────────────────────────────────────────────
# Groups CRUD
# ─────────────────────────────────────────────────────────────────────────────

def list_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""List all groups."""
	return conn.execute("SELECT * FROM groups ORDER BY name").fetchall()


def list_groups_with_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""List all groups with their target count (N+1 query optimization)."""
	sql = """
		SELECT g.*, COUNT(t.id) as target_count
		FROM groups g
		LEFT JOIN targets t ON t.group_name = g.name
		GROUP BY g.id
		ORDER BY g.name
	"""
	return conn.execute(sql).fetchall()


def get_group(conn: sqlite3.Connection, group_id: int) -> Optional[sqlite3.Row]:
	"""Get a group by id."""
	return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def get_group_by_name(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
	"""Get a group by name."""
	return conn.execute("SELECT * FROM groups WHERE name = ?", (name,)).fetchone()


def create_group(conn: sqlite3.Connection, *, name: str, description: Optional[str] = None) -> int:
	"""Create a group and return the new group id."""
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
			"INSERT INTO groups(name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
			(name, description, now, now),
		)
		return int(cur.lastrowid)


def update_group(conn: sqlite3.Connection, *, group_id: int, patch: dict[str, Any]) -> None:
	"""Apply a partial update to a group."""
	if not patch:
		return
	allowed = {"name", "description"}
	cols: list[str] = []
	params: list[Any] = []
	for k, v in patch.items():
		if k not in allowed:
			continue
		# Defense in depth: validate column name is a simple identifier
		if not k.isidentifier():
			raise ValueError(f"Invalid column name: {k!r}")
		cols.append(f'"{k}" = ?')  # Quoted identifier
		params.append(v)
	cols.append('"updated_at" = ?')
	params.append(utcnow())
	params.append(group_id)
	with transaction(conn):
		conn.execute(f"UPDATE groups SET {', '.join(cols)} WHERE id = ?", params)


def delete_group(conn: sqlite3.Connection, group_id: int) -> None:
	"""Delete a group by id."""
	with transaction(conn):
		conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))


def count_targets_in_group(conn: sqlite3.Connection, group_name: str) -> int:
	"""Count how many targets are assigned to the given group name."""
	row = conn.execute("SELECT COUNT(*) as cnt FROM targets WHERE group_name = ?", (group_name,)).fetchone()
	return int(row["cnt"]) if row else 0

# ---------------------------------------------------------------------------
# Login Attempts (Rate Limiting)
# ---------------------------------------------------------------------------

def get_login_attempt(conn: sqlite3.Connection, ip_address: str) -> Optional[sqlite3.Row]:
	"""Get login attempt record for an IP address."""
	return conn.execute(
		"SELECT * FROM login_attempts WHERE ip_address = ?",
		(ip_address,),
	).fetchone()


def record_failed_login(conn: sqlite3.Connection, ip_address: str) -> int:
	"""
	Record a failed login attempt and return the new failed count.
	
	Implements exponential backoff: lock_seconds = 2^failed_count (capped at 1 hour)
	Uses single atomic SQL statement with RETURNING for efficiency.
	"""
	now = utcnow()
	
	with transaction(conn):
		# First, get current count (if exists) to calculate correct locked_until
		row = conn.execute(
			"SELECT failed_count FROM login_attempts WHERE ip_address = ?",
			(ip_address,),
		).fetchone()
		
		new_count = (int(row["failed_count"]) + 1) if row else 1
		lock_seconds = min(2 ** new_count, 3600)
		locked_until = now + timedelta(seconds=lock_seconds)
		
		# Atomic upsert with correct locked_until from the start
		conn.execute(
			"""
			INSERT INTO login_attempts(ip_address, failed_count, last_attempt_at, locked_until, created_at)
			VALUES (?, ?, ?, ?, ?)
			ON CONFLICT(ip_address) DO UPDATE SET
				failed_count = ?,
				last_attempt_at = ?,
				locked_until = ?
			""",
			(ip_address, new_count, now, locked_until, now, new_count, now, locked_until),
		)
		return new_count


def clear_login_attempts(conn: sqlite3.Connection, ip_address: str) -> None:
	"""Clear login attempts after successful login."""
	with transaction(conn):
		conn.execute("DELETE FROM login_attempts WHERE ip_address = ?", (ip_address,))


def is_ip_locked(conn: sqlite3.Connection, ip_address: str) -> tuple[bool, Optional[int]]:
	"""
	Check if an IP is currently locked out.
	
	Returns: (is_locked, seconds_remaining)
	"""
	record = get_login_attempt(conn, ip_address)
	if not record or not record["locked_until"]:
		return False, None
	
	now = utcnow()
	locked_until = record["locked_until"]
	
	if locked_until > now:
		remaining = int((locked_until - now).total_seconds())
		return True, remaining
	
	return False, None


def cleanup_old_login_attempts(conn: sqlite3.Connection, max_age_hours: int = 24) -> int:
	"""
	Clean up old login attempt records.
	
	Removes records where the lock has expired and last attempt was > max_age_hours ago.
	Returns number of deleted records.
	"""
	cutoff = utcnow() - timedelta(hours=max_age_hours)
	with transaction(conn):
		sql = (
			"DELETE FROM login_attempts "
			"WHERE (locked_until IS NULL OR locked_until < ?) AND last_attempt_at < ?"
		)
		cur = conn.execute(sql, (utcnow(), cutoff))
		return cur.rowcount


# ─── Recipients ─────────────────────────────────────────────────────────────

def list_recipients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""List all alert recipients."""
	return list(conn.execute("SELECT * FROM recipients ORDER BY name").fetchall())


def get_recipient(conn: sqlite3.Connection, recipient_id: int) -> Optional[sqlite3.Row]:
	"""Get a single recipient by ID."""
	return conn.execute("SELECT * FROM recipients WHERE id = ?", (recipient_id,)).fetchone()


def get_recipient_by_phone(conn: sqlite3.Connection, phone: str) -> Optional[sqlite3.Row]:
	"""Get a recipient by phone number."""
	return conn.execute("SELECT * FROM recipients WHERE phone = ?", (phone,)).fetchone()


def create_recipient(conn: sqlite3.Connection, *, name: str, phone: str = None, email: str = None, is_enabled: bool = True) -> int:
	"""Create a new recipient and return its ID."""
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
			"""
			INSERT INTO recipients(name, phone, email, is_enabled, created_at, updated_at)
			VALUES (?, ?, ?, ?, ?, ?)
			""",
			(name, phone, email, 1 if is_enabled else 0, now, now),
		)
		return int(cur.lastrowid)


def update_recipient(conn: sqlite3.Connection, recipient_id: int, *, name: str = None, phone: str = None, email: str = None, is_enabled: bool = None, clear_phone: bool = False, clear_email: bool = False) -> None:
	"""Update a recipient.
	
	Args:
		recipient_id: ID of recipient to update
		name: New name (optional)
		phone: New phone number (optional)
		email: New email address (optional)
		is_enabled: New enabled status (optional)
		clear_phone: If True, set phone to NULL
		clear_email: If True, set email to NULL
	"""
	updates = ["updated_at = ?"]
	params: list[Any] = [utcnow()]
	
	if name is not None:
		updates.append("name = ?")
		params.append(name)
	if clear_phone:
		updates.append("phone = ?")
		params.append(None)  # Use NULL via parameter
	elif phone is not None:
		updates.append("phone = ?")
		params.append(phone)
	if clear_email:
		updates.append("email = ?")
		params.append(None)  # Use NULL via parameter
	elif email is not None:
		updates.append("email = ?")
		params.append(email)
	if is_enabled is not None:
		updates.append("is_enabled = ?")
		params.append(1 if is_enabled else 0)
	
	params.append(recipient_id)
	with transaction(conn):
		conn.execute(f"UPDATE recipients SET {', '.join(updates)} WHERE id = ?", params)


def delete_recipient(conn: sqlite3.Connection, recipient_id: int) -> None:
	"""Delete a recipient."""
	with transaction(conn):
		conn.execute("DELETE FROM recipients WHERE id = ?", (recipient_id,))


# ─── Target Recipients (Many-to-Many) ───────────────────────────────────────

def get_target_recipients(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
	"""Get all recipients assigned to a target."""
	return list(conn.execute(
		"""
		SELECT r.* FROM recipients r
		JOIN target_recipients tr ON r.id = tr.recipient_id
		WHERE tr.target_id = ?
		ORDER BY r.name
		""",
		(target_id,),
	).fetchall())


def get_target_recipient_ids(conn: sqlite3.Connection, target_id: int) -> list[int]:
	"""Get recipient IDs assigned to a target."""
	rows = conn.execute(
		"SELECT recipient_id FROM target_recipients WHERE target_id = ?",
		(target_id,),
	).fetchall()
	return [row["recipient_id"] for row in rows]


def set_target_recipients(conn: sqlite3.Connection, target_id: int, recipient_ids: list[int]) -> None:
	"""Set the recipients for a target (replaces existing assignments)."""
	with transaction(conn):
		conn.execute("DELETE FROM target_recipients WHERE target_id = ?", (target_id,))
		if recipient_ids:
			conn.executemany(
				"INSERT INTO target_recipients(target_id, recipient_id) VALUES (?, ?)",
				[(target_id, rid) for rid in recipient_ids],
			)


def add_target_recipient(conn: sqlite3.Connection, target_id: int, recipient_id: int) -> None:
	"""Add a recipient to a target."""
	with transaction(conn):
		conn.execute(
			"INSERT OR IGNORE INTO target_recipients(target_id, recipient_id) VALUES (?, ?)",
			(target_id, recipient_id),
		)


def remove_target_recipient(conn: sqlite3.Connection, target_id: int, recipient_id: int) -> None:
	"""Remove a recipient from a target."""
	with transaction(conn):
		conn.execute(
			"DELETE FROM target_recipients WHERE target_id = ? AND recipient_id = ?",
			(target_id, recipient_id),
		)


def get_recipient_targets(conn: sqlite3.Connection, recipient_id: int) -> list[sqlite3.Row]:
	"""Get all targets assigned to a recipient."""
	return list(conn.execute(
		"""
		SELECT t.id, t.name
		FROM targets t
		JOIN target_recipients tr ON t.id = tr.target_id
		WHERE tr.recipient_id = ?
		ORDER BY t.name
		""",
		(recipient_id,),
	).fetchall())


# ─── Alerts (Downtime Events) ───────────────────────────────────────────────

def create_alert(
	conn: sqlite3.Connection,
	*,
	alert_id: str,
	target_id: int,
	target_name: str,
	failed_service: str,
	started_at: datetime,
	recipients_notified: list[str],
) -> None:
	"""Create a new downtime alert record."""
	now = utcnow()
	recipients_json = json.dumps(recipients_notified)
	with transaction(conn):
		conn.execute(
			"""
			INSERT INTO alerts (
				id, target_id, target_name, failed_service, status,
				started_at, recipients_notified, created_at, updated_at
			) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
			""",
			(alert_id, target_id, target_name, failed_service, started_at, recipients_json, now, now),
		)


def resolve_alert(
	conn: sqlite3.Connection,
	alert_id: str,
	ended_at: datetime,
) -> None:
	"""Mark an alert as resolved and record the end time."""
	now = utcnow()
	with transaction(conn):
		# Get started_at to compute duration
		row = conn.execute("SELECT started_at FROM alerts WHERE id = ?", (alert_id,)).fetchone()
		if row:
			duration_seconds = int((ended_at - row["started_at"]).total_seconds())
			conn.execute(
				"""
				UPDATE alerts SET 
					status = 'resolved',
					ended_at = ?,
					duration_seconds = ?,
					updated_at = ?
				WHERE id = ?
				""",
				(ended_at, duration_seconds, now, alert_id),
			)


def get_open_alert_for_target(conn: sqlite3.Connection, target_id: int) -> Optional[sqlite3.Row]:
	"""Get the currently open alert for a target, if any."""
	return conn.execute(
		"SELECT * FROM alerts WHERE target_id = ? AND status = 'open' ORDER BY started_at DESC LIMIT 1",
		(target_id,),
	).fetchone()


def list_open_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
	"""List all currently open alerts.
	
	Returns:
		List of alert rows with columns: target_id, id, started_at, failed_service
	"""
	return conn.execute(
		"SELECT target_id, id, started_at, failed_service FROM alerts WHERE status = 'open'"
	).fetchall()


def acknowledge_alert(
	conn: sqlite3.Connection,
	alert_id: str,
	acknowledged_by: str,
) -> bool:
	"""Acknowledge (close) an alert. Sets status to resolved and records who closed it. Returns True if updated."""
	now = utcnow()
	with transaction(conn):
		# Get started_at to compute duration
		row = conn.execute("SELECT started_at FROM alerts WHERE id = ?", (alert_id,)).fetchone()
		duration_seconds = None
		if row and row["started_at"]:
			started_at = row["started_at"]
			# started_at should already be a datetime via converter; defensive check
			if isinstance(started_at, str):
				_log.warning("started_at arrived as str — converter may not be active")
				started_at = datetime.fromisoformat(started_at).replace(tzinfo=timezone.utc)
			duration_seconds = int((now - started_at).total_seconds())
		
		cur = conn.execute(
			"""
			UPDATE alerts SET 
				status = 'resolved',
				ended_at = COALESCE(ended_at, ?),
				duration_seconds = COALESCE(duration_seconds, ?),
				is_acknowledged = 1,
				acknowledged_by = ?,
				acknowledged_at = ?,
				updated_at = ?
			WHERE id = ? AND is_acknowledged = 0
			""",
			(now, duration_seconds, acknowledged_by, now, now, alert_id),
		)
		return cur.rowcount > 0


def acknowledge_all_open_alerts(
	conn: sqlite3.Connection,
	acknowledged_by: str,
) -> int:
	"""Acknowledge (close) all open alerts and return number of updated rows."""
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
			"""
			UPDATE alerts SET
				status = 'resolved',
				ended_at = COALESCE(ended_at, ?),
				duration_seconds = COALESCE(
					duration_seconds,
					CASE
						WHEN started_at IS NOT NULL THEN CAST((julianday(?) - julianday(started_at)) * 86400 AS INTEGER)
						ELSE NULL
					END
				),
				is_acknowledged = 1,
				acknowledged_by = ?,
				acknowledged_at = ?,
				updated_at = ?
			WHERE status = 'open' AND is_acknowledged = 0
			""",
			(now, now, acknowledged_by, now, now),
		)
		return cur.rowcount


def list_alerts(
	conn: sqlite3.Connection,
	*,
	target_id: Optional[int] = None,
	from_date: Optional[datetime] = None,
	to_date: Optional[datetime] = None,
	status: Optional[str] = None,
	page: int = 1,
	page_size: int = 50,
) -> tuple[list[sqlite3.Row], int]:
	"""
	List alerts with optional filters and pagination.
	Returns (items, total_count).
	"""
	conditions = []
	params: list[Any] = []
	
	if target_id is not None:
		conditions.append("target_id = ?")
		params.append(target_id)
	if from_date is not None:
		conditions.append("started_at >= ?")
		params.append(from_date)
	if to_date is not None:
		conditions.append("started_at <= ?")
		params.append(to_date)
	if status is not None:
		conditions.append("status = ?")
		params.append(status)
	
	where_clause = " AND ".join(conditions) if conditions else "1=1"
	
	# Count total
	count_row = conn.execute(
		f"SELECT COUNT(*) as cnt FROM alerts WHERE {where_clause}",
		params,
	).fetchone()
	total = count_row["cnt"] if count_row else 0
	
	# Fetch page
	offset = (page - 1) * page_size
	params_with_pagination = params + [page_size, offset]
	rows = conn.execute(
		f"""
		SELECT * FROM alerts 
		WHERE {where_clause}
		ORDER BY started_at DESC
		LIMIT ? OFFSET ?
		""",
		params_with_pagination,
	).fetchall()
	
	return list(rows), total


def get_alert(conn: sqlite3.Connection, alert_id: str) -> Optional[sqlite3.Row]:
	"""Get a single alert by ID."""
	return conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def get_alerts_for_target_in_range(
	conn: sqlite3.Connection,
	target_id: int,
	from_date: datetime,
	to_date: datetime,
) -> list[sqlite3.Row]:
	"""Get all resolved alerts for a target in a date range (for PDF reports)."""
	return list(conn.execute(
		"""
		SELECT * FROM alerts 
		WHERE target_id = ? 
			AND started_at >= ? 
			AND (ended_at <= ? OR ended_at IS NULL)
		ORDER BY started_at ASC
		""",
		(target_id, from_date, to_date),
	).fetchall())


def delete_all_alerts(conn: sqlite3.Connection) -> int:
	"""Delete all alerts from the database. Returns the number of deleted rows."""
	with transaction(conn):
		cur = conn.execute("DELETE FROM alerts")
		return cur.rowcount


def delete_alerts_by_target(conn: sqlite3.Connection, target_id: int) -> int:
	"""Delete all alerts (open and closed) for a specific target. Returns the number of deleted rows."""
	with transaction(conn):
		cur = conn.execute("DELETE FROM alerts WHERE target_id = ?", (target_id,))
		return cur.rowcount


# ---------------------------------------------------------------------------
# Monitor Quality (Quality Gate feature)
# ---------------------------------------------------------------------------

def insert_monitor_quality(
	conn: sqlite3.Connection,
	*,
	score: Optional[float],
	median_latency_ms: Optional[float],
	success_ratio: float,
	state: str,
) -> None:
	"""
	Insert a monitor quality measurement and update the latest snapshot.
	
	Args:
		score: Quality score (0-100), None if no connectivity
		median_latency_ms: Median RTT to probe targets
		success_ratio: Ratio of successful probes (0.0-1.0)
		state: Quality state ("ok", "degraded", "down")
	"""
	now = utcnow()
	
	with transaction(conn):
		conn.execute(
			"""
			INSERT INTO monitor_quality(ts, score, median_latency_ms, success_ratio, state)
			VALUES (?, ?, ?, ?, ?)
			""",
			(now, score, median_latency_ms, success_ratio, state),
		)
		
		conn.execute(
			"""
			INSERT INTO monitor_quality_latest(id, score, state, updated_at)
			VALUES (1, ?, ?, ?)
			ON CONFLICT(id) DO UPDATE SET
				score = excluded.score,
				state = excluded.state,
				updated_at = excluded.updated_at
			""",
			(score, state, now),
		)


def get_monitor_quality_state(conn: sqlite3.Connection) -> str:
	"""
	Get the current monitor quality state for gating decisions.
	
	Returns:
		"ok", "degraded", or "down" (defaults to "down" if no data)
	"""
	row = conn.execute(
		"SELECT state FROM monitor_quality_latest WHERE id = 1"
	).fetchone()
	return row["state"] if row else "down"


def get_monitor_quality_latest(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
	"""Get the full latest monitor quality snapshot."""
	return conn.execute(
		"SELECT * FROM monitor_quality_latest WHERE id = 1"
	).fetchone()


def query_monitor_quality(
	conn: sqlite3.Connection,
	*,
	since: Optional[datetime] = None,
	until: Optional[datetime] = None,
	limit: int = 1000,
) -> list[sqlite3.Row]:
	"""
	Query monitor quality time series.
	
	Args:
		since: Start of time range (inclusive)
		until: End of time range (inclusive)
		limit: Maximum number of rows to return
	
	Returns:
		List of monitor quality rows, ordered by timestamp ascending
	"""
	conditions = []
	params: list[Any] = []
	
	if since:
		conditions.append("ts >= ?")
		params.append(since)
	if until:
		conditions.append("ts <= ?")
		params.append(until)
	
	where = " AND ".join(conditions) if conditions else "1=1"
	params.append(limit)
	
	return list(conn.execute(
		f"""
		SELECT * FROM monitor_quality
		WHERE {where}
		ORDER BY ts ASC
		LIMIT ?
		""",
		params,
	).fetchall())


def prune_monitor_quality(conn: sqlite3.Connection, retention_days: int = 30) -> int:
	"""
	Prune old monitor quality records.
	
	Args:
		retention_days: Number of days to keep
	
	Returns:
		Number of deleted rows
	"""
	cutoff = utcnow() - timedelta(days=retention_days)
	with transaction(conn):
		cur = conn.execute(
			"DELETE FROM monitor_quality WHERE ts < ?",
			(cutoff,),
		)
		return cur.rowcount


# ─── Report Jobs (Async PDF Generation) ────────────────────────────────────


def create_report_job(
	conn: sqlite3.Connection,
	job_id: str,
	target_id: int,
	expires_hours: int = 1,
) -> None:
	"""Create a new pending report job."""
	now = utcnow()
	expires_at = now + timedelta(hours=expires_hours)
	with transaction(conn):
		conn.execute(
			"""
			INSERT INTO report_jobs(job_id, target_id, status, progress, created_at, expires_at)
			VALUES(?, ?, 'pending', 0, ?, ?)
			""",
			(job_id, target_id, now, expires_at),
		)


def get_report_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
	"""Get a report job by ID."""
	return conn.execute(
		"SELECT * FROM report_jobs WHERE job_id = ?",
		(job_id,),
	).fetchone()


def update_report_job(
	conn: sqlite3.Connection,
	job_id: str,
	*,
	status: Optional[str] = None,
	progress: Optional[int] = None,
	filename: Optional[str] = None,
	error: Optional[str] = None,
	completed: bool = False,
) -> None:
	"""Update report job fields."""
	updates = []
	params: list[Any] = []
	
	if status is not None:
		updates.append("status = ?")
		params.append(status)
	if progress is not None:
		updates.append("progress = ?")
		params.append(progress)
	if filename is not None:
		updates.append("filename = ?")
		params.append(filename)
	if error is not None:
		updates.append("error = ?")
		params.append(error)
	if completed:
		updates.append("completed_at = ?")
		params.append(utcnow())
	
	if not updates:
		return
	
	params.append(job_id)
	with transaction(conn):
		conn.execute(
			f"UPDATE report_jobs SET {', '.join(updates)} WHERE job_id = ?",
			params,
		)


def get_expired_job_ids(conn: sqlite3.Connection, now: datetime) -> list[str]:
	"""Get list of expired job IDs for cleanup (DAL abstraction)."""
	rows = conn.execute(
		"SELECT job_id FROM report_jobs WHERE expires_at < ?",
		(now,),
	).fetchall()
	return [row["job_id"] for row in rows]


def prune_expired_report_jobs(conn: sqlite3.Connection) -> int:
	"""Delete expired report jobs and their files."""
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
			"DELETE FROM report_jobs WHERE expires_at < ?",
			(now,),
		)
		return cur.rowcount


# ─── Scheduler Locking (Task 1: Idempotency) ────────────────────────────────


def register_scheduler_run(conn: sqlite3.Connection, run_id: str) -> None:
	"""Register a new scheduler run for idempotency tracking."""
	now = utcnow()
	with transaction(conn):
		conn.execute(
			"INSERT INTO scheduler_runs (run_id, started_at, heartbeat_at) VALUES (?, ?, ?)",
			(run_id, now, now),
		)


def update_scheduler_heartbeat(conn: sqlite3.Connection, run_id: str) -> None:
	"""Update scheduler heartbeat to prevent lock expiry."""
	now = utcnow()
	with transaction(conn):
		conn.execute(
			"UPDATE scheduler_runs SET heartbeat_at = ? WHERE run_id = ?",
			(now, run_id),
		)


def cleanup_stale_scheduler_runs(conn: sqlite3.Connection, timeout_seconds: int = 300) -> int:
	"""Remove scheduler runs that haven't heartbeated recently."""
	cutoff = utcnow() - timedelta(seconds=timeout_seconds)
	with transaction(conn):
		cur = conn.execute(
			"DELETE FROM scheduler_runs WHERE heartbeat_at < ?",
			(cutoff,),
		)
		return cur.rowcount


def try_lock_target(
	conn: sqlite3.Connection,
	target_id: int,
	run_id: str,
	ttl_seconds: int,
) -> bool:
	"""
	Try to acquire lock for target check.
	
	Returns True if lock acquired, False if already locked by another run.
	Uses INSERT OR REPLACE with expiry check for atomic locking.
	"""
	now = utcnow()
	expires_at = now + timedelta(seconds=ttl_seconds)
	
	with transaction(conn):
		# Clean up expired locks first
		conn.execute(
			"DELETE FROM target_locks WHERE expires_at < ?",
			(now,),
		)
		
		# Check if already locked by different run
		existing = conn.execute(
			"SELECT run_id FROM target_locks WHERE target_id = ? AND expires_at >= ?",
			(target_id, now),
		).fetchone()
		
		if existing and existing[0] != run_id:
			# Locked by another run
			return False
		
		# Acquire or refresh lock
		conn.execute(
			"INSERT OR REPLACE INTO target_locks (target_id, run_id, locked_at, expires_at) VALUES (?, ?, ?, ?)",
			(target_id, run_id, now, expires_at),
		)
		return True


def release_target_lock(conn: sqlite3.Connection, target_id: int, run_id: str) -> None:
	"""Release target lock after check completes."""
	with transaction(conn):
		conn.execute(
			"DELETE FROM target_locks WHERE target_id = ? AND run_id = ?",
			(target_id, run_id),
		)


def mark_stale_report_jobs_failed(conn: sqlite3.Connection) -> int:
	"""Mark any 'pending' or 'processing' jobs as failed (called on startup).
	
	Jobs in these states after a restart are orphaned because the in-memory
	queue was lost. Mark them as failed so the frontend stops polling.
	"""
	now = utcnow()
	with transaction(conn):
		cur = conn.execute(
			"""
			UPDATE report_jobs
			SET status = 'failed',
			    progress = 100,
			    error = 'Server restarted before job completed',
			    completed_at = ?
			WHERE status IN ('pending', 'processing')
			""",
			(now,),
		)
		return cur.rowcount
