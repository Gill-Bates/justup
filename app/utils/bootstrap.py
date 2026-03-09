#!/usr/bin/env python3
#
# app/utils/bootstrap.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Database bootstrap helpers (schema init, default settings, default admin)."""

from __future__ import annotations

import hashlib
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

import os

from ..db import sqlite as sqlite_db
from ..models.users import hash_password
from .migration import run_migrations

_log = logging.getLogger(__name__)

# GeoLite2 database download URL (P3TERX mirror, updated regularly)
GEOIP_DOWNLOAD_URL = "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-City.mmdb"
# Database filename
_GEOIP_DB_NAME = "GeoLite2-City.mmdb"

# MMDB format magic bytes: first 16 bytes should contain metadata marker
# The file ends with a metadata section starting with \xab\xcd\xefMaxMind.com
MMDB_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"


def get_geoip_build_info(data_dir: Path | None = None) -> tuple[str, int] | None:
	"""
	Get GeoIP database type and build epoch.
	
	Returns:
		Tuple of (database_type, build_epoch) or None if unavailable
	"""
	try:
		import geoip2.database
		
		if data_dir is None:
			data_dir = Path(os.environ.get("DATA_DIR", "./data"))
		
		db_path = data_dir / _GEOIP_DB_NAME
		if not db_path.exists():
			return None
		
		with geoip2.database.Reader(str(db_path)) as reader:
			return (reader.metadata().database_type, reader.metadata().build_epoch)
	except Exception:
		return None


def _verify_mmdb(file_path: Path) -> bool:
	"""
	Verify that a file is a valid MaxMind MMDB database.
	
	Checks:
	1. File exists and has reasonable size (>100KB for City DB)
	2. Contains the MMDB metadata marker
	3. Can be opened by geoip2 library (if available)
	"""
	if not file_path.exists():
		return False
	
	file_size = file_path.stat().st_size
	# City DB is typically >50MB; strict minimum for security
	# Only allow override in test environments to prevent bypass
	if os.getenv("TESTING") == "1" or os.getenv("PYTEST_CURRENT_TEST") is not None:
		_min_size = int(os.getenv("UPMON_MIN_GEOIP_SIZE", 1_000_000))  # 1MB for tests
	else:
		_min_size = 10_000_000  # 10MB minimum for production (City DB is ~60MB)
	
	if file_size < _min_size:
		_log.warning("GeoIP database too small: %d bytes (minimum: %d)", file_size, _min_size)
		return False
	
	# Check for MMDB metadata marker in last 128KB of file
	try:
		with open(file_path, "rb") as f:
			# Seek to last 128KB and search for marker
			f.seek(max(0, file_size - 131072))
			tail = f.read()
			if MMDB_METADATA_MARKER not in tail:
				_log.warning("GeoIP database missing MMDB metadata marker")
				return False
	except Exception as e:
		_log.warning(f"Failed to read GeoIP database: {e}")
		return False
	
	# Try to open with geoip2 library
	try:
		import geoip2.database
		with geoip2.database.Reader(str(file_path)) as reader:
			# Verify it's a City database
			if "City" not in reader.metadata().database_type:
				_log.warning("GeoIP database is not City type: %s", reader.metadata().database_type)
				return False
			_log.info("GeoIP database verified: %s, build %d",
					  reader.metadata().database_type, reader.metadata().build_epoch)
	except ImportError:
		_log.debug("geoip2 not installed, skipping deep verification")
	except Exception as e:
		_log.warning("GeoIP database verification failed: %s", e)
		return False
	
	return True


def _check_remote_size() -> Optional[int]:
	"""
	Check the remote file size via HEAD request.
	
	Returns the Content-Length or None if unavailable.
	"""
	try:
		req = Request(
			GEOIP_DOWNLOAD_URL,
			method="HEAD",
			headers={"User-Agent": "justUp-Monitor/1.0"}
		)
		with urlopen(req, timeout=30) as response:
			content_length = response.headers.get("Content-Length")
			if content_length:
				return int(content_length)
	except Exception as e:
			_log.debug("Failed to check remote GeoIP size: %s", e)
	return None


