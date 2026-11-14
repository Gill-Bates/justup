#!/usr/bin/env python3
#
# app/db/sqlite/migrations.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite schema versioning and initialization."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import NamedTuple

from ...utils.constants import (
    DEFAULT_TARGET_DOWN_ALERT_DELAY_MINUTES,
    TARGET_DOWN_ALERT_DELAY_MINUTES_MAX,
    TARGET_DOWN_ALERT_DELAY_MINUTES_MIN,
)
from ...utils.time import utcnow
from .connection import ensure_wal_mode, transaction
from .layout import (
    _SAFE_HINT,
    _SQLITE_CORRUPT_CODES,
    _database_file,
    _diff_layout,
    _drift_error,
    _layout,
    _layout_for_version,
    _layout_problems,
)
from .schema import (
    _RECIPIENTS_DDL,
    BLANK_CHARS_SQL,
    SCHEMA_CORRUPT,
    SCHEMA_FUTURE,
    SCHEMA_MIGRATION_FAILED,
    SCHEMA_NO_MIGRATION_PATH,
    SCHEMA_UNVERSIONED,
    SCHEMA_VERSION,
    IncompatibleSchemaError,
    _create_baseline_schema,
    _ensure_recipient_triggers,
    _ensure_unique_recipient_phone,
    get_schema_version,
)
from .users import ensure_user_recipients

_log = logging.getLogger("app.db.sqlite")


class Migration(NamedTuple):
    """One forward-only step from version - 1 to version after the new baseline."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]
    # Table rebuilds need ``PRAGMA foreign_keys=OFF``, which only works outside a transaction:
    # init_schema switches it off before BEGIN and restores it afterwards.
    foreign_keys_off: bool = False


# The former current version has the same layout as the new version-1 baseline.
# This one-time restamp preserves databases and backups created by that release.
LEGACY_SCHEMA_VERSION = 10

# The former global "Down Alert Delay" setting. Only the migration still knows it: the value moves into the
# per-target column and the row is deleted.
_OLD_DELAY_SETTING_KEY = "metrics_timeout_minutes"


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Version 2: index report_jobs.target_id; link recipients to users; per-target down alert delay.

    Rebuilds ``recipients`` with ``user_id`` and gives every user a recipient.

    Runs inside init_schema's immediate transaction with foreign keys switched off (a DROP TABLE with active
    foreign keys would cascade-delete ``target_recipients``). Repeatable: an existing ``user_id`` column skips the
    rebuild and the backfill only touches users without a recipient. Counts are verified before the commit.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_jobs_target ON report_jobs(target_id)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(recipients)")}
    if "user_id" not in columns:
        _rebuild_recipients_with_user_id(conn)
    before = conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
    now = utcnow()
    # Existing users get a disabled recipient without channels; they configure it in the user dialog.
    added = conn.execute(
        "INSERT INTO recipients(name, phone, email, is_enabled, enable_for_all_targets, created_at, updated_at, "
        "user_id) SELECT u.username, NULL, NULL, 0, 0, ?, ?, u.id FROM users u "
        "WHERE NOT EXISTS (SELECT 1 FROM recipients r WHERE r.user_id = u.id)",
        (now, now),
    ).rowcount
    after = conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
    if after != before + added:
        raise RuntimeError(f"recipients row count mismatch after backfill: {before} + {added} != {after}")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("foreign key violations after the recipients migration")
    _ensure_recipient_triggers(conn)
    _add_target_down_alert_delay(conn)


def _old_global_delay(conn: sqlite3.Connection) -> int:
    """The former global value with the semantics the scheduler used: missing/falsy/invalid -> 5, clamped to 1..10."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_OLD_DELAY_SETTING_KEY,)).fetchone()
    if row is None:
        return DEFAULT_TARGET_DOWN_ALERT_DELAY_MINUTES
    try:
        stored = json.loads(row[0])
        value = int(stored or DEFAULT_TARGET_DOWN_ALERT_DELAY_MINUTES)
    except (ValueError, TypeError, OverflowError):
        _log.warning("Stored %s is not a number; using the default", _OLD_DELAY_SETTING_KEY)
        return DEFAULT_TARGET_DOWN_ALERT_DELAY_MINUTES
    return max(TARGET_DOWN_ALERT_DELAY_MINUTES_MIN, min(TARGET_DOWN_ALERT_DELAY_MINUTES_MAX, value))


