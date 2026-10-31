#!/usr/bin/env python3
#
# app/utils/bootstrap.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Database bootstrap helpers (schema init, default settings, default admin)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..db import sqlite as sqlite_db
from . import bootstrap_credentials
from .config import DATA_DIR
from .constants import (
    DEFAULT_GEOIP_AUTO_UPDATE,
    DEFAULT_METRICS_TIMEOUT_MINUTES,
    DEFAULT_PDF_PAGE_SIZE,
    DEFAULT_PURGE_ENABLED,
    GEOIP_DB_RELPATH,
    SettingKeys,
)

_log = logging.getLogger(__name__)

# GeoLite2 database download URL (P3TERX mirror, updated regularly)
GEOIP_DOWNLOAD_URL = "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-City.mmdb"

# Plausibility ceiling for the download (the City database is well below 100 MB)
GEOIP_MAX_BYTES = 200 * 1024 * 1024
# Re-download after this age when the remote offers no usable change indicator
GEOIP_MAX_AGE_SECONDS = 45 * 24 * 3600

# The MMDB file ends with a metadata section that starts with this marker
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
            data_dir = DATA_DIR

        db_path = data_dir / GEOIP_DB_RELPATH
        if not db_path.exists():
            return None

        with geoip2.database.Reader(str(db_path)) as reader:
            return (reader.metadata().database_type, reader.metadata().build_epoch)
    except Exception:  # noqa: BLE001 - bootstrap resilience: log and fall back so startup never crashes
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
        _min_size = 1_000_000  # 1MB for tests
    else:
        _min_size = 10_000_000  # 10MB minimum for production (City DB is ~60MB)

    if file_size < _min_size:
        _log.warning("GeoIP database too small: %d bytes (minimum: %d)", file_size, _min_size)
        return False

    # Check for MMDB metadata marker in last 128KB of file
    try:
        with Path(file_path).open("rb") as f:
            # Seek to last 128KB and search for marker
            f.seek(max(0, file_size - 131072))
            tail = f.read()
            if MMDB_METADATA_MARKER not in tail:
                _log.warning("GeoIP database missing MMDB metadata marker")
                return False
    except OSError as e:
        _log.warning("Failed to read GeoIP database: %s", e)
        return False

    # Try to open with geoip2 library
    try:
        import geoip2.database

        with geoip2.database.Reader(str(file_path)) as reader:
            # Verify it's a City database
            if "City" not in reader.metadata().database_type:
                _log.warning("GeoIP database is not City type: %s", reader.metadata().database_type)
                return False
            _log.info(
                "GeoIP database verified: %s, build %d", reader.metadata().database_type, reader.metadata().build_epoch
            )
    except ImportError:
        _log.debug("geoip2 not installed, skipping deep verification")
    except Exception as e:  # noqa: BLE001 - bootstrap resilience: log and fall back so startup never crashes
        _log.warning("GeoIP database verification failed: %s", e)
        return False

    return True


class _DownloadTooLargeError(Exception):
    """The GeoIP download exceeds GEOIP_MAX_BYTES."""


def _meta_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".meta.json")


def _read_meta(db_path: Path) -> dict[str, str]:
    """Validators (ETag/Last-Modified) stored next to the database; empty if unavailable."""
    try:
        data = json.loads(_meta_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k in ("etag", "last_modified") if isinstance(v := data.get(k), str) and v}


def _write_meta(db_path: Path, headers) -> None:
    meta = {
        "etag": headers.get("ETag") or "",
        "last_modified": headers.get("Last-Modified") or "",
    }
    try:
        _meta_path(db_path).write_text(json.dumps(meta), encoding="utf-8")
    except OSError as e:
        # A stale sidecar would trigger a re-download on every start
        with contextlib.suppress(OSError):
            _meta_path(db_path).unlink(missing_ok=True)
        _log.warning("Could not store GeoIP validators: %s", e)


