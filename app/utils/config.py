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

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigValidationError(Exception):
    """Raised when critical configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration derived from env and defaults."""

    base_dir: Path
    db_path: Path
    tsdb_dir: Path
    data_dir: Path
    host: str = "0.0.0.0"  # noqa: S104 - documented default bind for a containerised server
    port: int = 8000
    log_level: str = "INFO"


MONITOR_REFRESH_SECONDS: int = 30


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if (len(value) >= 2) and ((value[0] == value[-1]) and value[0] in ('"', "'")):
        return value[1:-1]
    return value


def _parse_value(raw: str) -> str:
    """Parse a .env value: quoted values keep ``#``; unquoted ones end at `` #``."""
    text = raw.lstrip()
    quote = text[:1]
    if quote in ('"', "'"):
        chars: list[str] = []
        i = 1
        while i < len(text):
            ch = text[i]
            if quote == '"' and ch == "\\" and text[i + 1 : i + 2] in ('"', "\\"):
                chars.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                return "".join(chars)
            chars.append(ch)
            i += 1
    # Unquoted (or unterminated quote): strip an inline comment only when preceded by a space
    if " #" in raw:
        raw = raw.split(" #", 1)[0]
    return _strip_quotes(raw.strip())


def load_dotenv(dotenv_path: Path | None = None) -> None:
    """Load simple KEY=VALUE pairs from .env.

    Behavior:
    - Ignores blank lines and comments (# ...)
    - Does not override already-set environment variables
    """
    dotenv_path = dotenv_path or (_PROJECT_ROOT / ".env")
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

        value = _parse_value(value)
        if not key:
            continue
        os.environ.setdefault(key, value)


def load_config() -> Config:
    """Load configuration from environment variables (optionally via .env)."""
    load_dotenv()

    # Data path: use JUSTUP_DATA_DIR env var, or default to <project_root>/data
    data_dir = Path(os.getenv("JUSTUP_DATA_DIR", str(_PROJECT_ROOT / "data")))
    db_path = (data_dir / "sqlite" / "justup.db").resolve()
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
    host = os.getenv("JUSTUP_HOST", "0.0.0.0")  # noqa: S104 - documented default bind for a containerised server
    try:
        port = int(os.getenv("JUSTUP_PORT", "8000"))
    except ValueError:
        port = 8000

    return Config(
        base_dir=_PROJECT_ROOT,
        data_dir=data_dir,
        db_path=db_path,
        tsdb_dir=tsdb_dir,
        host=host,
        port=port,
        log_level=log_level,
    )


# Import-time paths for code without app state (justup-cli, bootstrap); the running
# app uses `load_config()` / `app.state.*`.
load_dotenv()
DATA_DIR = Path(os.getenv("JUSTUP_DATA_DIR", str(_PROJECT_ROOT / "data")))
DB_PATH = (DATA_DIR / "sqlite" / "justup.db").resolve()


def validate_runtime_config() -> None:
    """Validate critical runtime configuration (currently the encryption key).

    Optional features (Signal, provider lookup, etc.) are configured at runtime via
    settings DB or auto-detected, so they are not checked here.

    Raises:
        MissingEncryptionKeyError: If SECRET_KEY(S) is missing or not a valid Fernet key.
            The message never contains key material.
    """
    # Local import avoids a config <-> crypto import cycle.
    from .crypto import validate_encryption_key

    validate_encryption_key()
    _log.info("Runtime configuration validated")