def _add_target_down_alert_delay(conn: sqlite3.Connection) -> None:
    """Add targets.down_alert_delay_minutes, give every target the former global value and drop that setting.

    Repeatable: an existing column means the step already ran, so neither target values nor settings are touched.
    Runs inside init_schema's transaction; a failed row count rolls everything back. The ALTER text equals the
    baseline column definition, which the layout check after the migrations requires.
    """
    if any(row[1] == "down_alert_delay_minutes" for row in conn.execute("PRAGMA table_info(targets)")):
        return
    conn.execute(
        "ALTER TABLE targets ADD COLUMN down_alert_delay_minutes INTEGER NOT NULL DEFAULT 5 "
        "CHECK (down_alert_delay_minutes > 0)"
    )
    delay = _old_global_delay(conn)
    total = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    updated = conn.execute("UPDATE targets SET down_alert_delay_minutes = ?", (delay,)).rowcount
    if updated != total:
        raise RuntimeError(f"down alert delay was set on {updated} of {total} targets")
    conn.execute("DELETE FROM settings WHERE key = ?", (_OLD_DELAY_SETTING_KEY,))
    _log.info("Moved the global down alert delay (%d min) to %d target(s)", delay, total)


def _rebuild_recipients_with_user_id(conn: sqlite3.Connection) -> None:
    """SQLite table rebuild (create new, copy, drop, rename) keeping ids, rows and the AUTOINCREMENT counter."""
    # The compatibility check tolerates extra tables, so the scratch name may belong to someone else. Never
    # drop an unknown object: stop with a clear error before anything is written.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'recipients_new' COLLATE NOCASE").fetchone():
        raise RuntimeError(
            "table 'recipients_new' already exists and is not part of the schema; the migration needs that name "
            "and will not drop it. Rename or remove it yourself (after checking its content), then restart"
        )
    # Version 1 only required "not NULL", so a blank string may count as a channel there. It is normalised to
    # NULL below (no information is lost); a standalone recipient left without any real channel cannot be
    # kept under the stricter CHECK and is not deleted or invented away either: stop and name the rows.
    unusable = [
        int(row[0])
        for row in conn.execute(
            f"SELECT id FROM recipients WHERE COALESCE(trim(phone, {BLANK_CHARS_SQL}), '') = '' "  # noqa: S608 - constant
            f"AND COALESCE(trim(email, {BLANK_CHARS_SQL}), '') = '' ORDER BY id"
        )
    ]
    if unusable:
        raise RuntimeError(
            f"recipient id(s) {', '.join(map(str, unusable))} have no usable contact channel (phone and email are "
            "empty or blank); give them a phone number or email address, or delete them, then restart"
        )
    rows_before = conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
    links_before = conn.execute("SELECT COUNT(*) FROM target_recipients").fetchone()[0]
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'recipients'").fetchone()
    conn.execute(_RECIPIENTS_DDL.format(name="recipients_new", if_not_exists=""))
    conn.execute(
        "INSERT INTO recipients_new(id, name, phone, email, is_enabled, created_at, updated_at, "  # noqa: S608 - constant
        "enable_for_all_targets) SELECT id, name, "
        f"CASE WHEN trim(phone, {BLANK_CHARS_SQL}) = '' THEN NULL ELSE phone END, "
        f"CASE WHEN trim(email, {BLANK_CHARS_SQL}) = '' THEN NULL ELSE email END, "
        "is_enabled, created_at, updated_at, enable_for_all_targets FROM recipients"
    )
    conn.execute("DROP TABLE recipients")
    conn.execute("ALTER TABLE recipients_new RENAME TO recipients")
    if sequence is not None:  # a deleted recipient's id must not be handed out again
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'recipients'", (sequence[0],)
        ).rowcount
        if not updated:
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('recipients', ?)", (sequence[0],))
    # Non-unique first; _ensure_unique_recipient_phone upgrades it unless legacy duplicates exist.
    conn.execute("CREATE INDEX idx_recipients_phone ON recipients(phone)")
    conn.execute("CREATE INDEX idx_recipients_email ON recipients(email)")
    conn.execute("CREATE INDEX idx_recipients_enabled ON recipients(is_enabled)")
    _ensure_unique_recipient_phone(conn)
    rows_after = conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
    links_after = conn.execute("SELECT COUNT(*) FROM target_recipients").fetchone()[0]
    if (rows_after, links_after) != (rows_before, links_before):
        raise RuntimeError(
            f"recipients rebuild changed row counts: recipients {rows_before} -> {rows_after}, "
            f"target_recipients {links_before} -> {links_after}"
        )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(2, "link recipients to users; per-target down alert delay", _migrate_v1_to_v2, foreign_keys_off=True),
)


