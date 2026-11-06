#!/usr/bin/env python3
#
# app/db/sqlite/schema.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite baseline schema (DDL), schema version constants and the incompatible-schema error."""

from __future__ import annotations

import logging
import sqlite3

from ...utils.constants import CheckStatus
from .schema_notifications import (
    ensure_alert_dispatch_outbox_table,
    ensure_notification_queue_table,
    ensure_notifications_tables,
)

# Keep the pre-split logger name: log filters and caplog assertions address app.db.sqlite.
_log = logging.getLogger("app.db.sqlite")


# CHECK list for target_status.overall_status, built from the one status enum (constants only, no input).
_OVERALL_STATUS_SQL = ", ".join(f"'{status.value}'" for status in CheckStatus)


def ensure_target_network_ports_table(conn: sqlite3.Connection) -> None:
    """Create the protocol/port identity table used by network monitoring."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS target_network_ports (
            target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
            protocol TEXT NOT NULL CHECK (protocol IN ('tcp', 'udp')),
            port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
            source TEXT NOT NULL CHECK (source IN ('manual', 'discovered')),
            service_name TEXT,
            service_product TEXT,
            service_version TEXT,
            service_method TEXT CHECK (service_method IN ('probed', 'table')),
            confidence INTEGER CHECK (confidence BETWEEN 0 AND 10),
            discovered_at timestamp,
            status TEXT CHECK (status IN ('up', 'down', 'unknown')),
            checked_at timestamp,
            last_up_at timestamp,
            detail TEXT,
            raw_state TEXT CHECK (raw_state IN (
                'open', 'closed', 'filtered', 'unfiltered', 'open|filtered', 'closed|filtered', 'unknown'
            )),
            service_identity TEXT CHECK (service_identity IN ('identified', 'unknown', 'changed')),
            identity_changed_at timestamp,
            latency_ms REAL,
            PRIMARY KEY (target_id, protocol, port)
        )
        """
    )


