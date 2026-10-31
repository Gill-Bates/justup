#!/usr/bin/env python3
#
# tests/test_db_maintenance.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite maintenance contracts: batched retention, query plans, shared queue schema, constraints."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.db import pop_lookup, retention
from app.db import sqlite as sqlite_db
from app.services.notifications import spooler as spooler_mod
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite_db.connect(tmp_path / "m.db")
    sqlite_db.init_schema(c)
    yield c
    sqlite_db.close_connection(c)


def _plan(conn, sql, params=()):
    return " ; ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())


def _queue(conn, status, age_days, n=1):
    ts = (utcnow() - timedelta(days=age_days)).isoformat()
    conn.executemany(
        "INSERT INTO notification_queue (notification_type, channel_type, recipient_name, recipient_address, payload,"
        " status, next_retry_at, created_at, updated_at) VALUES ('a','email','r','a@b','{}',?,?,?,?)",
        [(status, ts, ts, ts)] * n,
    )
    conn.commit()


def test_critical_queries_use_indexes_not_table_scans(conn):
    now = utcnow()
    plans = {
        "alerts_target": _plan(conn, "SELECT * FROM alerts WHERE target_id=? ORDER BY started_at DESC LIMIT 50", (1,)),
        "alerts_status": _plan(
            conn, "SELECT * FROM alerts WHERE status=? AND started_at>=? ORDER BY started_at DESC", ("open", now)
        ),
        "queue_due": _plan(
            conn,
            "SELECT id FROM notification_queue WHERE (status='pending' AND next_retry_at<=?)"
            " OR (status='processing' AND updated_at<?)",
            ("x", "y"),
        ),
        "queue_prune": _plan(
            conn, "SELECT rowid FROM notification_queue WHERE status IN ('sent','failed') AND updated_at<?", ("x",)
        ),
        "token": _plan(conn, "SELECT * FROM auth_tokens WHERE token_hash=?", ("t",)),
        "token_expiry": _plan(conn, "SELECT rowid FROM auth_tokens WHERE expires_at<=?", ("x",)),
        "pop_alias": _plan(conn, "SELECT * FROM pop_locations WHERE alias_code IN (?,?) AND retired=0", ("a", "b")),
    }
    for name, plan in plans.items():
        assert "SEARCH" in plan, (name, plan)
        assert "SCAN" not in plan.replace("SCAN alerts USING", ""), (name, plan)
    assert "TEMP B-TREE" not in plans["alerts_target"] + plans["alerts_status"]


def test_no_index_is_a_prefix_duplicate_of_another(conn):
    seen: dict[str, list[tuple[str, ...]]] = {}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        cols = [
            tuple(c[2] for c in conn.execute(f"PRAGMA index_info({i[1]})"))
            for i in conn.execute(f"PRAGMA index_list({table})")
        ]
        seen[table] = cols
        for a in cols:
            for b in cols:
                assert a == b or not (len(a) < len(b) and b[: len(a)] == a), (table, a, b)


def test_spooler_and_init_schema_share_one_queue_definition(tmp_path):
    raw = sqlite_db.connect(tmp_path / "q.db")
    sqlite_db.ensure_wal_mode(raw)
    spooler_mod.NotificationSpooler(lambda: sqlite_db.connect(tmp_path / "q.db"))
    spooler_sql = raw.execute("SELECT sql FROM sqlite_master WHERE name='notification_queue'").fetchone()[0]
    full = sqlite_db.connect(tmp_path / "f.db")
    sqlite_db.init_schema(full)
    assert full.execute("SELECT sql FROM sqlite_master WHERE name='notification_queue'").fetchone()[0] == spooler_sql
    sqlite_db.close_connection(raw)
    sqlite_db.close_connection(full)


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            (
                "INSERT INTO notification_queue (notification_type, channel_type, recipient_name, recipient_address, "
                "payload, status, next_retry_at, created_at, updated_at) "
                "VALUES ('a','email','r','a','{}','bogus','x','x','x')"
            ),
            (),
        ),
        (
            (
                "INSERT INTO notification_queue (notification_type, channel_type, recipient_name, recipient_address, "
                "payload, retry_count, next_retry_at, created_at, updated_at) "
                "VALUES ('a','email','r','a','{}',-1,'x','x','x')"
            ),
            (),
        ),
        ("INSERT INTO users (username, password_hash, is_admin, created_at) VALUES ('u','h',2,?)", (utcnow(),)),
        (
            "INSERT INTO login_attempts (ip_address, failed_count, last_attempt_at, created_at) VALUES ('1',-1,?,?)",
            (utcnow(), utcnow()),
        ),
    ],
)
def test_db_constraints_reject_invalid_state(conn, sql, params):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_retention_prunes_in_batches_idempotently_and_keeps_live_rows(conn, monkeypatch):
    monkeypatch.setattr(retention, "BATCH_SIZE", 7)
    monkeypatch.setattr(retention.time, "sleep", lambda _s: None)
    _queue(conn, "sent", 30, n=20)
    _queue(conn, "failed", 30, n=3)
    _queue(conn, "pending", 30)
    _queue(conn, "processing", 30)
    _queue(conn, "sent", 1, n=2)
    first = retention.run_retention(conn)
    assert first.complete
    assert first.deleted["notification_queue"] == 23
    left = dict(conn.execute("SELECT status, COUNT(*) FROM notification_queue GROUP BY status").fetchall())
    assert left == {"pending": 1, "processing": 1, "sent": 2}
    assert retention.run_retention(conn).total_deleted == 0


