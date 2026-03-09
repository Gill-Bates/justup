#!/usr/bin/env python3
#
# app/utils/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Configuration loading and app-level defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


class ConfigValidationError(Exception):
	"""Raised when critical configuration is missing or invalid."""
	pass


@dataclass(frozen=True)
class Config:
	"""Resolved runtime configuration derived from env and defaults."""
	base_dir: Path
	db_path: Path
	tsdb_dir: Path
	data_dir: Path
	host: str = "0.0.0.0"
	port: int = 8080
	log_level: str = "INFO"


# Application constants (DB settings are authoritative once stored)
DEFAULT_RETENTION_DAYS: int = 365
MONITOR_REFRESH_SECONDS: int = 30
MONITOR_TICK_SECONDS: float = 1.0


def _strip_quotes(value: str) -> str:
	value = value.strip()
	if (len(value) >= 2) and ((value[0] == value[-1]) and value[0] in ('"', "'")):
		return value[1:-1]
	return value


def load_dotenv(dotenv_path: Path | None = None) -> None:
	"""Load simple KEY=VALUE pairs from .env.

	Behavior:
	- Ignores blank lines and comments (# ...)
	- Does not override already-set environment variables
	"""
	# New location is in app/utils/, project root is 2 levels up
	project_root = Path(__file__).resolve().parents[2]
	dotenv_path = dotenv_path or (project_root / ".env")
	if not dotenv_path.exists():
		return
	for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#"):
			continue
		if "=" not in line:
			continue
		key, value = line.split("=", 1)
		key = key.strip()
		
		# Robust inline comment handling (only strip if preceded by space to avoid breaking URLs)
		if " #" in value:
			value = value.split(" #", 1)[0]
			
		value = _strip_quotes(value.strip())
		if not key:
			continue
		os.environ.setdefault(key, value)


def load_config() -> Config:
	"""Load configuration from environment variables (optionally via .env)."""
	load_dotenv()
	project_root = Path(__file__).resolve().parents[2]
	base_dir = project_root
	
	# Data path: use JUSTUP_DATA_DIR env var, or default to <project_root>/data
	data_dir = Path(os.getenv("JUSTUP_DATA_DIR", str(project_root / "data")))
	db_path = (data_dir / "sqlite" / "app.sqlite3").resolve()
	tsdb_dir = (data_dir / "tsdb").resolve()

	# Self-healing: Ensure directories exist
	try:
		data_dir.mkdir(parents=True, exist_ok=True)
		(data_dir / "sqlite").mkdir(parents=True, exist_ok=True)
		tsdb_dir.mkdir(parents=True, exist_ok=True)
	except OSError:
		# e.g. permission error, ignored during config load, will fail later
		pass

	# Validate log level
	allowed_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
	log_level = os.getenv("LOG_LEVEL", "INFO").upper()
	if log_level not in allowed_levels:
		log_level = "INFO"

	# Network binding
	host = os.getenv("JUSTUP_HOST", "0.0.0.0")
	try:
		port = int(os.getenv("JUSTUP_PORT", "8080"))
	except ValueError:
		port = 8080

	return Config(
		base_dir=base_dir,
		data_dir=data_dir,
		db_path=db_path,
		tsdb_dir=tsdb_dir,
		host=host,
		port=port,
		log_level=log_level,
	)


# -----------------------------------------------------------------------------
# Backwards-compatible module constants
# -----------------------------------------------------------------------------
#
# Some older code paths expect these names to exist at import time (e.g. DATA_DIR).
# The application uses `load_config()` / `app.state.*` as the authoritative source,
# but keeping these aliases avoids runtime ImportError after upgrades.
load_dotenv()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("JUSTUP_DATA_DIR", str(_PROJECT_ROOT / "data")))
DB_PATH = (DATA_DIR / "sqlite" / "app.sqlite3").resolve()
TSDB_DIR = (DATA_DIR / "tsdb").resolve()


def validate_runtime_config() -> None:
	"""
	Validate runtime configuration at application startup.
	
	Checks critical environment variables that are required for core functionality.
	This is intentionally minimal - only validates encryption key (done in crypto module).
	
	Optional features (Signal, provider lookup, etc.) are configured at runtime
	via settings DB or auto-detected from signal-cli state. No startup warnings needed.
	
	Raises:
		ConfigValidationError: If critical configuration is missing
	"""
	# Encryption key is already validated by crypto module on import
	# No additional validation needed - keep startup logs clean
	_log.info("Runtime configuration validated")