def _create_baseline_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index of the current schema (SCHEMA_VERSION; idempotent, no transaction handling)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            can_close_alerts INTEGER NOT NULL DEFAULT 0 CHECK (can_close_alerts IN (0, 1)),
            must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
            theme TEXT NOT NULL DEFAULT 'system' CHECK (theme IN ('system', 'light', 'dark')),
            last_login_at timestamp,
            last_login_ip TEXT,
            created_at timestamp NOT NULL
        )
        """
    )

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
                group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
                is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
            interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
            retention_days INTEGER NOT NULL CHECK (retention_days > 0),
            enable_ping INTEGER NOT NULL CHECK (enable_ping IN (0, 1)),
            enable_http_check INTEGER NOT NULL CHECK (enable_http_check IN (0, 1)),
            enable_cert_expiration INTEGER NOT NULL CHECK (enable_cert_expiration IN (0, 1)),
            enable_tcp_connect INTEGER NOT NULL DEFAULT 0 CHECK (enable_tcp_connect IN (0, 1)),
            enable_udp_scan INTEGER NOT NULL DEFAULT 0 CHECK (enable_udp_scan IN (0, 1)),
            host TEXT NOT NULL DEFAULT '',
            ping_host TEXT,
            http_url TEXT,
            http_username TEXT,
            http_password TEXT,
            http_prefer_head INTEGER NOT NULL DEFAULT 1 CHECK (http_prefer_head IN (0, 1)),
            http_basic_auth_enabled INTEGER NOT NULL DEFAULT 0 CHECK (http_basic_auth_enabled IN (0, 1)),
            http_success_codes TEXT DEFAULT '200-399',
            http_port INTEGER CHECK (http_port BETWEEN 1 AND 65535),
            cert_host TEXT,
                cert_port INTEGER CHECK (cert_port BETWEEN 1 AND 65535),
                ignore_cert_errors INTEGER NOT NULL DEFAULT 0 CHECK (ignore_cert_errors IN (0, 1)),
                tcp_host TEXT,
                tcp_ports TEXT DEFAULT '80',
                udp_ports TEXT NOT NULL DEFAULT '',
                max_retries INTEGER NOT NULL DEFAULT 5 CHECK (max_retries >= 0),
                ping_timeout_minutes INTEGER NOT NULL DEFAULT 5 CHECK (ping_timeout_minutes > 0),
                auto_close_alert INTEGER NOT NULL DEFAULT 1 CHECK (auto_close_alert IN (0, 1)),
                sla_enabled INTEGER NOT NULL DEFAULT 0 CHECK (sla_enabled IN (0, 1)),
                sla_availability_pct REAL CHECK (sla_availability_pct BETWEEN 0 AND 100),
            sla_response_time_ms REAL,
            sla_ping_latency_ms REAL,
            identity_signature TEXT,
            created_at timestamp NOT NULL,
            updated_at timestamp NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_targets_group_id ON targets(group_id)")

    ensure_target_network_ports_table(conn)

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
            failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
            last_attempt_at timestamp NOT NULL,
            locked_until timestamp,
            created_at timestamp NOT NULL
        )
        """
    )

    # Current status cache - updated by scheduler after each check
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS target_status (
            target_id INTEGER PRIMARY KEY,
            ping_up INTEGER CHECK (ping_up IN (0, 1)),
            ping_latency_ms REAL,
            ping_checked_at timestamp,
            http_up INTEGER CHECK (http_up IN (0, 1)),
            http_status INTEGER,
            http_latency_ms REAL,
            http_checked_at timestamp,
            tcp_up INTEGER CHECK (tcp_up IN (0, 1)),
            tcp_latency_ms REAL,
            tcp_checked_at timestamp,
            tcp_port_status TEXT,
            cert_days_left INTEGER,
            cert_checked_at timestamp,
            uptime_percent REAL,
            uptime_percent_30d REAL,
            overall_status TEXT NOT NULL DEFAULT 'unknown' CHECK (overall_status IN ({_OVERALL_STATUS_SQL})),
            last_down_at timestamp,
            updated_at timestamp NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
        )
        """
    )

    # Scheduler run tracking for idempotency and crash recovery
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_target_alert_users_user ON target_alert_users(user_id)")

    # Alert recipients table (phone contacts for Signal notifications + email)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
            created_at timestamp NOT NULL,
            updated_at timestamp NOT NULL,
            enable_for_all_targets INTEGER NOT NULL DEFAULT 0 CHECK (enable_for_all_targets IN (0, 1)),
            CHECK (phone IS NOT NULL OR email IS NOT NULL)
        )
        """
    )
    # Partial unique: several recipients without a phone stay possible, a number exists once.
    conn.execute(_RECIPIENT_PHONE_UNIQUE_DDL)
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_target_recipients_recipient ON target_recipients(recipient_id)")

    # Downtime alerts table - tracks all downtime events with UUID
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            target_id INTEGER NOT NULL,
            target_name TEXT NOT NULL,
            failed_service TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
            active_incident INTEGER NOT NULL DEFAULT 1 CHECK (active_incident IN (0, 1)),
            started_at timestamp NOT NULL,
            ended_at timestamp,
            duration_seconds INTEGER,
            recipients_notified TEXT,
            is_acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (is_acknowledged IN (0, 1)),
            acknowledged_by TEXT,
            acknowledged_at timestamp,
            created_at timestamp NOT NULL,
            updated_at timestamp NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
        )
        """
    )
    # Composite (equality column, started_at): serves target/status filters AND the ORDER BY
    # started_at DESC without a sort; the plain target_id/status indexes are their prefixes.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_target_started ON alerts(target_id, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status_started ON alerts(status, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_started_at ON alerts(started_at)")
    conn.execute(ALERT_SUMMARY_INDEXES[0])
    conn.execute(ALERT_SUMMARY_INDEXES[1])

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
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
            progress INTEGER DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
            filename TEXT,
            error TEXT,
            created_at timestamp NOT NULL,
            completed_at timestamp,
            expires_at timestamp,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_jobs_expires ON report_jobs(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_jobs_user ON report_jobs(user_id)")

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_alias_prio ON pop_locations(alias_code, priority, confidence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_canonical ON pop_locations(canonical_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_retired ON pop_locations(retired)")
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
            seen_count      INTEGER NOT NULL DEFAULT 1 CHECK(seen_count >= 0),
            last_seen       TEXT NOT NULL,
            reviewed        INTEGER NOT NULL DEFAULT 0 CHECK(reviewed IN (0, 1)),
            created_at      TEXT NOT NULL,
            hostname        TEXT,
            public_ip       TEXT,
            parser_context  TEXT,
            proposal_source TEXT,
            proposal_lat   REAL,
            proposal_lon   REAL,
            independent_count INTEGER NOT NULL DEFAULT 0,
            conflict_count INTEGER NOT NULL DEFAULT 0,
            quality_score INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_evidence TEXT,
            approved_location_id INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pop_candidates_count ON pop_alias_candidates(seen_count)")
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

    for ddl in IXP_TABLE_DDL:
        conn.execute(ddl)

    ensure_notification_queue_table(conn)
    ensure_notifications_tables(conn)
    ensure_alert_dispatch_outbox_table(conn)

    # Per-user dashboard layout: one row per placed widget. The CHECKs mirror the API validation
    # (x 0..11, w 1..12, x+w<=12, h 1..100, y+h<=1000 = DASHBOARD_MAX_Y in dashboard_layouts.py).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_layouts (
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

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_targets_identity_signature ON targets(identity_signature)")


# Schema versioning (PRAGMA user_version)

_RECIPIENT_PHONE_UNIQUE_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_recipients_phone ON recipients(phone) "
    "WHERE phone IS NOT NULL AND phone != ''"
)


def _ensure_unique_recipient_phone(conn: sqlite3.Connection) -> None:
    """Upgrade a non-unique ``idx_recipients_phone`` of a restamped former version-10 database to the unique one.

    Existing duplicates are only reported; nothing is deleted and the old index stays in place.
    Until they are merged, create_recipient()/update_recipient() still refuse any new duplicate
    atomically (DuplicatePhoneError), so the set of duplicates can only shrink.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_recipients_phone'"
    ).fetchone()
    if row is None or "UNIQUE" in (row[0] or "").upper():
        return
    duplicates = conn.execute(
        "SELECT COUNT(*) FROM (SELECT phone FROM recipients WHERE phone IS NOT NULL AND phone != '' "
        "GROUP BY phone HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if duplicates:
        _log.warning(
            "recipients: %d phone number(s) exist more than once; the unique phone index was not created. "
            "Existing duplicates are kept and new ones are rejected. Edit or delete the duplicate recipients "
            "(Settings > Recipients), then restart to enable the unique index.",
            duplicates,
        )
        return
    conn.execute("DROP INDEX idx_recipients_phone")
    conn.execute(_RECIPIENT_PHONE_UNIQUE_DDL)


# The current complete layout is the new version-1 baseline. Only an exact former-current version-10
# layout is restamped; other historical layouts are refused without changes.
SCHEMA_VERSION = 1

# Dashboard summary (resolved / acknowledged in the last 24 h): range scans on their own timestamps.
ALERT_SUMMARY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_alerts_status_ended ON alerts(status, ended_at)",
    "CREATE INDEX IF NOT EXISTS idx_alerts_acknowledged_at ON alerts(acknowledged_at) WHERE is_acknowledged = 1",
)