def check_schema_compatibility(conn: sqlite3.Connection) -> int:
    """Read-only validation of an empty, current, or exact former-current database.

    Version and layout come from one read snapshot (a transaction of their own unless the caller already holds
    one), so a concurrent migration cannot show up as a version of one schema and the layout of another.
    """
    owns_snapshot = not conn.in_transaction
    try:
        if owns_snapshot:
            conn.execute("BEGIN")
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
    finally:
        if owns_snapshot and conn.in_transaction:
            conn.rollback()
    where = f"Cannot use database {_database_file(conn)}"
    if version == 0:
        if tables:
            raise IncompatibleSchemaError(
                f"{where}: it contains tables but no schema version (unversioned pre-release database); "
                f"the data was left untouched. {_SAFE_HINT}",
                kind=SCHEMA_UNVERSIONED,
                version=0,
            )
    elif version in (SCHEMA_VERSION, LEGACY_SCHEMA_VERSION, 1):
        if diffs := _diff_layout((tables, indexes), _layout_for_version(version)):
            raise _drift_error(conn, version, diffs, expected_version=1 if version != SCHEMA_VERSION else version)
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


def _pending_migrations(current: int) -> list[Migration]:
    """Migrations that lead from a stored version to SCHEMA_VERSION (the legacy stamp starts at version 1)."""
    if current == 0:
        return []
    start = 1 if current == LEGACY_SCHEMA_VERSION else current
    return [
        m for m in sorted(MIGRATIONS, key=lambda migration: migration.version) if start < m.version <= SCHEMA_VERSION
    ]


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the baseline or validate, migrate and stamp a supported database.

    The layout check precedes writes. Schema creation, migrations, and the version stamp share an immediate
    transaction, so a failure leaves existing data and version unchanged. The version is read again under the
    write lock: a second process that started at the same time finds the finished database and does nothing.
    """
    current = check_schema_compatibility(conn)
    if current == 0:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    elif int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) == 0:
        _log.info("Database has auto_vacuum=0; free pages are not returned incrementally (no automatic VACUUM)")
    ensure_wal_mode(conn)
    pending = _pending_migrations(current)
    restore_foreign_keys = _disable_foreign_keys_for(conn, pending, current)
    step: Migration | None = None
    try:
        with transaction(conn, immediate=True):
            # Re-check under the write lock: another process may have migrated since the first check, which
            # would otherwise be reported as drift or migrated a second time.
            locked = check_schema_compatibility(conn)
            if locked != current:
                _log.info("Schema changed from version %d to %d while waiting for the write lock", current, locked)
                current, pending = locked, _pending_migrations(locked)
                if any(m.foreign_keys_off for m in pending) and int(conn.execute("PRAGMA foreign_keys").fetchone()[0]):
                    raise IncompatibleSchemaError(
                        f"Cannot use database {_database_file(conn)}: the schema changed while starting and needs a "
                        f"restart to migrate with foreign keys disabled. {_SAFE_HINT}",
                        kind=SCHEMA_MIGRATION_FAILED,
                        version=current,
                    )
            if current == 0:
                _create_baseline_schema(conn)
            else:
                _ensure_unique_recipient_phone(conn)
            for step in pending:
                step.apply(conn)
            step = None
            if current != 0:
                _ensure_recipient_triggers(conn)
                healed = ensure_user_recipients(conn)
                if healed and current == SCHEMA_VERSION:
                    _log.warning(
                        "Created a missing recipient for %d user(s); the database was changed directly", healed
                    )
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
    finally:
        if restore_foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
    for migration in pending:
        _log.info("Schema migration %d -> %d (%s): ok", migration.version - 1, migration.version, migration.description)
    if current == LEGACY_SCHEMA_VERSION:
        _log.info("Schema baseline reset %d -> %d: ok", current, SCHEMA_VERSION)
    conn.execute("PRAGMA optimize")


def _disable_foreign_keys_for(conn: sqlite3.Connection, pending: list[Migration], current: int) -> bool:
    """Switch foreign keys off when a pending migration rebuilds tables; True if init_schema must restore them.

    The pragma is a no-op inside a transaction, so its effect is read back and the start is refused before
    anything is written. Without this a DROP TABLE would cascade-delete rows of referencing tables.
    """
    if not any(m.foreign_keys_off for m in pending):
        return False
    was_on = int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    if not was_on:
        return False
    conn.execute("PRAGMA foreign_keys=OFF")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise IncompatibleSchemaError(
            f"Cannot use database {_database_file(conn)}: foreign keys could not be disabled for the schema "
            f"migration (is a transaction open on this connection?). The file was not modified. " + _SAFE_HINT,
            kind=SCHEMA_MIGRATION_FAILED,
            version=current,
        )
    return True