def test_retention_keeps_open_and_recent_alerts_and_drops_old_resolved(conn):
    conn.execute(
        "INSERT INTO targets (name, interval_seconds, retention_days, enable_ping, enable_http_check,"
        " enable_cert_expiration, host, created_at, updated_at) VALUES ('t',60,30,1,0,0,'h',?,?)",
        (utcnow(), utcnow()),
    )
    old = utcnow() - timedelta(days=retention.ALERT_RETENTION_DAYS + 5)
    for aid, status, started in [("a", "resolved", old), ("b", "open", old), ("c", "resolved", utcnow())]:
        conn.execute(
            "INSERT INTO alerts (id, target_id, target_name, failed_service, status, started_at, created_at, "
            "updated_at)"
            " VALUES (?,1,'t','http',?,?,?,?)",
            (aid, status, started, started, started),
        )
    conn.commit()
    retention.run_retention(conn)
    assert {r[0] for r in conn.execute("SELECT id FROM alerts")} == {"b", "c"}


def test_failed_retention_task_is_reported_and_does_not_block_others(conn, monkeypatch):
    _queue(conn, "sent", 30)
    real = retention.delete_in_batches

    def flaky(c, table, *a, **k):
        if table == "auth_tokens":
            raise sqlite3.OperationalError("database is locked")
        return real(c, table, *a, **k)

    monkeypatch.setattr(retention, "delete_in_batches", flaky)
    report = retention.run_retention(conn)
    assert report.failed == ["auth_tokens"]
    assert not report.complete
    assert report.deleted["notification_queue"] == 1


def test_delete_in_batches_rejects_unknown_table(conn):
    with pytest.raises(ValueError):
        retention.delete_in_batches(conn, "users", "1=1")


def test_db_stats_and_checkpoint_are_measurable(conn):
    _queue(conn, "pending", 0, n=3)
    stats = retention.collect_db_stats(conn, quick_check=True)
    assert stats["queue"] == {"pending": 3}
    assert stats["quick_check"] == ["ok"]
    assert stats["journal_mode"] == "wal"
    assert stats["synchronous"] == "FULL"
    wal = retention.checkpoint_wal(conn)
    assert wal["mode"] == "PASSIVE"
    assert retention.checkpoint_wal(conn, truncate_threshold=0)["wal_bytes_after"] == 0


def test_indexed_hostname_lookup_matches_full_scan_reference(conn):
    rows = [("fra", "fra"), ("fra2", "fra"), ("ams1", "ams"), ("lhr", "lhr"), ("nyc", "nyc"), ("par01", "par")]
    for alias, canon in rows:
        conn.execute(
            "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence,"
            " priority, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (canon, alias, "c", "DE", 1.0, 2.0, "seed", 100, 1, "x", "x"),
        )
    conn.commit()
    reference = """SELECT alias_code FROM pop_locations WHERE retired = 0 AND (? LIKE '%.' || alias_code || '.%'
        OR (alias_code GLOB '*[0-9]*' AND ? LIKE '%.' || alias_code || '%'))
        ORDER BY CASE WHEN instr(?, '.' || alias_code || '.') > 0 THEN instr(?, '.' || alias_code || '.')
        ELSE instr(?, '.' || alias_code) END ASC, length(alias_code) DESC, priority ASC, confidence DESC LIMIT 1"""
    for host in [
        "ae1.fra2.edge.example.net",
        "core-ams1-01.example.net",
        "lhr.example.com",
        "xx-par01-yy.net",
        "nothing.example.org",
        "nyc_gw.example.net",
        "fra",
        "FRA2.Example.Net.",
        "ams10.example.net",
    ]:
        pad = "." + host.lower().strip().strip(".").replace("-", ".").replace("_", ".") + "."
        ref = conn.execute(reference, (pad,) * 5).fetchone()
        got = pop_lookup.lkp_h(conn, host)
        assert (got["alias_code"] if got else None) == (ref[0] if ref else None), host
