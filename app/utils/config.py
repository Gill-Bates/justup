#!/usr/bin/env python3
#
# app/utils/config.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Configuration loading and app-level defaults."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
	"""Resolved runtime configuration derived from env and defaults."""
	base_dir: Path
	db_path: Path
	tsdb_dir: Path
	data_dir: Path
	log_level: str = "INFO"
	secret_key: str = ""


_config: Config | None = None
_config_lock = threading.Lock()


def _parse_value(raw: str) -> str:
	raw = raw.strip()
	if raw and raw[0] in ('"', "'"):
		quote = raw[0]
		end = raw.find(quote, 1)
		if end != -1:
			return raw[1:end]
	if " #" in raw:
		raw = raw.split(" #", 1)[0]
	return raw.strip()


def load_dotenv(dotenv_path: Path | None = None) -> None:
	project_root = Path(__file__).resolve().parents[2]
	dotenv_path = dotenv_path or (project_root / "settings.env")
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
		if key.startswith("export "):
			key = key[7:].strip()
		value = _parse_value(value)
		if not key:
			continue
		os.environ.setdefault(key, value)


def load_config() -> Config:
	global _config
	with _config_lock:
		if _config is not None:
			return _config

		load_dotenv()
		project_root = Path(__file__).resolve().parents[2]
		base_dir = project_root

		data_dir = (project_root / "data").resolve()
		db_path = (data_dir / "justup.db").resolve()
		tsdb_dir = (data_dir / "tsdb").resolve()

		log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
		secret_key = os.environ.get("JUSTUP_SECRET_KEY", "")

		if not secret_key:
			_log.warning(
				"JUSTUP_SECRET_KEY is not set. Generate one with: head -c 32 /dev/urandom | base64"
			)

		_config = Config(
			base_dir=base_dir,
			db_path=db_path,
			tsdb_dir=tsdb_dir,
			data_dir=data_dir,
			log_level=log_level,
			secret_key=secret_key,
		)
		return _config


def get_config() -> Config:
	"""Get the cached config, loading it if needed."""
	if _config is None:
		return load_config()
	return _config