def _check_remote_meta() -> dict[str, object] | None:
    """
    Query size and validators of the remote file via HEAD request.

    Returns a dict with ``size`` (int | None), ``etag`` and ``last_modified``
    (str | None), or None if the remote could not be reached.
    """
    try:
        req = Request(GEOIP_DOWNLOAD_URL, method="HEAD", headers={"User-Agent": "justUp-Monitor/1.0"})
        with urlopen(req, timeout=30) as response:  # noqa: S310 - fixed https GEOIP_DOWNLOAD_URL constant, no user-controlled scheme
            content_length = response.headers.get("Content-Length")
            return {
                "size": int(content_length) if content_length and content_length.isdigit() else None,
                "etag": response.headers.get("ETag") or None,
                "last_modified": response.headers.get("Last-Modified") or None,
            }
    except Exception as e:  # noqa: BLE001 - bootstrap resilience: log and fall back so startup never crashes
        _log.debug("Failed to check remote GeoIP metadata: %s", e)
    return None


def _update_reason(geoip_path: Path, local_size: int, remote: dict[str, object] | None) -> str | None:
    """Return why the local database should be replaced, or None to keep it.

    Validators win over size; size is only a fallback. With no usable indicator
    the database is refreshed once it exceeds GEOIP_MAX_AGE_SECONDS.
    """
    if remote is None:
        return None  # remote unreachable: keep the existing database
    stored = _read_meta(geoip_path)
    for field in ("etag", "last_modified"):
        if remote.get(field) and stored.get(field):
            return None if remote[field] == stored[field] else f"{field} changed"
    size = remote.get("size")
    if isinstance(size, int) and size != local_size:
        return f"size changed (local: {local_size:,}, remote: {size:,})"
    if remote.get("etag") or remote.get("last_modified") or isinstance(size, int):
        return None
    try:
        age = time.time() - geoip_path.stat().st_mtime
    except OSError:
        return None
    return "database older than maximum age" if age > GEOIP_MAX_AGE_SECONDS else None


def _copy_limited(response, dest: Path, max_bytes: int | None = None) -> tuple[int, str]:
    """Stream ``response`` into ``dest``; abort past ``max_bytes``. Returns (bytes, sha256 hex)."""
    max_bytes = GEOIP_MAX_BYTES if max_bytes is None else max_bytes
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise _DownloadTooLargeError(f"declared size {declared} exceeds limit {max_bytes}")
    downloaded = 0
    hasher = hashlib.sha256()
    with dest.open("wb") as f:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise _DownloadTooLargeError(f"download exceeds limit {max_bytes}")
            f.write(chunk)
            hasher.update(chunk)
    return downloaded, hasher.hexdigest()


