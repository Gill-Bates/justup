#!/usr/bin/env python3
#
# app/db/sqlite/schema_v1.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Frozen DDL of schema version 1 (also the layout of the former stamp 10).

This literal is a historical record used only to recognise and migrate version-1 databases. It is deliberately
not derived from the current baseline: later schema changes must never alter what "version 1" means. Do not edit.
"""

# ruff: noqa: E501 - verbatim historical DDL
from __future__ import annotations

# Every table and index except ``recipients`` and its indexes (see below).
_V1_OTHER_DDL: tuple[str, ...] = (
    """CREATE TABLE users (
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
        )""",
    """CREATE TABLE auth_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at timestamp NOT NULL,
            max_expires_at timestamp NOT NULL,
            created_at timestamp NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""",
    """CREATE INDEX idx_auth_tokens_expires_at ON auth_tokens(expires_at)""",
    """CREATE INDEX idx_auth_tokens_user_id ON auth_tokens(user_id)""",
    """CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at timestamp NOT NULL
        )""",
    """CREATE TABLE targets (
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
        )""",
    """CREATE INDEX idx_targets_group_id ON targets(group_id)""",
    """CREATE TABLE target_network_ports (
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
        )""",
    """CREATE TABLE groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at timestamp NOT NULL,
            updated_at timestamp NOT NULL
        )""",
    """CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL UNIQUE,
            failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
            last_attempt_at timestamp NOT NULL,
            locked_until timestamp,
            created_at timestamp NOT NULL
        )""",
    """CREATE TABLE target_status (
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
            overall_status TEXT NOT NULL DEFAULT 'unknown' CHECK (overall_status IN ('up', 'down', 'unknown', 'partial', 'error', 'disabled')),
            last_down_at timestamp,
            updated_at timestamp NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
        )""",
    """CREATE TABLE scheduler_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        )""",
    """CREATE TABLE target_locks (
            target_id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            locked_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE
        )""",
    """CREATE INDEX idx_target_locks_expires ON target_locks(expires_at)""",
    """CREATE TABLE target_alert_users (
            target_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (target_id, user_id),
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""",
    """CREATE INDEX idx_target_alert_users_user ON target_alert_users(user_id)""",
    """CREATE TABLE target_recipients (
            target_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            PRIMARY KEY (target_id, recipient_id),
            FOREIGN KEY(target_id) REFERENCES targets(id) ON DELETE CASCADE,
            FOREIGN KEY(recipient_id) REFERENCES recipients(id) ON DELETE CASCADE
        )""",
    """CREATE INDEX idx_target_recipients_recipient ON target_recipients(recipient_id)""",
    """CREATE TABLE alerts (
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
        )""",
    """CREATE INDEX idx_alerts_target_started ON alerts(target_id, started_at)""",
    """CREATE INDEX idx_alerts_status_started ON alerts(status, started_at)""",
    """CREATE INDEX idx_alerts_started_at ON alerts(started_at)""",
    """CREATE INDEX idx_alerts_status_ended ON alerts(status, ended_at)""",
    """CREATE INDEX idx_alerts_acknowledged_at ON alerts(acknowledged_at) WHERE is_acknowledged = 1""",
    """CREATE TABLE monitor_quality (
            ts timestamp PRIMARY KEY,
            score REAL,
            median_latency_ms REAL,
            success_ratio REAL NOT NULL,
            state TEXT NOT NULL
        )""",
    """CREATE TABLE monitor_quality_latest (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            score REAL,
            state TEXT NOT NULL,
            updated_at timestamp NOT NULL
        )""",
    """CREATE TABLE report_jobs (
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
        )""",
    """CREATE INDEX idx_report_jobs_expires ON report_jobs(expires_at)""",
    """CREATE INDEX idx_report_jobs_user ON report_jobs(user_id)""",
    """CREATE TABLE pop_locations (
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
        )""",
    """CREATE INDEX idx_pop_alias_prio ON pop_locations(alias_code, priority, confidence)""",
    """CREATE INDEX idx_pop_canonical ON pop_locations(canonical_code)""",
    """CREATE INDEX idx_pop_retired ON pop_locations(retired)""",
    """CREATE INDEX idx_pop_import_run ON pop_locations(import_run_id)""",
    """CREATE INDEX idx_pop_source_retired ON pop_locations(source, retired)""",
    """CREATE TABLE pop_alias_candidates (
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
        )""",
    """CREATE INDEX idx_pop_candidates_count ON pop_alias_candidates(seen_count)""",
    """CREATE TABLE pop_candidate_observations (
            candidate_id INTEGER NOT NULL REFERENCES pop_alias_candidates(id),
            measurement_key TEXT NOT NULL,
            context_key TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id, measurement_key)
        )""",
    """CREATE INDEX idx_pop_observations_context ON pop_candidate_observations(candidate_id, context_key)""",
    """CREATE TABLE pop_candidate_reviews (
            id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES pop_alias_candidates(id),
            action TEXT NOT NULL CHECK(action IN ('approve', 'reject', 'revoke')),
            actor TEXT NOT NULL,
            evidence TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            location_id INTEGER
        )""",
    """CREATE TABLE pop_city_evidence (
            city_code TEXT NOT NULL,
            city TEXT NOT NULL,
            country TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            facility_count INTEGER NOT NULL,
            position_source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(city_code, country)
        )""",
    """CREATE TABLE pop_import_runs (
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
        )""",
    """CREATE INDEX idx_pop_runs_source ON pop_import_runs(source)""",
    """CREATE INDEX idx_pop_runs_status ON pop_import_runs(status)""",
    """CREATE TABLE ixp_exchanges (
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
    )""",
    """CREATE TABLE ixp_prefixes (
        prefix          TEXT PRIMARY KEY,
        ip_version      INTEGER NOT NULL CHECK(ip_version IN (4, 6)),
        prefix_length   INTEGER NOT NULL CHECK(prefix_length BETWEEN 1 AND 128),
        ix_id           INTEGER NOT NULL REFERENCES ixp_exchanges(ix_id) ON DELETE CASCADE,
        ixlan_id        INTEGER NOT NULL,
        source          TEXT NOT NULL,
        import_run_id   TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
    """CREATE INDEX idx_ixp_prefixes_ix ON ixp_prefixes(ix_id)""",
    """CREATE TABLE ixp_snapshot (
        id              INTEGER PRIMARY KEY CHECK(id = 1),
        import_run_id   TEXT NOT NULL,
        completed_at    TEXT NOT NULL,
        exchange_count  INTEGER NOT NULL CHECK(exchange_count >= 0),
        prefix_count    INTEGER NOT NULL CHECK(prefix_count >= 0),
        source          TEXT NOT NULL
    )""",
    """CREATE TABLE notification_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notification_type TEXT NOT NULL,
        channel_type TEXT NOT NULL,
        recipient_id INTEGER,
        recipient_name TEXT NOT NULL,
        recipient_address TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'processing', 'sent', 'failed')),
        retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
        next_retry_at TEXT NOT NULL,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        target_id INTEGER,
        limited_attempts INTEGER NOT NULL DEFAULT 0 CHECK (limited_attempts >= 0)
    )""",
    """CREATE TABLE notification_channel_state (
        channel_key TEXT PRIMARY KEY,
        last_attempt_at TEXT,
        cooldown_until TEXT,
        cooldown_reason TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE INDEX idx_notification_queue_status_retry ON notification_queue (status, next_retry_at)""",
    """CREATE INDEX idx_notification_queue_status_updated ON notification_queue (status, updated_at)""",
    """CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        category TEXT NOT NULL
            CHECK (category IN ('monitoring', 'reports', 'delivery', 'security', 'activity')),
        severity TEXT NOT NULL CHECK (severity IN ('info', 'success', 'warning', 'error')),
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        created_at timestamp NOT NULL,
        resource_type TEXT
            CHECK (resource_type IS NULL OR resource_type IN ('target', 'alert', 'report_job', 'queue')),
        resource_id TEXT,
        target_id INTEGER,
        actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        actor_name TEXT,
        audience TEXT NOT NULL CHECK (audience IN ('admins', 'user', 'target')),
        recipient_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        event_count INTEGER NOT NULL DEFAULT 1 CHECK (event_count >= 1),
        idempotency_key TEXT NOT NULL UNIQUE,
        CHECK (audience <> 'user' OR recipient_user_id IS NOT NULL),
        CHECK (audience <> 'target' OR target_id IS NOT NULL)
    )""",
    """CREATE INDEX idx_notifications_recipient ON notifications (recipient_user_id, id DESC)""",
    """CREATE INDEX idx_notifications_category ON notifications (category, id DESC)""",
    """CREATE INDEX idx_notifications_audience ON notifications (audience, id DESC)""",
    """CREATE INDEX idx_notifications_created ON notifications (created_at)""",
    """CREATE TABLE notification_reads (
        notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        read_at timestamp NOT NULL,
        PRIMARY KEY (notification_id, user_id)
    ) WITHOUT ROWID
    """,
    """CREATE INDEX idx_notification_reads_user ON notification_reads (user_id, notification_id)""",
    """CREATE TABLE notification_dismissals (
        notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        dismissed_at timestamp NOT NULL,
        PRIMARY KEY (notification_id, user_id)
    ) WITHOUT ROWID
    """,
    """CREATE INDEX idx_notification_dismissals_user ON notification_dismissals (user_id, notification_id)""",
    """CREATE TABLE alert_dispatch_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('open', 'resolved')),
    target_id INTEGER NOT NULL,
    target_name TEXT NOT NULL,
    failed_service TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (alert_id, event_type)
)""",
    """CREATE INDEX idx_alert_dispatch_outbox_pending ON alert_dispatch_outbox(status, created_at, id)""",
    """CREATE TABLE dashboard_layouts (
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
        """,
    """CREATE UNIQUE INDEX idx_targets_identity_signature ON targets(identity_signature)""",
)

_V1_RECIPIENTS_DDL: tuple[str, ...] = (
    """CREATE TABLE recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
            created_at timestamp NOT NULL,
            updated_at timestamp NOT NULL,
            enable_for_all_targets INTEGER NOT NULL DEFAULT 0 CHECK (enable_for_all_targets IN (0, 1)),
            CHECK (phone IS NOT NULL OR email IS NOT NULL)
        )""",
    """CREATE UNIQUE INDEX idx_recipients_phone ON recipients(phone) WHERE phone IS NOT NULL AND phone != ''""",
    "CREATE INDEX idx_recipients_email ON recipients(email)",
    "CREATE INDEX idx_recipients_enabled ON recipients(is_enabled)",
)

V1_DDL: tuple[str, ...] = _V1_OTHER_DDL + _V1_RECIPIENTS_DDL