# IXP peering-LAN prefixes imported from PeeringDB (ix, ixlan, ixpfx, ixfac, fac). A prefix proves
# IXP membership, never a router position.
# Only status "ok" records are stored; a missing record means withdrawn. ``ixp_snapshot`` is the single row
# of the last complete, validated import.
IXP_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS ixp_exchanges (
        ix_id           INTEGER PRIMARY KEY,
        name            TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 128),
        city            TEXT CHECK(city IS NULL OR length(city) <= 64),
        country         TEXT CHECK(country IS NULL OR length(country) = 2),
        location_class  TEXT NOT NULL
            CHECK(location_class IN ('metro', 'distributed', 'conflicting', 'unknown')),
        ref_city        TEXT CHECK(ref_city IS NULL OR length(ref_city) <= 64),
        ref_country     TEXT CHECK(ref_country IS NULL OR length(ref_country) = 2),
        ref_lat         REAL CHECK(ref_lat IS NULL OR ref_lat BETWEEN -90 AND 90),
        ref_lon         REAL CHECK(ref_lon IS NULL OR ref_lon BETWEEN -180 AND 180),
        ref_radius_km   REAL CHECK(ref_radius_km IS NULL OR ref_radius_km > 0),
        facility_count  INTEGER NOT NULL DEFAULT 0 CHECK(facility_count >= 0),
        source          TEXT NOT NULL,
        import_run_id   TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        CHECK(location_class != 'metro' OR (
            ref_city IS NOT NULL AND ref_country IS NOT NULL
            AND ref_lat IS NOT NULL AND ref_lon IS NOT NULL AND ref_radius_km IS NOT NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ixp_prefixes (
        prefix          TEXT PRIMARY KEY,
        ip_version      INTEGER NOT NULL CHECK(ip_version IN (4, 6)),
        prefix_length   INTEGER NOT NULL CHECK(prefix_length BETWEEN 1 AND 128),
        ix_id           INTEGER NOT NULL REFERENCES ixp_exchanges(ix_id) ON DELETE CASCADE,
        ixlan_id        INTEGER NOT NULL,
        source          TEXT NOT NULL,
        import_run_id   TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ixp_prefixes_ix ON ixp_prefixes(ix_id)",
    """
    CREATE TABLE IF NOT EXISTS ixp_snapshot (
        id              INTEGER PRIMARY KEY CHECK(id = 1),
        import_run_id   TEXT NOT NULL,
        completed_at    TEXT NOT NULL,
        exchange_count  INTEGER NOT NULL CHECK(exchange_count >= 0),
        prefix_count    INTEGER NOT NULL CHECK(prefix_count >= 0),
        source          TEXT NOT NULL
    )
    """,
)


# IncompatibleSchemaError.kind values
SCHEMA_FUTURE = "future_schema"


SCHEMA_UNVERSIONED = "unversioned_database"


SCHEMA_NO_MIGRATION_PATH = "missing_migration_path"


SCHEMA_DRIFT = "schema_drift"


SCHEMA_MIGRATION_FAILED = "migration_failed"


SCHEMA_CORRUPT = "corrupt_database"


class IncompatibleSchemaError(RuntimeError):
    """The database file cannot be used by this release; it was left untouched (``kind``: see SCHEMA_*)."""

    def __init__(self, message: str, *, kind: str = SCHEMA_DRIFT, version: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.version = version


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the stored PRAGMA user_version (0 = never stamped)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