def _download_geoip_db(target_path: Path, force: bool = False) -> Path | None:
    """
    Download GeoLite2-City database if not present, invalid, or outdated.

    Args:
            target_path: Where to save the database.
            force: If True, skip the update check and always download.

    Returns the path to the database if successful, None otherwise.
    """
    geoip_path = target_path
    local_exists = geoip_path.exists()
    local_size = geoip_path.stat().st_size if local_exists else 0

    # Check if valid database already exists
    if local_exists and _verify_mmdb(geoip_path):
        if not force:
            reason = _update_reason(geoip_path, local_size, _check_remote_meta())
            if reason is None:
                _log.debug("GeoIP database kept (up-to-date or remote check inconclusive): %s", geoip_path)
                return geoip_path
            _log.info("GeoIP database update needed: %s", reason)
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
        temp_fd, temp_path_str = tempfile.mkstemp(suffix=".mmdb.tmp", prefix="geoip_", dir=str(geoip_path.parent))
        temp_path = Path(temp_path_str)

        # Close fd immediately, open by path to prevent fd leak
        os.close(temp_fd)
        temp_fd = None

        # Download with timeout and user agent
        req = Request(GEOIP_DOWNLOAD_URL, headers={"User-Agent": "justUp-Monitor/1.0"})

        with urlopen(req, timeout=120) as response:  # noqa: S310 - fixed https GEOIP_DOWNLOAD_URL constant, no user-controlled scheme
            # Check content type if available
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type.lower():
                _log.error("GeoIP download returned HTML instead of binary data")
                return None

            downloaded, digest = _copy_limited(response, temp_path)
            _log.info("Downloaded %s bytes, SHA256: %s...", f"{downloaded:,}", digest[:16])
            response_headers = response.headers

        # Verify the downloaded file
        if not _verify_mmdb(temp_path):
            _log.error("Downloaded GeoIP database failed verification - possibly corrupted or malicious")
            return None

        # Atomic rename (same filesystem guarantees atomicity)
        temp_path.rename(geoip_path)
        temp_path = None  # Renamed successfully, don't try to cleanup
        _write_meta(geoip_path, response_headers)
        _log.info("GeoIP database installed: %s", geoip_path)
        return geoip_path

    except _DownloadTooLargeError as e:
        _log.error("GeoIP download rejected: %s", e)
        return None
    except URLError as e:
        _log.warning("Failed to download GeoIP database: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 - bootstrap resilience: log and fall back so startup never crashes
        _log.error("GeoIP database download error: %s", e)
        return None
    finally:
        # Cleanup: close fd if still open, remove temp file on failure
        if temp_fd is not None:
            with contextlib.suppress(OSError):
                os.close(temp_fd)
        if temp_path and temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _get_default_settings() -> dict:
    """Built-in defaults seeded when no value is stored."""
    return {
        SettingKeys.PURGE_ENABLED: DEFAULT_PURGE_ENABLED,
        SettingKeys.PDF_PAGE_SIZE: DEFAULT_PDF_PAGE_SIZE,
        SettingKeys.METRICS_TIMEOUT_MINUTES: DEFAULT_METRICS_TIMEOUT_MINUTES,
        SettingKeys.GEOIP_AUTO_UPDATE: DEFAULT_GEOIP_AUTO_UPDATE,
    }


def bootstrap(db_path: Path, tsdb_dir: Path, data_dir: Path | None = None) -> bool:
    """Initialize the database schema and seed default settings/users.

    Returns True if the bootstrap admin was created on this start, in which
    case the application stays gated until its password was changed.
    """
    conn = sqlite_db.connect(db_path)
    # Initialize before try to prevent NameError if data_dir is None
    geoip_auto_update = None
    admin_created = False

    try:
        # Fresh-create: init_schema builds the complete current schema in one
        # step. There is no migration/upgrade path (schema version 1).
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        is_fresh_install = cursor.fetchone() is None

        if is_fresh_install:
            _log.info("Fresh installation detected - schema will be initialized")

        sqlite_db.init_schema(conn)

        for k, v in _get_default_settings().items():
            if sqlite_db.get_setting(conn, k, None) is None:
                sqlite_db.set_setting(conn, k, v)

        # Initial admin password: printed on creation and again on every start until it was changed
        bootstrap_credentials.configure(data_dir if data_dir is not None else db_path.parent)
        admin_created = sqlite_db.ensure_default_admin(conn)
        if not admin_created:
            sqlite_db.reprint_pending_bootstrap_credentials(conn)

        # TSDB init (folder creation)
        from ..db import tsdb as tsdb_db

        tsdb_db.init_tsdb(tsdb_dir)

        # Read GeoIP auto-update setting before closing connection
        if data_dir is not None:
            raw = sqlite_db.get_setting(conn, SettingKeys.GEOIP_AUTO_UPDATE, DEFAULT_GEOIP_AUTO_UPDATE)
            # Normalize string/bool/int to bool (handles "true", "false", 1, 0)
            geoip_auto_update = str(raw).strip().lower() in {"1", "true", "yes", "on"}
    finally:
        sqlite_db.close_connection(conn)

    # GeoIP download AFTER database connection is closed (avoid lock contention)
    if data_dir is not None and geoip_auto_update is not None:
        geoip_path = data_dir / GEOIP_DB_RELPATH
        if geoip_auto_update:
            _download_geoip_db(target_path=geoip_path, force=False)
        elif not geoip_path.exists():
            # Always download if missing, regardless of setting
            _log.info("GeoIP database missing, downloading (auto-update disabled, initial download only)...")
            _download_geoip_db(target_path=geoip_path, force=False)

    return admin_created