def _download_geoip_db(target_path: Path, force: bool = False) -> Optional[Path]:
	"""
	Download GeoLite2-City database if not present, invalid, or outdated.
	
	Args:
		target_path: Where to save the database.
		force: If True, skip size check and always download if remote differs.
	
	Returns the path to the database if successful, None otherwise.
	"""
	geoip_path = target_path
	local_exists = geoip_path.exists()
	local_size = geoip_path.stat().st_size if local_exists else 0
	
	# Check if valid database already exists
	if local_exists and _verify_mmdb(geoip_path):
		if not force:
			# Check remote size for updates
			remote_size = _check_remote_size()
			if remote_size and remote_size == local_size:
				_log.debug("GeoIP database up-to-date: %s", geoip_path)
				return geoip_path
			elif remote_size and remote_size != local_size:
				_log.info("GeoIP database size changed (local: %s, remote: %s), updating...", f"{local_size:,}", f"{remote_size:,}")
			else:
				# Couldn't check remote, keep existing
				_log.debug("GeoIP database present, remote check skipped: %s", geoip_path)
				return geoip_path
		else:
			_log.info("GeoIP database force update requested")
	elif local_exists:
		_log.warning("GeoIP database exists but failed verification, re-downloading...")
	
	_log.info("Downloading GeoIP database from %s...", GEOIP_DOWNLOAD_URL)
	
	# Ensure target directory exists
	geoip_path.parent.mkdir(parents=True, exist_ok=True)
	
	# Download to temp file first, then verify before moving
	temp_fd = None
	temp_path = None
	try:
		# Create temp file in target directory (same filesystem = atomic rename)
		temp_fd, temp_path_str = tempfile.mkstemp(
			suffix=".mmdb.tmp",
			prefix="geoip_",
			dir=str(geoip_path.parent)
		)
		temp_path = Path(temp_path_str)
		
		# Close fd immediately, open by path to prevent fd leak
		os.close(temp_fd)
		temp_fd = None
		
		# Download with timeout and user agent
		req = Request(
			GEOIP_DOWNLOAD_URL,
			headers={"User-Agent": "justUp-Monitor/1.0"}
		)
		
		with urlopen(req, timeout=120) as response:
			# Check content type if available
			content_type = response.headers.get("Content-Type", "")
			if "text/html" in content_type.lower():
				_log.error("GeoIP download returned HTML instead of binary data")
				return None
			
			# Download with progress logging
			total_size = int(response.headers.get("Content-Length", 0))
			downloaded = 0
			hasher = hashlib.sha256()
			
			with open(temp_path, "wb") as f:
				while True:
					chunk = response.read(65536)
					if not chunk:
						break
					f.write(chunk)
					hasher.update(chunk)
					downloaded += len(chunk)
			
			_log.info("Downloaded %s bytes, SHA256: %s...", f"{downloaded:,}", hasher.hexdigest()[:16])
		
		# Verify the downloaded file
		if not _verify_mmdb(temp_path):
			_log.error("Downloaded GeoIP database failed verification - possibly corrupted or malicious")
			return None
		
		# Atomic rename (same filesystem guarantees atomicity)
		temp_path.rename(geoip_path)
		temp_path = None  # Renamed successfully, don't try to cleanup
		_log.info("GeoIP database installed: %s", geoip_path)
		return geoip_path
		
	except URLError as e:
		_log.warning("Failed to download GeoIP database: %s", e)
		return None
	except Exception as e:
		_log.error("GeoIP database download error: %s", e)
		return None
	finally:
		# Cleanup: close fd if still open, remove temp file on failure
		if temp_fd is not None:
			try:
				os.close(temp_fd)
			except OSError:
				pass
		if temp_path and temp_path.exists():
			try:
				temp_path.unlink()
			except OSError:
				pass
