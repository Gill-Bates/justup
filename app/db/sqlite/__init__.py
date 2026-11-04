#!/usr/bin/env python3
#
# app/db/sqlite/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite database access layer and schema initialization.

The former single module ``app/db/sqlite.py`` is split by domain; this package re-exports every
name so ``from app.db import sqlite as sqlite_db`` and ``sqlite_db.<name>`` keep working.

Layout (imports only point downwards, domain modules never import ``migrations``):

- ``connection``: connection registry, quiesce, pragmas, transactions
- ``schema_notifications`` / ``schema``: baseline DDL, schema version constants, error type
- ``layout``: structural schema layout reading and comparison
- ``migrations``: ``MIGRATIONS`` registry, ``check_schema_compatibility``, ``init_schema``
- domain modules: settings, target_status, auth_tokens, users, targets, groups, login_attempts,
  recipients, alerts, monitor_quality, report_jobs, scheduler_runs, bootstrap

Monkeypatching ``sqlite_db.<name>`` keeps working for internal callers: the package module forwards
an assignment to every submodule that holds the name (see ``_SqliteModule``).
"""

# Re-exports are the purpose of this module.
# ruff: noqa: F401
from __future__ import annotations

import sqlite3  # tests patch sqlite_db.sqlite3.connect
import sys
import time  # tests patch sqlite_db.time.sleep
from types import ModuleType

from . import (
    alerts,
    auth_tokens,
    bootstrap,
    connection,
    groups,
    layout,
    login_attempts,
    migrations,
    monitor_quality,
    network_ports,
    recipients,
    report_jobs,
    scheduler_runs,
    schema,
    schema_notifications,
    settings,
    target_status,
    targets,
    users,
)
from .alerts import (
    acknowledge_alert,
    acknowledge_all_open_alerts,
    create_alert,
    deactivate_alert,
    delete_alerts_by_target,
    delete_all_alerts,
    enqueue_alert_dispatch,
    get_alert,
    get_alerts_for_target_in_range,
    has_pending_alert_dispatches,
    list_active_alerts,
    list_alerts,
    list_open_alerts,
    list_pending_alert_dispatches,
    record_alert_dispatch_attempt,
    resolve_alert,
    summarize_alerts,
)
from .auth_tokens import (
    TOKEN_RENEW_MIN_DELTA,
    cleanup_expired_tokens,
    get_session_max_days,
    get_user_for_token,
    get_user_for_token_readonly,
    issue_token,
    revoke_token,
    revoke_tokens_for_user,
)
from .bootstrap import (
    _BOOTSTRAP_SPECIALS,
    _PRONOUNCEABLE_CONSONANTS,
    _PRONOUNCEABLE_VOWELS,
    BOOTSTRAP_ADMIN_USERNAME,
    _generate_pronounceable_password,
    _print_bootstrap_credentials,
    ensure_default_admin,
    reprint_pending_bootstrap_credentials,
)
from .connection import (
    _CONNECTIONS_LOCK,
    _OPEN_CONNECTIONS,
    _TX_LOCK,
    _TX_STATES,
    BUSY_TIMEOUT_MS,
    SYNCHRONOUS_MODE,
    DatabaseQuiescedError,
    _adapt_datetime,
    _convert_datetime,
    _deregister_if_closed,
    _is_closed,
    _log,
    _prune_closed_connections,
    _savepoint,
    _TxState,
    begin_quiesce,
    close_all_connections,
    close_connection,
    connect,
    end_quiesce,
    ensure_wal_mode,
    get_connection_stats,
    immediate_transaction,
    run_after_commit,
    transaction,
)
from .groups import (
    GroupNotEmptyError,
    GroupNotFoundError,
    count_targets_in_group,
    create_group,
    delete_group,
    delete_group_if_empty,
    get_group,
    get_group_by_name,
    list_groups,
    list_groups_with_counts,
    update_group,
)
from .layout import (
    _MAX_REPORTED_DIFFS,
    _SAFE_HINT,
    _SQL_TOKEN,
    _SQLITE_CORRUPT_CODES,
    _TOLERATED_COLUMN_DEFAULTS,
    _TOLERATED_INDEX_VARIANTS,
    _check_expressions,
    _database_file,
    _describe_column,
    _diff_layout,
    _drift_error,
    _expected_layout,
    _IndexShape,
    _Layout,
    _layout,
    _layout_problems,
    _normalise_sql,
    _short,
    _skip_quoted,
    _TableShape,
)
from .login_attempts import (
    _LOGIN_LOCK_MAX_SECONDS,
    clear_login_attempts,
    get_login_attempt,
    is_ip_locked,
    login_lock_seconds,
    record_failed_login,
)
from .migrations import (
    MIGRATIONS,
    Migration,
    _migrate_2_spooler_resilience,
    _migrate_3_alert_dispatch_outbox,
    _migrate_5_recipient_enable_for_all_targets,
    _missing_migrations,
    check_schema_compatibility,
    init_schema,
)
from .monitor_quality import (
    get_monitor_quality_latest,
    insert_monitor_quality,
    query_monitor_quality,
)
from .recipients import (
    DuplicatePhoneError,
    _assert_phone_free,
    assign_flagged_recipients,
    create_recipient,
    delete_recipient,
    get_recipient,
    get_recipient_by_phone,
    get_recipient_targets,
    get_target_recipient_ids,
    get_target_recipients,
    get_targets_by_recipient,
    list_recipients,
    set_target_recipients,
    update_recipient,
)
from .report_jobs import (
    _notify_report_stage,
    create_report_job,
    get_expired_job_ids,
    get_report_job,
    mark_stale_report_jobs_failed,
    prune_expired_report_jobs,
    update_report_job,
)
from .network_ports import all_network_ports, list_network_ports, sync_network_ports, update_network_port_status
from .scheduler_runs import (
    cleanup_stale_scheduler_runs,
    register_scheduler_run,
    release_target_lock,
    renew_target_lock,
    try_lock_target,
    update_scheduler_heartbeat,
)
from .schema import (
    _OVERALL_STATUS_SQL,
    _RECIPIENT_PHONE_UNIQUE_DDL,
    SCHEMA_CORRUPT,
    SCHEMA_DRIFT,
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
from .schema_notifications import (
    _ALERT_DISPATCH_OUTBOX_DDL,
    _NOTIFICATION_QUEUE_DDL,
    _NOTIFICATIONS_DDL,
    ensure_alert_dispatch_outbox_table,
    ensure_notification_queue_table,
    ensure_notifications_tables,
)
from .settings import (
    delete_setting,
    get_setting,
    set_setting,
)
from .target_status import (
    delete_target_status,
    get_all_target_statuses,
    get_target_status,
    set_target_status_snapshot,
)
from .targets import (
    _TARGET_SELECT,
    DuplicateTargetError,
    _assert_no_exact_duplicate,
    _resolve_group_id,
    create_target,
    delete_target,
    get_target,
    get_target_alert_users,
    list_targets,
    list_targets_paginated,
    set_target_alert_users,
    update_target,
)
from .users import (
    _discard_bootstrap_credentials_for,
    create_user,
    delete_user,
    get_all_users,
    get_first_admin,
    get_user,
    get_user_by_id,
    get_user_by_username,
    get_user_theme,
    set_user_active,
    set_user_theme,
    update_user,
    update_user_last_login,
    update_user_password,
)

_SUBMODULES = (
    connection,
    schema_notifications,
    schema,
    layout,
    migrations,
    settings,
    target_status,
    auth_tokens,
    users,
    targets,
    groups,
    login_attempts,
    recipients,
    alerts,
    monitor_quality,
    report_jobs,
    network_ports,
    scheduler_runs,
    bootstrap,
)


def _connection_state(name: str) -> property:
    """Data descriptor that reads and writes a counter owned by ``connection`` (never a stale copy)."""
    return property(lambda _self: getattr(connection, name), lambda _self, value: setattr(connection, name, value))


class _SqliteModule(ModuleType):
    """Package module that keeps ``sqlite_db.<name> = value`` visible to the submodules."""

    # connection.py rebinds these with ``global``; a re-exported copy would go stale.
    _CONNECTIONS_CLOSED = _connection_state("_CONNECTIONS_CLOSED")
    _CONNECTIONS_OPENED = _connection_state("_CONNECTIONS_OPENED")
    _PENDING_CONNECTS = _connection_state("_PENDING_CONNECTS")
    _QUIESCED = _connection_state("_QUIESCED")

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for submodule in _SUBMODULES:
            if name in vars(submodule):
                setattr(submodule, name, value)


sys.modules[__name__].__class__ = _SqliteModule
