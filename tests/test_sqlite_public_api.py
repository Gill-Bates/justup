#!/usr/bin/env python3
#
# tests/test_sqlite_public_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Public surface of ``app.db.sqlite`` after the split of the former ``app/db/sqlite.py`` module.

The name lists below were taken from the AST of the single-file module (commit 5a5507f) when it was
split into a package. Callers keep using ``sqlite_db.<name>``, ``from app.db.sqlite import <name>``
and ``monkeypatch.setattr(sqlite_db, ...)``, so all three must keep working.
"""

from __future__ import annotations

import contextlib
import inspect
import sqlite3

import pytest

from app.db import sqlite as sqlite_db

# name -> parameter spec: ``name`` required, ``name=`` has a default, ``*`` keyword-only marker.
FUNCTIONS: dict[str, str] = {
    "_adapt_datetime": "value",
    "_convert_datetime": "value",
    "connect": "db_path",
    "ensure_wal_mode": "conn",
    "_prune_closed_connections": "",
    "begin_quiesce": "*, keep=, timeout=",
    "end_quiesce": "",
    "_is_closed": "conn",
    "_deregister_if_closed": "conn",
    "close_connection": "conn",
    "close_all_connections": "*, settle_timeout=",
    "get_connection_stats": "",
    "run_after_commit": "conn, callback",
    "_savepoint": "conn, state",
    "transaction": "conn, *, immediate=",
    "immediate_transaction": "conn",
    "ensure_notification_queue_table": "conn",
    "ensure_alert_dispatch_outbox_table": "conn",
    "ensure_notifications_tables": "conn",
    "_create_baseline_schema": "conn",
    "_ensure_unique_recipient_phone": "conn",
    "assign_flagged_recipients": "conn, target_id",
    "get_schema_version": "conn",
    "_normalise_sql": "expression",
    "_skip_quoted": "sql, start",
    "_check_expressions": "sql",
    "_layout": "conn",
    "_expected_layout": "",
    "_describe_column": "column",
    "_short": "items",
    "_diff_layout": "actual, expected",
    "_database_file": "conn",
    "_layout_problems": "conn",
    "_drift_error": "conn, version, diffs",
    "check_schema_compatibility": "conn",
    "_missing_migrations": "current",
    "init_schema": "conn",
    "set_setting": "conn, key, value, *, auto_commit=",
    "get_setting": "conn, key, default=",
    "delete_setting": "conn, key",
    "set_target_status_snapshot": "conn, target_id, *, ping_up, ping_latency_ms, http_up, http_status, http_latency_ms, tcp_up, tcp_latency_ms, tcp_port_status=, cert_days_left, uptime_percent, uptime_percent_30d=, overall_status",  # noqa: E501
    "get_target_status": "conn, target_id",
    "get_all_target_statuses": "conn",
    "delete_target_status": "conn, target_id",
    "get_session_max_days": "conn",
    "cleanup_expired_tokens": "conn",
    "create_user": "conn, *, username, password_hash, is_admin, can_close_alerts=, must_change_password=",
    "get_user_by_username": "conn, username",
    "get_user_by_id": "conn, user_id",
    "get_user_theme": "conn, user_id",
    "set_user_theme": "conn, user_id, theme",
    "get_user": "conn, user_id",
    "get_all_users": "conn",
    "delete_user": "conn, user_id",
    "get_first_admin": "conn",
    "update_user": "conn, user_id, *, username, is_admin, can_close_alerts=",
    "set_user_active": "conn, user_id, *, is_active",
    "update_user_password": "conn, user_id, password_hash, *, must_change_password=, expected_hash=",
    "_discard_bootstrap_credentials_for": "conn, user_id",
    "update_user_last_login": "conn, user_id, ip_address=",
    "issue_token": "conn, *, user_id, token_hash, expires_at, max_expires_at",
    "revoke_token": "conn, *, token_hash",
    "revoke_tokens_for_user": "conn, user_id",
    "get_user_for_token": "conn, *, token_hash",
    "get_user_for_token_readonly": "conn, *, token_hash",
    "set_target_alert_users": "conn, target_id, user_ids",
    "get_target_alert_users": "conn, target_id",
    "_resolve_group_id": "conn, name",
    "list_targets": "conn",
    "list_targets_paginated": "conn, page=, page_size=",
    "get_target": "conn, target_id",
    "_assert_no_exact_duplicate": "conn, data, exclude_id=",
    "create_target": "conn, values, *, manage_transaction=",
    "update_target": "conn, *, target_id, patch, manage_transaction=",
    "delete_target": "conn, target_id",
    "list_groups": "conn",
    "list_groups_with_counts": "conn",
    "get_group": "conn, group_id",
    "get_group_by_name": "conn, name",
    "create_group": "conn, *, name, description=",
    "update_group": "conn, *, group_id, patch",
    "delete_group": "conn, group_id",
    "delete_group_if_empty": "conn, group_id",
    "count_targets_in_group": "conn, group_name",
    "get_login_attempt": "conn, ip_address",
    "login_lock_seconds": "failed_count",
    "record_failed_login": "conn, ip_address",
    "clear_login_attempts": "conn, ip_address",
    "is_ip_locked": "conn, ip_address",
    "list_recipients": "conn",
    "get_recipient": "conn, recipient_id",
    "get_recipient_by_phone": "conn, phone",
    "_assert_phone_free": "conn, phone, *, exclude_id=",
    "create_recipient": "conn, *, name, phone=, email=, is_enabled=, enable_for_all_targets=",
    "update_recipient": (
        "conn, recipient_id, *, name=, phone=, email=, is_enabled=, enable_for_all_targets=, clear_phone=, clear_email="
    ),
    "delete_recipient": "conn, recipient_id",
    "get_target_recipients": "conn, target_id",
    "get_target_recipient_ids": "conn, target_id",
    "set_target_recipients": "conn, target_id, recipient_ids, *, manage_transaction=",
    "get_targets_by_recipient": "conn",
    "get_recipient_targets": "conn, recipient_id",
    "create_alert": "conn, *, alert_id, target_id, target_name, failed_service, started_at, recipients_notified, dispatch_outbox=",  # noqa: E501
    "enqueue_alert_dispatch": "conn, *, alert_id, event_type, target_id, target_name, failed_service, started_at, ended_at=, duration_seconds=",  # noqa: E501
    "record_alert_dispatch_attempt": "conn, *, alert_id, event_type, succeeded, retry_delay_seconds=, error=",
    "list_pending_alert_dispatches": "conn, *, limit=",
    "has_pending_alert_dispatches": "conn",
    "resolve_alert": "conn, alert_id, ended_at, *, dispatch_outbox=",
    "deactivate_alert": "conn, alert_id",
    "list_open_alerts": "conn",
    "list_active_alerts": "conn",
    "acknowledge_alert": "conn, alert_id, acknowledged_by",
    "acknowledge_all_open_alerts": "conn, acknowledged_by",
    "list_alerts": "conn, *, target_id=, from_date=, to_date=, status=, page=, page_size=",
    "get_alert": "conn, alert_id",
    "get_alerts_for_target_in_range": "conn, target_id, from_date, to_date",
    "delete_all_alerts": "conn",
    "delete_alerts_by_target": "conn, target_id",
    "insert_monitor_quality": "conn, *, score, median_latency_ms, success_ratio, state",
    "get_monitor_quality_latest": "conn",
    "query_monitor_quality": "conn, *, since=, until=, limit=",
    "create_report_job": "conn, job_id, target_id, expires_hours=, user_id=",
    "_notify_report_stage": "conn, job_id, stage",
    "get_report_job": "conn, job_id",
    "update_report_job": "conn, job_id, *, status=, progress=, filename=, error=, completed=",
    "get_expired_job_ids": "conn, now",
    "prune_expired_report_jobs": "conn",
    "register_scheduler_run": "conn, run_id",
    "update_scheduler_heartbeat": "conn, run_id",
    "cleanup_stale_scheduler_runs": "conn, timeout_seconds=",
    "try_lock_target": "conn, target_id, run_id, ttl_seconds",
    "release_target_lock": "conn, target_id, run_id",
    "mark_stale_report_jobs_failed": "conn",
    "_print_bootstrap_credentials": "username, password",
    "reprint_pending_bootstrap_credentials": "conn",
    "_generate_pronounceable_password": "",
    "ensure_default_admin": "conn, credentials_path=",
}

CLASSES = (
    "DatabaseQuiescedError",
    "_TxState",
    "IncompatibleSchemaError",
    "Migration",
    "_TableShape",
    "_IndexShape",
    "DuplicateTargetError",
    "GroupNotFoundError",
    "GroupNotEmptyError",
    "DuplicatePhoneError",
)

# Module-level constants and shared state (the underscore ones are used by tests and tooling).
CONSTANTS = (
    "_OPEN_CONNECTIONS",
    "_CONNECTIONS_LOCK",
    "_CONNECTIONS_OPENED",
    "_CONNECTIONS_CLOSED",
    "_PENDING_CONNECTS",
    "BUSY_TIMEOUT_MS",
    "SYNCHRONOUS_MODE",
    "_QUIESCED",
    "_TX_STATES",
    "_TX_LOCK",
    "_NOTIFICATION_QUEUE_DDL",
    "_ALERT_DISPATCH_OUTBOX_DDL",
    "_NOTIFICATIONS_DDL",
    "_OVERALL_STATUS_SQL",
    "_RECIPIENT_PHONE_UNIQUE_DDL",
    "SCHEMA_VERSION",
    "SCHEMA_FUTURE",
    "SCHEMA_UNVERSIONED",
    "SCHEMA_NO_MIGRATION_PATH",
    "SCHEMA_DRIFT",
    "SCHEMA_MIGRATION_FAILED",
    "SCHEMA_CORRUPT",
    "MIGRATIONS",
    "_Layout",
    "_TOLERATED_COLUMN_DEFAULTS",
    "_TOLERATED_INDEX_VARIANTS",
    "_SQLITE_CORRUPT_CODES",
    "_MAX_REPORTED_DIFFS",
    "_SQL_TOKEN",
    "_SAFE_HINT",
    "TOKEN_RENEW_MIN_DELTA",
    "_TARGET_SELECT",
    "_LOGIN_LOCK_MAX_SECONDS",
    "BOOTSTRAP_ADMIN_USERNAME",
    "_PRONOUNCEABLE_VOWELS",
    "_PRONOUNCEABLE_CONSONANTS",
    "_BOOTSTRAP_SPECIALS",
    "_log",
)


def _spec(function) -> str:
    parts: list[str] = []
    star_emitted = False
    for p in inspect.signature(function).parameters.values():
        if p.kind is p.VAR_POSITIONAL:
            parts.append(f"*{p.name}")
            star_emitted = True
        elif p.kind is p.VAR_KEYWORD:
            parts.append(f"**{p.name}")
        else:
            if p.kind is p.KEYWORD_ONLY and not star_emitted:
                parts.append("*")
                star_emitted = True
            parts.append(p.name + ("=" if p.default is not p.empty else ""))
    return ", ".join(parts)


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_function_is_exported_with_the_same_signature(name: str) -> None:
    function = getattr(sqlite_db, name)
    assert callable(function)
    assert _spec(function) == FUNCTIONS[name]


@pytest.mark.parametrize("name", CLASSES)
def test_class_is_exported(name: str) -> None:
    assert inspect.isclass(getattr(sqlite_db, name))


@pytest.mark.parametrize("name", CONSTANTS)
def test_constant_is_exported(name: str) -> None:
    assert hasattr(sqlite_db, name)


def test_from_import_form_resolves() -> None:
    from app.db.sqlite import IncompatibleSchemaError, Migration, init_schema, transaction  # noqa: F401

    assert issubclass(IncompatibleSchemaError, RuntimeError)


def test_logger_name_is_unchanged() -> None:
    assert sqlite_db._log.name == "app.db.sqlite"  # noqa: SLF001 - tests address the logger by this name


def test_patching_a_package_name_reaches_internal_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth_tokens.get_session_max_days calls get_setting, which lives in another submodule."""
    monkeypatch.setattr(sqlite_db, "get_setting", lambda _conn, _key, _default=None: 7)
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        assert sqlite_db.get_session_max_days(conn) == 7


def test_connection_counters_are_read_and_written_through_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.sqlite import connection

    monkeypatch.setattr(sqlite_db, "_QUIESCED", True)
    assert connection._QUIESCED is True  # noqa: SLF001 - verifying the owner module sees the patch
    monkeypatch.undo()
    assert sqlite_db._QUIESCED is False  # noqa: SLF001
