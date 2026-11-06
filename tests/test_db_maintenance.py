#!/usr/bin/env python3
#
# tests/test_db_maintenance.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite maintenance contracts: batched retention, query plans, shared queue schema, constraints."""

from __future__ import annotations

import sqlite3
import time
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


def test_pop_candidate_retention_preserves_reviews_and_deletes_unreviewed_atomically(conn) -> None:
    old = (utcnow() - timedelta(days=retention.POP_CANDIDATE_UNREVIEWED_RETENTION_DAYS + 2)).isoformat()
    pending = conn.execute(
        "INSERT INTO pop_alias_candidates (alias_code, last_seen, created_at) VALUES ('old', ?, ?)", (old, old)
    ).lastrowid
    reviewed = conn.execute(
        "INSERT INTO pop_alias_candidates (alias_code, last_seen, created_at, reviewed, status) "
        "VALUES ('kept', ?, ?, 1, 'rejected')",
        (old, old),
    ).lastrowid
    learned = conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, "
        "confidence, priority, created_at, updated_at) "
        "VALUES ('apv','apv','Reviewed','DE',50,8,'learned',70,10,?,?)",
        (old, old),
    ).lastrowid
    approved = conn.execute(
        "INSERT INTO pop_alias_candidates "
        "(alias_code, last_seen, created_at, reviewed, status, approved_location_id) "
        "VALUES ('apv', ?, ?, 1, 'approved', ?)",
        (old, old, learned),
    ).lastrowid
    for candidate_id, measurement in ((pending, "a"), (reviewed, "b"), (approved, "c")):
        conn.execute(
            "INSERT INTO pop_candidate_observations "
            "(candidate_id, measurement_key, context_key, observed_at) VALUES (?, ?, ?, ?)",
            (candidate_id, measurement * 64, measurement * 64, old),
        )
    conn.execute(
        "INSERT INTO pop_candidate_reviews (candidate_id, action, actor, evidence, reviewed_at) "
        "VALUES (?, 'reject', 'reviewer', 'historical conflict', ?)",
        (reviewed, old),
    )
    conn.execute(
        "INSERT INTO pop_candidate_reviews (candidate_id, action, actor, evidence, reviewed_at, location_id) "
        "VALUES (?, 'approve', 'reviewer', 'independent evidence', ?, ?)",
        (approved, old, learned),
    )
    conn.commit()
    conn.execute(
        "CREATE TRIGGER fail_pending_delete BEFORE DELETE ON pop_alias_candidates "
        "WHEN old.alias_code = 'old' BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
    )
    failed = retention.run_retention(conn)
    assert failed.failed == ["pop_alias_candidates"]
    assert (
        conn.execute("SELECT COUNT(*) FROM pop_candidate_observations WHERE candidate_id = ?", (pending,)).fetchone()[0]
        == 1
    )
    conn.execute("DROP TRIGGER fail_pending_delete")

    completed = retention.run_retention(conn)
    assert completed.deleted["pop_alias_candidates"] == 1
    assert conn.execute("SELECT COUNT(*) FROM pop_alias_candidates WHERE id = ?", (pending,)).fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM pop_candidate_observations WHERE candidate_id = ?", (pending,)).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT status FROM pop_alias_candidates WHERE id = ?", (reviewed,)).fetchone()[0] == "rejected"
    assert (
        conn.execute("SELECT COUNT(*) FROM pop_candidate_reviews WHERE candidate_id = ?", (reviewed,)).fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT status FROM pop_alias_candidates WHERE id = ?", (approved,)).fetchone()[0] == "approved"
    assert (
        conn.execute("SELECT COUNT(*) FROM pop_candidate_reviews WHERE candidate_id = ?", (approved,)).fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT retired FROM pop_locations WHERE id = ?", (learned,)).fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_retention_keeps_open_and_recent_alerts_and_drops_old_resolved(conn):
    _insert_target(conn)
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


def _inbox(conn, age_days, *, key, n=1, event_count=1):
    ts = utcnow() - timedelta(days=age_days)
    for i in range(n):
        conn.execute(
            "INSERT INTO notifications (event_type, category, severity, title, created_at, audience,"
            " event_count, idempotency_key) VALUES ('t','activity','info','x',?,'admins',?,?)",
            (ts, event_count, f"{key}-{i}"),
        )
    conn.commit()


def _inbox_keys(conn):
    return {r[0] for r in conn.execute("SELECT idempotency_key FROM notifications")}


def test_inbox_retention_boundary_applies_to_read_and_unread(conn):
    days = retention.NOTIFICATION_INBOX_RETENTION_DAYS
    assert days == 180
    _inbox(conn, days - 1, key="d179")
    _inbox(conn, days + 1, key="d181")
    _inbox(conn, days + 1, key="d181-unread")
    # Exactly 180 days: created a moment ago, so the cutoff computed in run_retention is later
    _inbox(conn, days, key="d180")
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('u','h',?)", (utcnow(),))
    conn.execute(
        "INSERT INTO notification_reads SELECT id, 1, ? FROM notifications WHERE idempotency_key = 'd181-0'",
        (utcnow(),),
    )
    conn.commit()
    retention.run_retention(conn)
    assert _inbox_keys(conn) == {"d179-0"}


def test_inbox_retention_cascades_to_reads_and_dismissals(conn):
    _inbox(conn, 200, key="old")
    _inbox(conn, 1, key="new")
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES ('u','h',?)", (utcnow(),))
    conn.execute("INSERT INTO notification_reads SELECT id, 1, ? FROM notifications", (utcnow(),))
    conn.execute("INSERT INTO notification_dismissals SELECT id, 1, ? FROM notifications", (utcnow(),))
    conn.commit()
    retention.run_retention(conn)
    assert _inbox_keys(conn) == {"new-0"}
    assert conn.execute("SELECT COUNT(*) FROM notification_reads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM notification_dismissals").fetchone()[0] == 1


def _insert_target(conn, now=None) -> None:
    now = now or utcnow()
    conn.execute(
        "INSERT INTO targets (name, interval_seconds, retention_days, enable_ping, enable_http_check,"
        " enable_cert_expiration, host, created_at, updated_at) VALUES ('t',60,30,1,0,0,'h',?,?)",
        (now, now),
    )


def test_inbox_retention_runs_in_batches_across_runs(conn, monkeypatch):
    monkeypatch.setattr(retention.time, "sleep", lambda _s: None)
    _inbox(conn, 300, key="old", n=2500)  # more than two default batches
    count = lambda: conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]  # noqa: E731
    real = retention.delete_in_batches
    ticks = iter([0.0, 1e12])  # stop the first run after one batch

    with monkeypatch.context() as patch:

        def limited(c, table, where, params=(), **kw):
            if table == "notifications" and kw.get("deadline") is not None:
                patch.setattr(retention.time, "monotonic", lambda: next(ticks))
            return real(c, table, where, params, **kw)

        patch.setattr(retention, "delete_in_batches", limited)
        first = retention.run_retention(conn)
    assert not first.complete
    assert count() == 1500
    assert retention.run_retention(conn).complete
    assert count() == 0


def test_inbox_retention_keeps_alerts_queue_and_bumped_grouped_rows(conn):
    _insert_target(conn)
    old = utcnow() - timedelta(days=400)
    conn.execute(
        "INSERT INTO alerts (id, target_id, target_name, failed_service, status, started_at, created_at, updated_at)"
        " VALUES ('a',1,'t','http','open',?,?,?)",
        (old, old, old),
    )
    conn.commit()
    _queue(conn, "pending", 400)
    from app.db import notifications as inbox

    now = utcnow()
    inbox.emit_delivery_sent(conn, "email", now=now)
    inbox.emit_delivery_sent(conn, "email", now=now)  # bump within the same group window
    retention.run_retention(conn)
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM notification_queue").fetchone()[0] == 1
    assert conn.execute("SELECT event_count FROM notifications").fetchone()[0] == 2


def _bloat_and_free(conn, rows=3000):
    conn.execute("CREATE TABLE junk (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO junk (v) VALUES (?)", [("x" * 500,)] * rows)
    conn.commit()
    conn.execute("DELETE FROM junk")
    conn.commit()


def _free(conn):
    return conn.execute("PRAGMA freelist_count").fetchone()[0]


def test_new_database_uses_incremental_auto_vacuum(conn):
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == retention.AUTO_VACUUM_INCREMENTAL
    assert retention.collect_db_stats(conn)["auto_vacuum"] == "incremental"


def test_existing_database_with_auto_vacuum_off_is_not_converted(tmp_path):
    path = tmp_path / "old.db"
    first = sqlite_db.connect(path)
    sqlite_db.init_schema(first)
    first.execute("PRAGMA auto_vacuum=NONE")
    first.execute("VACUUM")  # test setup only: simulates a file created before v1.0.0
    sqlite_db.close_connection(first)
    c = sqlite_db.connect(path)
    sqlite_db.init_schema(c)
    assert c.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    _bloat_and_free(c)
    before = _free(c)
    assert retention.run_incremental_vacuum(c, min_free_pages=1) == 0
    assert _free(c) == before
    sqlite_db.close_connection(c)


def test_incremental_vacuum_is_bounded_and_reduces_free_pages(conn):
    _bloat_and_free(conn)
    before = _free(conn)
    assert before > 300
    assert retention.run_incremental_vacuum(conn, min_free_pages=1, min_free_ratio=0, max_pages=100) == 100
    assert _free(conn) == before - 100
    retention.run_incremental_vacuum(conn, min_free_pages=1, min_free_ratio=0, max_pages=10_000)
    assert _free(conn) == 0


def test_incremental_vacuum_skips_below_thresholds_and_inside_transaction(conn):
    _bloat_and_free(conn)
    before = _free(conn)
    assert retention.run_incremental_vacuum(conn, min_free_pages=before + 1, min_free_ratio=0) == 0
    assert retention.run_incremental_vacuum(conn, min_free_pages=1, min_free_ratio=1.1) == 0
    conn.execute("BEGIN IMMEDIATE")
    assert retention.run_incremental_vacuum(conn, min_free_pages=1, min_free_ratio=0) == 0
    conn.rollback()
    assert _free(conn) == before


def test_incremental_vacuum_skips_when_database_is_busy(conn, tmp_path):
    _bloat_and_free(conn)
    other = sqlite_db.connect(tmp_path / "m.db")
    other.execute("PRAGMA busy_timeout=50")
    other.execute("BEGIN IMMEDIATE")
    try:
        assert retention.run_incremental_vacuum(other, min_free_pages=1, min_free_ratio=0) == 0
    finally:
        other.rollback()
        sqlite_db.close_connection(other)
    c2 = sqlite_db.connect(tmp_path / "m.db")
    c2.execute("PRAGMA busy_timeout=50")
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert retention.run_incremental_vacuum(c2, min_free_pages=1, min_free_ratio=0) == 0
    finally:
        conn.rollback()
        sqlite_db.close_connection(c2)


def _alert(conn, aid, status, started, ended=None):
    conn.execute(
        "INSERT INTO alerts (id, target_id, target_name, failed_service, status, started_at, ended_at, created_at,"
        " updated_at) VALUES (?,1,'t','http',?,?,?,?,?)",
        (aid, status, started, ended, started, started),
    )


def test_resolved_alert_retention_counts_from_resolution_with_legacy_fallback(conn):
    now = utcnow()
    limit = retention.ALERT_RETENTION_DAYS
    long_ago = now - timedelta(days=limit + 70)
    _insert_target(conn, now)
    _alert(conn, "resolved-today", "resolved", long_ago, now)
    _alert(conn, "resolved-long-ago", "resolved", long_ago, now - timedelta(days=limit + 5))
    _alert(conn, "open", "open", long_ago)
    _alert(conn, "legacy-old", "resolved", long_ago)
    _alert(conn, "legacy-recent", "resolved", now - timedelta(days=5))
    conn.commit()
    retention.run_retention(conn)
    assert {r[0] for r in conn.execute("SELECT id FROM alerts")} == {"resolved-today", "open", "legacy-recent"}


def test_delivery_queue_rule_honours_the_fixed_reference_time(conn):
    fixed = utcnow() - timedelta(days=400)
    days = retention.NOTIFICATION_RETENTION_DAYS
    for age in (days + 1, days - 1):
        ts = (fixed - timedelta(days=age)).isoformat()
        conn.execute(
            "INSERT INTO notification_queue (notification_type, channel_type, recipient_name, recipient_address,"
            " payload, status, next_retry_at, created_at, updated_at) VALUES ('a','email','r','a@b','{}','sent',?,?,?)",
            (ts, ts, ts),
        )
    conn.commit()
    retention.run_retention(conn, now=fixed)
    left = [r[0] for r in conn.execute("SELECT updated_at FROM notification_queue")]
    assert left == [(fixed - timedelta(days=days - 1)).isoformat()]


def test_checkpoint_skips_truncate_while_a_reader_holds_the_wal(conn, tmp_path):
    _queue(conn, "pending", 0, n=50)
    reader = sqlite_db.connect(tmp_path / "m.db")
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM notification_queue").fetchone()  # pins the snapshot
        _queue(conn, "pending", 0, n=50)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        result = retention.checkpoint_wal(conn, truncate_threshold=0)
        conn.set_trace_callback(None)
    finally:
        reader.rollback()
        sqlite_db.close_connection(reader)
    assert result["mode"] == "PASSIVE"
    assert result["truncate_skipped"] is True
    assert not any("TRUNCATE" in sql for sql in statements)
    assert result["wal_bytes_after"] > 0


def test_truncate_wait_is_bounded_when_a_reader_holds_the_snapshot_after_the_last_write(conn, tmp_path):
    _queue(conn, "pending", 0, n=50)
    reader = sqlite_db.connect(tmp_path / "m.db")
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM notification_queue").fetchone()  # snapshot includes every write
        started = time.monotonic()
        result = retention.checkpoint_wal(conn, truncate_threshold=0)
        elapsed = time.monotonic() - started
    finally:
        reader.rollback()
        sqlite_db.close_connection(reader)
    assert elapsed < 2.0  # the connection's busy_timeout is 5 s
    assert result["truncate_skipped"] is True
    assert result["mode"] == "PASSIVE"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == sqlite_db.BUSY_TIMEOUT_MS


def test_checkpoint_truncates_a_large_wal_without_readers(conn):
    _queue(conn, "pending", 0, n=200)
    before = retention.collect_db_stats(conn)["wal_bytes"]
    assert before > 0
    result = retention.checkpoint_wal(conn, truncate_threshold=before - 1)
    assert result["mode"] == "TRUNCATE"
    assert result["truncate_skipped"] is False
    assert result["wal_bytes_after"] == 0


def test_missing_wal_counts_as_zero_bytes(conn, monkeypatch):
    real_stat = Path.stat

    def missing(self, *a, **k):
        if self.name.endswith("-wal"):
            raise FileNotFoundError(self.name)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", missing)
    assert retention.collect_db_stats(conn)["wal_bytes"] == 0


def test_unreadable_wal_size_is_unknown_not_zero_and_blocks_truncate(conn, monkeypatch, caplog):
    _queue(conn, "pending", 0, n=3)
    real_stat = Path.stat

    def denied(self, *a, **k):
        if self.name.endswith("-wal"):
            raise PermissionError("no access")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", denied)
    with caplog.at_level("WARNING", logger=retention.__name__):
        assert retention.collect_db_stats(conn)["wal_bytes"] is None
        result = retention.checkpoint_wal(conn, truncate_threshold=0)
    assert "Cannot read size" in caplog.text
    assert result["mode"] == "PASSIVE"
    assert result["wal_bytes_before"] is None
