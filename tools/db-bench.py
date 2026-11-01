#!/usr/bin/env python3
#
# tools/db-bench.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Offline SQLite benchmarks for justUp (never touches real data; always uses a temp DB).

Subcommands:
  plans   Index inventory, EXPLAIN QUERY PLAN and timings for the critical queries on synthetic data.
  sync    Compare PRAGMA synchronous=FULL against NORMAL (throughput, latency, WAL size, CPU, I/O).

Usage:
  .venv/bin/python tools/db-bench.py plans [--scale 1.0] [--tmpdir DIR]
  .venv/bin/python tools/db-bench.py sync  [--tmpdir DIR] [--writes 1500] [--seconds 8]

Use ``--tmpdir`` on a real disk for ``sync``: fsync on tmpfs is free and would hide the difference.
"""

# ruff: noqa: ISC004, E501, S311, B007, S608
# Benchmark script: long literal SQL, deterministic PRNG for synthetic data, console output by design.
from __future__ import annotations

import argparse
import os
import random
import resource
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import sqlite as sdb

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# Critical queries (SQL mirrors the application code; see docs/development/database-maintenance.md).
QUERIES: dict[str, tuple[str, tuple]] = {
    "target_list_page": (
        "SELECT * FROM targets ORDER BY CASE WHEN group_name IS NULL THEN 1 ELSE 0 END, group_name, name, id "
        "LIMIT 50 OFFSET 100",
        (),
    ),
    "target_list_all": ("SELECT * FROM targets ORDER BY group_name, name, id", ()),
    "alerts_by_target": (
        "SELECT * FROM alerts WHERE target_id = ? ORDER BY started_at DESC LIMIT 50 OFFSET 0",
        (17,),
    ),
    "alerts_by_status_range": (
        "SELECT * FROM alerts WHERE started_at >= ? AND started_at <= ? AND status = ? "
        "ORDER BY started_at DESC LIMIT 50 OFFSET 0",
        ((NOW - timedelta(days=30)).isoformat(), NOW.isoformat(), "open"),
    ),
    "alerts_unfiltered_page": ("SELECT * FROM alerts WHERE 1=1 ORDER BY started_at DESC LIMIT 50 OFFSET 0", ()),
    "alerts_count_status": ("SELECT COUNT(*) FROM alerts WHERE status = ?", ("open",)),
    "alerts_open_for_target": ("SELECT * FROM alerts WHERE target_id = ? AND status = 'open' LIMIT 1", (17,)),
    "alerts_target_report_range": (
        "SELECT * FROM alerts WHERE target_id = ? AND started_at >= ? AND (ended_at <= ? OR ended_at IS NULL) "
        "ORDER BY started_at ASC",
        (17, (NOW - timedelta(days=30)).isoformat(), NOW.isoformat()),
    ),
    "queue_due_candidates": (
        "SELECT id, channel_type, status FROM notification_queue "
        "WHERE (status = 'pending' AND next_retry_at <= ?) OR (status = 'processing' AND updated_at < ?) "
        "ORDER BY next_retry_at, id LIMIT 50",
        (NOW.isoformat(), (NOW - timedelta(minutes=10)).isoformat()),
    ),
    "queue_cleanup_select": (
        "SELECT id FROM notification_queue WHERE status IN ('sent','failed') AND updated_at < ? LIMIT 1000",
        ((NOW - timedelta(days=7)).isoformat(),),
    ),
    "queue_stats_group": ("SELECT status, COUNT(*) FROM notification_queue GROUP BY status", ()),
    "token_lookup": (
        "SELECT t.id FROM auth_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? "
        "AND t.expires_at > ? AND t.max_expires_at > ? AND u.is_active = 1",
        ("tok-5000", NOW.isoformat(), NOW.isoformat()),
    ),
    "token_expiry_delete": ("SELECT id FROM auth_tokens WHERE expires_at <= ? LIMIT 1000", (NOW.isoformat(),)),
    "login_attempt_by_ip": ("SELECT * FROM login_attempts WHERE ip_address = ?", ("10.0.1.5",)),
    "login_cleanup": (
        "SELECT id FROM login_attempts WHERE (locked_until IS NULL OR locked_until < ?) AND last_attempt_at < ?",
        (NOW.isoformat(), (NOW - timedelta(hours=24)).isoformat()),
    ),
    "quality_prune": (
        "SELECT ts FROM monitor_quality WHERE ts < ? LIMIT 1000",
        ((NOW - timedelta(days=30)).isoformat(),),
    ),
    "quality_recent": (
        "SELECT * FROM monitor_quality WHERE ts >= ? ORDER BY ts DESC LIMIT 500",
        ((NOW - timedelta(days=1)).isoformat(),),
    ),
    "report_jobs_expired": ("SELECT job_id FROM report_jobs WHERE expires_at < ?", (NOW.isoformat(),)),
    "report_jobs_active": ("SELECT * FROM report_jobs WHERE status IN ('pending','running')", ()),
    "locks_expired": ("SELECT target_id FROM target_locks WHERE expires_at < ?", (NOW.isoformat(),)),
    "pop_token_lookup": (
        "SELECT canonical_code FROM pop_locations WHERE alias_code IN (?,?,?) AND retired = 0 "
        "ORDER BY priority ASC, confidence DESC",
        ("fra", "ams", "lhr"),
    ),
    "pop_hostname_like": (
        "SELECT canonical_code FROM pop_locations WHERE retired = 0 AND (? LIKE '%.' || alias_code || '.%' "
        "OR (alias_code GLOB '*[0-9]*' AND ? LIKE '%.' || alias_code || '%')) "
        "ORDER BY length(alias_code) DESC, priority ASC, confidence DESC LIMIT 1",
        (".ae1.fra2.edge.example.net.", ".ae1.fra2.edge.example.net."),
    ),
    "pop_hostname_indexed": (
        "SELECT canonical_code FROM pop_locations WHERE retired = 0 AND alias_code IN (?,?,?,?,?,?) "
        "AND (? LIKE '%.' || alias_code || '.%' OR (alias_code GLOB '*[0-9]*' AND ? LIKE '%.' || alias_code || '%')) "
        "ORDER BY length(alias_code) DESC, priority ASC, confidence DESC LIMIT 1",
        ("fra", "fra2", "ae1", "edge", "example", "net", ".ae1.fra2.edge.example.net.", ".ae1.fra2.edge.example.net."),
    ),
    "pop_counts_by_source": ("SELECT source, COUNT(*) FROM pop_locations WHERE retired = 0 GROUP BY source", ()),
    "pop_candidates_top": (
        "SELECT * FROM pop_alias_candidates WHERE reviewed = 0 ORDER BY seen_count DESC LIMIT 50",
        (),
    ),
    "pop_runs_recent": ("SELECT * FROM pop_import_runs ORDER BY started_at DESC LIMIT 10", ()),
    "alert_users_for_target": ("SELECT user_id FROM target_alert_users WHERE target_id = ?", (17,)),
    "alert_targets_for_user": ("SELECT target_id FROM target_alert_users WHERE user_id = ?", (3,)),
    "recipients_for_target": ("SELECT recipient_id FROM target_recipients WHERE target_id = ?", (17,)),
    "targets_for_recipient": ("SELECT target_id FROM target_recipients WHERE recipient_id = ?", (3,)),
}


def _ts(offset_seconds: float) -> datetime:
    return NOW - timedelta(seconds=offset_seconds)


def populate(conn: sqlite3.Connection, scale: float) -> dict[str, int]:
    """Fill the schema with realistic synthetic volumes; returns the row counts."""
    rnd = random.Random(42)
    n_targets, n_alerts = int(2000 * scale), int(200_000 * scale)
    n_queue, n_tokens = int(100_000 * scale), int(50_000 * scale)
    n_login, n_quality = int(20_000 * scale), int(100_000 * scale)
    n_jobs, n_pop = int(5000 * scale), int(30_000 * scale)
    created = _ts(86400 * 90)
    with conn:
        for i in range(1, 51):
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)", (f"user{i}", "x", created)
            )
        for i in range(1, 51):
            conn.execute(
                "INSERT INTO recipients (name, email, created_at, updated_at) VALUES (?,?,?,?)",
                (f"r{i}", f"r{i}@example.net", created, created),
            )
        for i in range(n_targets):
            conn.execute(
                "INSERT INTO targets (name, group_name, interval_seconds, retention_days, enable_ping, "
                "enable_http_check, enable_cert_expiration, host, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"target-{i:05d}",
                    None if i % 10 == 0 else f"group-{i % 40}",
                    60,
                    30,
                    1,
                    1,
                    1,
                    f"h{i}.example.net",
                    created,
                    created,
                ),
            )
        for tid in range(1, n_targets + 1):
            for uid in rnd.sample(range(1, 51), 2):
                conn.execute("INSERT INTO target_alert_users VALUES (?,?)", (tid, uid))
            for rid in rnd.sample(range(1, 51), 2):
                conn.execute("INSERT INTO target_recipients VALUES (?,?)", (tid, rid))
        conn.executemany(
            "INSERT INTO alerts (id, target_id, target_name, failed_service, status, started_at, ended_at, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                (
                    str(uuid.UUID(int=rnd.getrandbits(128), version=4)),
                    rnd.randint(1, n_targets),
                    "t",
                    "http",
                    "open" if i % 500 == 0 else "resolved",
                    (s := _ts(rnd.uniform(0, 86400 * 365))),
                    None if i % 500 == 0 else s + timedelta(minutes=5),
                    s,
                    s,
                )
                for i in range(n_alerts)
            ),
        )
        conn.executemany(
            "INSERT INTO notification_queue (notification_type, channel_type, recipient_name, recipient_address, "
            "payload, status, next_retry_at, created_at, updated_at, target_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    "alert",
                    "email" if i % 2 else "signal",
                    "r",
                    "a@b",
                    "{}",
                    ("pending" if i % 5000 == 0 else "failed" if i % 50 == 0 else "sent"),
                    (s := _ts(rnd.uniform(0, 86400 * 60))).isoformat(),
                    s.isoformat(),
                    s.isoformat(),
                    rnd.randint(1, n_targets),
                )
                for i in range(n_queue)
            ),
        )
        conn.executemany(
            "INSERT INTO auth_tokens (user_id, token_hash, expires_at, max_expires_at, created_at) VALUES (?,?,?,?,?)",
            (
                (rnd.randint(1, 50), f"tok-{i}", _ts(-rnd.uniform(-86400 * 2, 86400)), _ts(-86400), created)
                for i in range(n_tokens)
            ),
        )
        conn.executemany(
            "INSERT INTO login_attempts (ip_address, failed_count, last_attempt_at, created_at) VALUES (?,?,?,?)",
            (
                (f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}", 1, _ts(rnd.uniform(0, 86400 * 5)), created)
                for i in range(n_login)
            ),
        )
        conn.executemany(
            "INSERT INTO monitor_quality (ts, score, success_ratio, state) VALUES (?,?,?,?)",
            ((_ts(i * 30), 99.0, 1.0, "ok") for i in range(n_quality)),
        )
        conn.executemany(
            "INSERT INTO report_jobs (job_id, target_id, status, created_at, expires_at) VALUES (?,?,?,?,?)",
            (
                (
                    str(uuid.uuid4()),
                    rnd.randint(1, n_targets),
                    "completed",
                    _ts(86400 * 5),
                    _ts(rnd.uniform(-86400, 86400 * 30)),
                )
                for _ in range(n_jobs)
            ),
        )
        alphabet = "abcdefghijklmnopqrstuvwxyz"
        for i in range(n_pop):
            code = "".join(rnd.choice(alphabet) for _ in range(3)) + (str(rnd.randint(1, 9)) if i % 3 == 0 else "")
            conn.execute(
                "INSERT OR IGNORE INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, "
                "confidence, priority, created_at, updated_at, import_run_id, retired) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    code[:3],
                    code,
                    "c",
                    "DE",
                    50.0,
                    8.0,
                    rnd.choice(["iata", "peeringdb", "seed"]),
                    80,
                    10,
                    "x",
                    "x",
                    f"run{i % 50}",
                    0,
                ),
            )
        for code in ("fra", "ams", "lhr"):
            conn.execute(
                "INSERT OR IGNORE INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, "
                "confidence, priority, created_at, updated_at, retired) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                (code, code, "c", "DE", 50.0, 8.0, "seed", 90, 1, "x", "x"),
            )
        conn.executemany(
            "INSERT INTO pop_alias_candidates (alias_code, seen_count, last_seen, created_at) VALUES (?,?,?,?)",
            ((f"cand{i}", rnd.randint(1, 50), "x", "x") for i in range(int(5000 * scale))),
        )
        conn.executemany(
            "INSERT INTO pop_import_runs (run_id, source, started_at, status) VALUES (?,?,?,?)",
            ((f"run{i}", "iata", _ts(i * 3600).isoformat(), "success") for i in range(int(500 * scale))),
        )
    conn.execute("ANALYZE")
    return {
        row[0]: conn.execute(f"SELECT COUNT(*) FROM {row[0]}").fetchone()[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }


def index_inventory(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """(table, index, origin, columns); origin c=explicit, u=UNIQUE, pk=PRIMARY KEY (automatic)."""
    out = []
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    ]
    for table in sorted(tables):
        for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
            cols = ",".join(str(c[2]) for c in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall())
            out.append((table, idx[1], idx[3], cols))
    return out


def run_plans(args: argparse.Namespace) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="justup-bench-", dir=args.tmpdir))
    try:
        db = tmp / "bench.db"
        conn = sdb.connect(db)
        sdb.init_schema(conn)
        t0 = time.perf_counter()
        counts = populate(conn, args.scale)
        print(f"# populate: {time.perf_counter() - t0:.1f}s, sqlite {sqlite3.sqlite_version}, scale {args.scale}")
        print("# row counts:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v))
        print("\n## Indexes (table | index | origin | columns)")
        for row in index_inventory(conn):
            print(" | ".join(row))
        print("\n## Query plans and timings (best of 5)")
        for name, (sql, params) in QUERIES.items():
            plan = " ; ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
            best = min(_time(conn, sql, params) for _ in range(5))
            print(f"{name:28s} {best * 1000:9.3f} ms  {plan}")
        print(f"\n## Sizes: db={db.stat().st_size / 1e6:.1f} MB")
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _time(conn: sqlite3.Connection, sql: str, params: tuple) -> float:
    t0 = time.perf_counter()
    conn.execute(sql, params).fetchall()
    return time.perf_counter() - t0


def _pct(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))] * 1000 if ordered else 0.0


def _proc_io() -> int:
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            if line.startswith("write_bytes"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def run_sync_mode(mode: str, tmpdir: str | None, writes: int, seconds: float) -> dict[str, float]:
    tmp = Path(tempfile.mkdtemp(prefix=f"justup-sync-{mode}-", dir=tmpdir))
    try:
        sdb.SYNCHRONOUS_MODE = mode
        db = tmp / "bench.db"
        conn = sdb.connect(db)
        sdb.init_schema(conn)
        populate(conn, 0.05)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

        result: dict[str, float] = {}
        # (1) single-writer throughput: one committed row per transaction, like queue/alert writes
        c = sdb.connect(db)
        lat = []
        t0 = time.perf_counter()
        for i in range(writes):
            s = time.perf_counter()
            with sdb.transaction(c):
                c.execute(
                    "INSERT INTO notification_queue (notification_type, channel_type, recipient_name, recipient_address, "
                    "payload, status, next_retry_at, created_at, updated_at) VALUES ('a','email','r','a@b','{}','pending',?,?,?)",
                    (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
                )
            lat.append(time.perf_counter() - s)
        elapsed = time.perf_counter() - t0
        result["tx_per_s"] = writes / elapsed
        result["tx_p50_ms"], result["tx_p95_ms"], result["tx_p99_ms"] = (_pct(lat, p) for p in (0.5, 0.95, 0.99))
        c.close()

        # (2) monitoring load: 4 writers (status updates) + 3 readers (list queries) for a fixed duration
        stop = threading.Event()
        wlat: list[float] = []
        rlat: list[float] = []
        lock = threading.Lock()
        wal = db.with_name(db.name + "-wal")
        wal_peak = [0]

        def writer(seed: int) -> None:
            wc = sdb.connect(db)
            rnd = random.Random(seed)
            while not stop.is_set():
                s = time.perf_counter()
                try:
                    with sdb.transaction(wc, immediate=True):
                        wc.execute(
                            "INSERT OR REPLACE INTO target_status (target_id, overall_status, updated_at) VALUES (?, 'up', ?)",
                            (rnd.randint(1, 100), NOW),
                        )
                        wc.execute(
                            "INSERT INTO monitor_quality (ts, score, success_ratio, state) VALUES (?,?,?,?)",
                            (NOW + timedelta(microseconds=rnd.getrandbits(40)), 99.0, 1.0, "ok"),
                        )
                except sqlite3.Error:
                    continue
                with lock:
                    wlat.append(time.perf_counter() - s)
            wc.close()

        def reader() -> None:
            rc = sdb.connect(db)
            while not stop.is_set():
                s = time.perf_counter()
                rc.execute("SELECT * FROM targets ORDER BY group_name, name, id LIMIT 50").fetchall()
                rc.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()
                with lock:
                    rlat.append(time.perf_counter() - s)
            rc.close()

        cpu0, io0 = resource.getrusage(resource.RUSAGE_SELF), _proc_io()
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + [
            threading.Thread(target=reader) for _ in range(3)
        ]
        for t in threads:
            t.start()
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if wal.exists():
                wal_peak[0] = max(wal_peak[0], wal.stat().st_size)
            time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join()
        cpu1, io1 = resource.getrusage(resource.RUSAGE_SELF), _proc_io()
        result["load_commits_per_s"] = len(wlat) / seconds
        result["load_w_p50_ms"], result["load_w_p95_ms"], result["load_w_p99_ms"] = (
            _pct(wlat, p) for p in (0.5, 0.95, 0.99)
        )
        result["load_r_p50_ms"], result["load_r_p99_ms"] = _pct(rlat, 0.5), _pct(rlat, 0.99)
        result["wal_peak_mb"] = wal_peak[0] / 1e6
        result["cpu_s"] = (cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime)
        result["maxrss_mb"] = cpu1.ru_maxrss / 1024
        result["write_mb"] = (io1 - io0) / 1e6
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_sync(args: argparse.Namespace) -> None:
    runs = args.runs
    print(
        f"# sync benchmark: tmpdir={args.tmpdir or tempfile.gettempdir()} writes={args.writes} load={args.seconds}s runs={runs}"
    )
    results: dict[str, list[dict[str, float]]] = {"FULL": [], "NORMAL": []}
    for r in range(runs):
        for mode in ("FULL", "NORMAL") if r % 2 == 0 else ("NORMAL", "FULL"):
            results[mode].append(run_sync_mode(mode, args.tmpdir, args.writes, args.seconds))
    keys = list(results["FULL"][0])
    print(f"{'metric':22s} {'FULL':>12s} {'NORMAL':>12s}   (median of {runs} runs)")
    for k in keys:
        full = statistics.median(r[k] for r in results["FULL"])
        normal = statistics.median(r[k] for r in results["NORMAL"])
        print(f"{k:22s} {full:12.2f} {normal:12.2f}")
    print(f"# os.cpu_count={os.cpu_count()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plans")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--tmpdir")
    p.set_defaults(fn=run_plans)
    s = sub.add_parser("sync")
    s.add_argument("--tmpdir")
    s.add_argument("--writes", type=int, default=1500)
    s.add_argument("--seconds", type=float, default=8.0)
    s.add_argument("--runs", type=int, default=3)
    s.set_defaults(fn=run_sync)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
