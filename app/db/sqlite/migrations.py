#!/usr/bin/env python3
#
# app/db/sqlite/migrations.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite schema migrations: registry, compatibility check and ``init_schema`` orchestration."""

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
from .schema_notifications import ensure_alert_dispatch_outbox_table, ensure_notification_queue_table

# Keep the pre-split logger name: log filters and caplog assertions address app.db.sqlite.
_log = logging.getLogger("app.db.sqlite")


class Migration(NamedTuple):
    """One forward-only schema step that upgrades a database from ``version - 1`` to ``version``."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _migrate_2_spooler_resilience(conn: sqlite3.Connection) -> None:
    """Add the separate budget for non-transient send failures and the persistent channel state."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_queue)")}
    if "limited_attempts" not in columns:
        conn.execute(
            "ALTER TABLE notification_queue "
            "ADD COLUMN limited_attempts INTEGER NOT NULL DEFAULT 0 CHECK (limited_attempts >= 0)"
        )
        # Earlier releases counted every failure against one budget: keep those rows' history.
        conn.execute("UPDATE notification_queue SET limited_attempts = retry_count")
    ensure_notification_queue_table(conn)


def _migrate_3_alert_dispatch_outbox(conn: sqlite3.Connection) -> None:
    ensure_alert_dispatch_outbox_table(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "active_incident" not in columns:
        conn.execute(
            "ALTER TABLE alerts ADD COLUMN active_incident INTEGER NOT NULL DEFAULT 0 CHECK (active_incident IN (0, 1))"
        )
    conn.execute(
        """
        UPDATE alerts SET active_incident = 1
        WHERE id IN (
            SELECT id FROM (
                SELECT a.id, a.target_id,
                       upper(a.failed_service) LIKE 'CERT%' AS is_cert,
                       row_number() OVER (
                           PARTITION BY a.target_id, upper(a.failed_service) LIKE 'CERT%'
                           ORDER BY a.started_at DESC, a.id DESC
                       ) AS position
                FROM alerts a
                WHERE a.status = 'open'
            ) ranked
            WHERE position = 1 AND (
                (is_cert AND EXISTS (
                    SELECT 1 FROM target_status ts
                    WHERE ts.target_id = ranked.target_id AND ts.cert_days_left <= 7
                )) OR (NOT is_cert AND EXISTS (
                    SELECT 1 FROM target_status ts
                    WHERE ts.target_id = ranked.target_id AND ts.overall_status = 'down'
                ))
            )
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO alert_dispatch_outbox (
            alert_id, event_type, target_id, target_name, failed_service,
            started_at, ended_at, duration_seconds, status, attempts,
            next_attempt_at, created_at, updated_at
        )
        SELECT id, 'open', target_id, target_name, failed_service,
               started_at, NULL, NULL, 'pending', 0, created_at, created_at, updated_at
        FROM alerts WHERE status = 'open' AND active_incident = 1
        """
    )


def _migrate_4_dashboard_widget_height_mode(conn: sqlite3.Connection) -> None:
    """One-time schema upgrade: preserve v3 positions/widths and assign automatic height mode."""
    views = list(conn.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'view' ORDER BY name"))
    view_triggers = [
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND tbl_name IN "
            "(SELECT name FROM sqlite_schema WHERE type = 'view') ORDER BY name"
        )
    ]
    dependent_sql = [
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_schema WHERE tbl_name = 'dashboard_layouts' "
            "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"
        )
    ]
    for name, _sql in views:
        quoted_name = '"' + name.replace('"', '""') + '"'
        conn.execute(f"DROP VIEW {quoted_name}")
    conn.execute(
        """
        CREATE TABLE dashboard_layouts_v4 (
            user_id INTEGER NOT NULL,
            widget_id TEXT NOT NULL CHECK (length(widget_id) BETWEEN 1 AND 64),
            position INTEGER NOT NULL CHECK (position >= 0),
            x INTEGER NOT NULL CHECK (x BETWEEN 0 AND 11),
            y INTEGER NOT NULL CHECK (y >= 0),
            w INTEGER NOT NULL CHECK (w BETWEEN 1 AND 12),
            h INTEGER NOT NULL CHECK (h BETWEEN 1 AND 100),
            visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
            layout_version INTEGER NOT NULL DEFAULT 2 CHECK (layout_version = 2),
            height_mode TEXT NOT NULL DEFAULT 'auto' CHECK (height_mode IN ('auto', 'manual')),
            PRIMARY KEY (user_id, widget_id),
            CHECK (x + w <= 12),
            CHECK (y + h <= 1000),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        INSERT INTO dashboard_layouts_v4 (
            user_id, widget_id, position, x, y, w, h, visible, layout_version, height_mode
        )
        SELECT user_id, widget_id, position, x, y, w, h, visible, 2, 'auto'
        FROM dashboard_layouts
        """
    )
    conn.execute("DROP TABLE dashboard_layouts")
    conn.execute("ALTER TABLE dashboard_layouts_v4 RENAME TO dashboard_layouts")
    for statement in dependent_sql:
        conn.execute(statement)
    for _name, statement in views:
        conn.execute(statement)
    for statement in view_triggers:
        conn.execute(statement)


