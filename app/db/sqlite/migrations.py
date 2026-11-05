#!/usr/bin/env python3
#
# app/db/sqlite/migrations.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite schema versioning and initialization."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import NamedTuple

from .connection import ensure_wal_mode, transaction
from .layout import (
    _SAFE_HINT,
    _SQLITE_CORRUPT_CODES,
    _database_file,
    _diff_layout,
    _drift_error,
    _expected_layout,
    _layout,
    _layout_problems,
)
from .schema import (
    SCHEMA_CORRUPT,
    SCHEMA_FUTURE,
    SCHEMA_MIGRATION_FAILED,
    SCHEMA_NO_MIGRATION_PATH,
    SCHEMA_UNVERSIONED,
    SCHEMA_VERSION,
    IncompatibleSchemaError,
    _create_baseline_schema,
    _ensure_unique_recipient_phone,
    get_schema_version,
)

_log = logging.getLogger("app.db.sqlite")


class Migration(NamedTuple):
    """One forward-only step from version - 1 to version after the new baseline."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


# The former current version has the same layout as the new version-1 baseline.
# This one-time restamp preserves databases and backups created by that release.
LEGACY_SCHEMA_VERSION = 10
MIGRATIONS: tuple[Migration, ...] = ()


def check_schema_compatibility(conn: sqlite3.Connection) -> int:
    """Read-only validation of an empty, current, or exact former-current database."""
    try:
        version = get_schema_version(conn)
        tables, indexes = _layout(conn)
    except sqlite3.DatabaseError as exc:
        if (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF in _SQLITE_CORRUPT_CODES:
            raise IncompatibleSchemaError(
                f"Cannot use database {_database_file(conn)}: it is not a readable SQLite database ({exc}). "
                + _SAFE_HINT,
                kind=SCHEMA_CORRUPT,
            ) from exc
        raise
    where = f"Cannot use database {_database_file(conn)}"
    if version == 0:
        if tables:
            raise IncompatibleSchemaError(
                f"{where}: it contains tables but no schema version (unversioned pre-release database); "
                f"the data was left untouched. {_SAFE_HINT}",
                kind=SCHEMA_UNVERSIONED,
                version=0,
            )
    elif version in (SCHEMA_VERSION, LEGACY_SCHEMA_VERSION):
        if diffs := _diff_layout((tables, indexes), _expected_layout()):
            raise _drift_error(conn, version, diffs)
    elif version < SCHEMA_VERSION and not _missing_migrations(version):
        return version
    elif version < LEGACY_SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            f"{where}: its schema version is {version}, but this release supports {SCHEMA_VERSION} "
            f"and has no migration path from {version}. {_SAFE_HINT}",
            kind=SCHEMA_NO_MIGRATION_PATH,
            version=version,
        )
    else:
        raise IncompatibleSchemaError(
            f"{where}: its schema version is {version}, newer than the {SCHEMA_VERSION} this release supports; "
            f"start the matching release or restore a compatible backup. {_SAFE_HINT}",
            kind=SCHEMA_FUTURE,
            version=version,
        )
    return version


def _missing_migrations(current: int) -> bool:
    """Whether a version has no complete path to the current baseline."""
    if current == LEGACY_SCHEMA_VERSION:
        return False
    if current > SCHEMA_VERSION:
        return True
    chain = sorted(m.version for m in MIGRATIONS if m.version > current)
    return chain != list(range(current + 1, SCHEMA_VERSION + 1))


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the v1 baseline or validate and restamp an exact former-current database.

    The layout check precedes writes. Schema creation, future migrations, and the version stamp
    share an immediate transaction, so a failure leaves existing data and version unchanged.
    """
    current = check_schema_compatibility(conn)
    if current == 0:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    elif int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) == 0:
        _log.info("Database has auto_vacuum=0; free pages are not returned incrementally (no automatic VACUUM)")
    ensure_wal_mode(conn)
    pending = (
        []
        if current == 0
        else [
            m
            for m in sorted(MIGRATIONS, key=lambda migration: migration.version)
            if current < m.version <= SCHEMA_VERSION
        ]
    )
    step: Migration | None = None
    try:
        with transaction(conn, immediate=True):
            if current == 0:
                _create_baseline_schema(conn)
            else:
                _ensure_unique_recipient_phone(conn)
            for step in pending:
                step.apply(conn)
            step = None
            if current != SCHEMA_VERSION:
                if diffs := _layout_problems(conn):
                    raise _drift_error(conn, current, diffs)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except Exception as exc:
        if step is not None:
            _log.error(
                "Schema migration %d -> %d (%s) failed and rolled back: %s",
                step.version - 1,
                step.version,
                step.description,
                exc,
            )
            raise IncompatibleSchemaError(
                f"Cannot use database {_database_file(conn)}: schema migration {step.version - 1} -> "
                f"{step.version} ({step.description}) failed and was rolled back: {type(exc).__name__}: {exc}. "
                + _SAFE_HINT,
                kind=SCHEMA_MIGRATION_FAILED,
                version=current,
            ) from exc
        raise
    for migration in pending:
        _log.info("Schema migration %d -> %d (%s): ok", migration.version - 1, migration.version, migration.description)
    if current == LEGACY_SCHEMA_VERSION:
        _log.info("Schema baseline reset %d -> %d: ok", current, SCHEMA_VERSION)
    conn.execute("PRAGMA optimize")