def _get_default_settings() -> dict:
	"""Build default settings from current environment. Called at bootstrap time."""
	return {
		"signal_api_base_url": os.getenv("UPMON_SIGNAL_API_BASE_URL") or None,
		"signal_api_recipient": os.getenv("UPMON_SIGNAL_API_RECIPIENT") or None,
		"purge_enabled": os.getenv("UPMON_PURGE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
		"pdf_page_size": os.getenv("UPMON_PDF_PAGE_SIZE", "a4").strip().lower(),
		"metrics_timeout_minutes": int(os.getenv("UPMON_METRICS_TIMEOUT_MINUTES", "5")),
		"geoip_auto_update": os.getenv("UPMON_GEOIP_AUTO_UPDATE", "true").strip().lower() in {"1", "true", "yes", "on"},
	}


def bootstrap(db_path: Path, tsdb_dir: Path, data_dir: Optional[Path] = None) -> None:
	"""Initialize the database schema and seed default settings/users."""
	conn = sqlite_db.connect(db_path)
	# Initialize before try to prevent NameError if data_dir is None
	geoip_auto_update = None
	
	try:
		# Detect fresh installation: check if users table exists before schema init
		# This allows us to skip migration execution on new installs
		cursor = conn.execute(
			"SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
		)
		is_fresh_install = cursor.fetchone() is None
		
		if is_fresh_install:
			_log.info("Fresh installation detected - schema will be initialized")
		
		sqlite_db.init_schema(conn)
		
		# Run database migrations
		# For fresh installs: skip execution, just mark all as applied
		# For upgrades: execute pending migrations
		run_migrations(conn, fresh_install=is_fresh_install)

		# Settings defaults (evaluate env vars at runtime, not import time)
		for k, v in _get_default_settings().items():
			if sqlite_db.get_setting(conn, k, None) is None:
				sqlite_db.set_setting(conn, k, v)

		# Default admin user (with secure random password)
		row = conn.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
		if row is None:
			# Use secure random password unless explicitly set via env
			import secrets
			initial_password = os.environ.get("UPMON_ADMIN_PASSWORD")
			if not initial_password:
				# 16 bytes = 128 bits entropy, ~22 character password
				initial_password = secrets.token_urlsafe(16)
				# Print to stdout only (not logs) to avoid credential leakage in log aggregators
				print(
					f"\n{'═' * 55}\n"
					f"  Default admin account created.\n"
					f"  Username: admin\n"
					f"  Password: {initial_password}\n"
					f"  ⚠️  Change this password immediately!\n"
					f"  Set UPMON_ADMIN_PASSWORD env var to customize.\n"
					f"{'═' * 55}\n",
					flush=True
				)
				_log.warning("Default admin account created with auto-generated password (printed to stdout only)")
			
			conn.execute(
				"INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
				("admin", hash_password(initial_password), 1, sqlite_db.utcnow()),
			)
			conn.commit()
		else:
			# Admin user already exists - warn if ENV is set
			if os.environ.get("UPMON_ADMIN_PASSWORD"):
				_log.warning(
					"UPMON_ADMIN_PASSWORD env var is set, but admin user already exists. "
					"Ignoring ENV. Delete the database or reset password via UI to apply."
				)

		# TSDB init (folder creation)
		from ..db import tsdb as tsdb_db

		tsdb_db.init_tsdb(tsdb_dir)
		
		# Read GeoIP auto-update setting before closing connection
		if data_dir is not None:
			raw = sqlite_db.get_setting(conn, "geoip_auto_update", True)
			# Normalize string/bool/int to bool (handles "true", "false", 1, 0)
			geoip_auto_update = str(raw).strip().lower() in {"1", "true", "yes", "on"}
	finally:
		conn.close()
	
	# GeoIP download AFTER database connection is closed (avoid lock contention)
	if data_dir is not None and geoip_auto_update is not None:
		geoip_path = data_dir / _GEOIP_DB_NAME
		if geoip_auto_update:
			_download_geoip_db(target_path=geoip_path, force=False)
		elif not geoip_path.exists():
			# Always download if missing, regardless of setting
			_log.info("GeoIP database missing, downloading (auto-update disabled, initial download only)...")
			_download_geoip_db(target_path=geoip_path, force=False)

