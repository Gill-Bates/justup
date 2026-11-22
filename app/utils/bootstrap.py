#!/usr/bin/env python3
#
# app/utils/bootstrap.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Database bootstrap helpers (schema init, default settings, default admin)."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from gillbates_geoip import GeoIPUnavailableError

from ..db import sqlite as sqlite_db
from ..services.geoip import databases as geoip_databases
from . import bootstrap_credentials
from .config import DATA_DIR
from .constants import (
    DEFAULT_GEOIP_AUTO_UPDATE,
    DEFAULT_PDF_PAGE_SIZE,
    DEFAULT_PURGE_ENABLED,
    GEOIP_ASN_DB_RELPATH,
    GEOIP_DB_RELPATH,
    SettingKeys,
)

_log = logging.getLogger(__name__)


def get_geoip_build_info(
    data_dir: Path | None = None, relpath: PurePosixPath = GEOIP_DB_RELPATH
) -> tuple[str, int] | None:
    """Type and build epoch of the City database, or of the ASN one when ``relpath`` is the ASN path."""
    if data_dir is None:
        data_dir = DATA_DIR
    kind = "asn" if relpath == GEOIP_ASN_DB_RELPATH else "city"
    return geoip_databases(data_dir / GEOIP_DB_RELPATH).build_info(kind)


def _refresh_geoip(data_dir: Path, *, auto_update: bool) -> None:
    """Download or refresh both databases through ``gillbates_geoip``; never raises (startup must not fail)."""
    city_path = data_dir / GEOIP_DB_RELPATH
    asn_path = data_dir / GEOIP_ASN_DB_RELPATH
    for label, path in (("City", city_path), ("ASN", asn_path)):
        if not _is_inside_geoip_dir(data_dir, path):
            _log.error("GeoIP %s path leaves the geoip data directory, skipped", label)
            return
    if not auto_update and city_path.exists() and asn_path.exists():
        return
    if not auto_update:
        _log.info("GeoIP database missing, downloading (auto-update disabled, initial download only)...")
    try:
        geoip_databases(city_path).ensure()
    except GeoIPUnavailableError as exc:
        _log.warning("GeoIP download failed: %s", exc)


def _is_inside_geoip_dir(data_dir: Path, path: Path) -> bool:
    """True if ``path`` resolves to a location inside ``<data_dir>/geoip`` (no traversal, no symlink escape)."""
    try:
        path.resolve().relative_to((data_dir / "geoip").resolve())
    except ValueError, OSError:
        return False
    return True


def _get_default_settings() -> dict:
    """Built-in defaults seeded when no value is stored."""
    return {
        SettingKeys.PURGE_ENABLED: DEFAULT_PURGE_ENABLED,
        SettingKeys.PDF_PAGE_SIZE: DEFAULT_PDF_PAGE_SIZE,
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
        # init_schema creates the current schema on a new database, migrates a supported older one in
        # one transaction and refuses an incompatible one (IncompatibleSchemaError) without touching it.
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
        _refresh_geoip(data_dir, auto_update=geoip_auto_update)

    return admin_created