def _migrate_5_recipient_enable_for_all_targets(conn: sqlite3.Connection) -> None:
    """Add the per-recipient "enable for all targets" flag; existing recipients keep it off."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recipients)")}
    if "enable_for_all_targets" not in columns:
        conn.execute(
            "ALTER TABLE recipients ADD COLUMN "
            "enable_for_all_targets INTEGER NOT NULL DEFAULT 0 CHECK (enable_for_all_targets IN (0, 1))"
        )


def _migrate_6_pop_candidates(conn: sqlite3.Connection) -> None:
    """Add bounded PoP observations and explicit-review metadata without promoting old candidates."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pop_alias_candidates)")}
    additions = {
        "hostname": "TEXT",
        "public_ip": "TEXT",
        "parser_context": "TEXT",
        "proposal_source": "TEXT",
        "proposal_lat": "REAL",
        "proposal_lon": "REAL",
        "independent_count": "INTEGER NOT NULL DEFAULT 0",
        "conflict_count": "INTEGER NOT NULL DEFAULT 0",
        "quality_score": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "reviewed_at": "TEXT",
        "reviewed_by": "TEXT",
        "review_evidence": "TEXT",
        "approved_location_id": "INTEGER",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE pop_alias_candidates ADD COLUMN {name} {definition}")
    conn.execute(
        "UPDATE pop_alias_candidates SET status = 'legacy_reviewed' WHERE reviewed = 1 AND approved_location_id IS NULL"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pop_candidate_observations (
            candidate_id INTEGER NOT NULL REFERENCES pop_alias_candidates(id),
            measurement_key TEXT NOT NULL,
            context_key TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id, measurement_key)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pop_observations_context "
        "ON pop_candidate_observations(candidate_id, context_key)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pop_candidate_reviews (
            id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES pop_alias_candidates(id),
            action TEXT NOT NULL CHECK(action IN ('approve', 'reject', 'revoke')),
            actor TEXT NOT NULL,
            evidence TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            location_id INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pop_city_evidence (
            city_code TEXT NOT NULL,
            city TEXT NOT NULL,
            country TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            facility_count INTEGER NOT NULL,
            position_source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(city_code, country)
        )"""
    )


# Ordered by version, contiguous from 2.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        2,
        "spooler resilience: limited_attempts column, notification_channel_state table",
        _migrate_2_spooler_resilience,
    ),
    Migration(3, "durable alert dispatch outbox", _migrate_3_alert_dispatch_outbox),
    Migration(4, "dashboard widget automatic and manual height contract", _migrate_4_dashboard_widget_height_mode),
    Migration(5, "recipients.enable_for_all_targets flag", _migrate_5_recipient_enable_for_all_targets),
    Migration(6, "bounded PoP candidate evidence and explicit review metadata", _migrate_6_pop_candidates),
)


def check_schema_compatibility(conn: sqlite3.Connection) -> int:
    """Return the stored user_version if the database may be initialised or opened by this release.

    Compatible: a new, empty database (user_version 0, no application tables), a database with an
    older user_version and a registered migration path, or a database at SCHEMA_VERSION whose tables,
    columns (types, NOT NULL, defaults, primary keys), foreign keys, unique keys, CHECK constraints and
    required indexes match the current schema. Column order is ignored: ALTER TABLE ADD COLUMN always
    appends, so a correctly migrated database orders columns differently from a new one.
    Read-only: nothing is written.

    Raises:
        IncompatibleSchemaError: ``kind`` tells corrupt file, unversioned pre-release tables, future
            schema version, missing migration path or schema drift apart.
    """
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
                f"this release migrates only databases created by v1.0.0 or later. To start over, move the file "
                f"and data/tsdb aside (measurements are bound to target IDs). {_SAFE_HINT}",
                kind=SCHEMA_UNVERSIONED,
                version=0,
            )
    elif version > SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            f"{where}: its schema version is {version}, newer than the {SCHEMA_VERSION} this release supports; "
            f"it was written by a newer JustUp. Start that release again or restore a backup made with this one. "
            + _SAFE_HINT,
            kind=SCHEMA_FUTURE,
            version=version,
        )
    elif version < SCHEMA_VERSION:
        if _missing_migrations(version):
            raise IncompatibleSchemaError(
                f"{where}: its schema version is {version}, but this release supports {SCHEMA_VERSION} and has no "
                f"complete migration path from {version}. {_SAFE_HINT}",
                kind=SCHEMA_NO_MIGRATION_PATH,
                version=version,
            )
    elif diffs := _diff_layout((tables, indexes), _expected_layout()):
        raise _drift_error(conn, version, diffs)
    return version


def _missing_migrations(current: int) -> bool:
    """True if MIGRATIONS has no contiguous chain from *current* + 1 up to SCHEMA_VERSION."""
    chain = sorted(m.version for m in MIGRATIONS if m.version > current)
    return chain != list(range(current + 1, SCHEMA_VERSION + 1))


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema on a new database, or accept (and upgrade) a compatible one.

    The compatibility check runs first and writes nothing, so an incompatible file stays byte-identical.
    Migrations and the final layout validation share one immediate transaction: any failure rolls
    schema changes and user_version back together. Each applied migration is logged once.

    Raises:
        IncompatibleSchemaError: see check_schema_compatibility(); also ``kind`` migration_failed
            or schema_drift when an upgrade was rolled back.
    """
    current = check_schema_compatibility(conn)
    if current == 0:
        # Only effective before the first table exists; existing files are never converted (needs a VACUUM).
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    elif int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) == 0:
        _log.info("Database has auto_vacuum=0; free pages are not returned incrementally (no automatic VACUUM)")
    ensure_wal_mode(conn)  # journal_mode cannot change inside the transaction below
    pending = [m for m in sorted(MIGRATIONS, key=lambda m: m.version) if m.version > current]
    rebuild = any(m.version == 4 for m in pending)
    foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if rebuild:
        if conn.in_transaction:
            raise sqlite3.OperationalError("Dashboard layout migration requires an outermost transaction")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = OFF")
    step: Migration | None = None
    try:
        with transaction(conn, immediate=True):
            if current == 0:
                _create_baseline_schema(conn)
            for step in pending:
                step.apply(conn)
                if step.version == 4 and (violations := conn.execute("PRAGMA foreign_key_check").fetchall()):
                    raise sqlite3.IntegrityError(f"Foreign-key violations after dashboard migration: {violations[:3]}")
            step = None
            if current != SCHEMA_VERSION:
                if diffs := _layout_problems(conn):
                    raise _drift_error(conn, current, diffs)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif current:
                _ensure_unique_recipient_phone(conn)
    except Exception as exc:
        if step is not None:
            _log.error(
                "Schema migration %d -> %d (%s): failed and rolled back, database stays at schema version %d: %s",
                step.version - 1,
                step.version,
                step.description,
                current,
                exc,
            )
            raise IncompatibleSchemaError(
                f"Cannot use database {_database_file(conn)}: schema migration {step.version - 1} -> {step.version} "
                f"({step.description}) failed and was rolled back; the database is unchanged at schema version "
                f"{current}: {type(exc).__name__}: {exc}. {_SAFE_HINT}",
                kind=SCHEMA_MIGRATION_FAILED,
                version=current,
            ) from exc
        if pending and isinstance(exc, IncompatibleSchemaError):
            _log.error(
                "Schema migration %d -> %d: rolled back, the migrated layout is invalid: %s",
                current,
                SCHEMA_VERSION,
                exc,
            )
        raise
    finally:
        if rebuild and foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
    for migration in pending if current else ():
        _log.info("Schema migration %d -> %d (%s): ok", migration.version - 1, migration.version, migration.description)
    # After index/schema changes: refresh planner statistics where stale (cheap, outside the transaction).
    conn.execute("PRAGMA optimize")
