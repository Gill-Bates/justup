#!/usr/bin/env python3
#
# tests/test_db_review_fixes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Regression tests for the external DB/middleware/models review (DB-REVIEW.md).
#
# ruff: noqa: SLF001
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.db import notifications as inbox
from app.db import sqlite as sqlite_db
from app.db import tsdb
from app.models.groups import GroupUpdate
from app.models.targets import normalize_ports
from app.utils.tcp_ports import parse_port, parse_port_list
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    sqlite_db.init_schema(c)
    return c


# ─── G1: group rename keeps the target assignment ────────────────────────────


def test_group_rename_carries_targets_along(mem):
    gid = sqlite_db.create_group(mem, name="old")
    tid = sqlite_db.create_target(
        mem, {"name": "t", "host": "example.org", "interval_seconds": 60, "retention_days": 30, "group": "old"}
    )
    sqlite_db.update_group(mem, group_id=gid, patch={"name": "new"})
    assert sqlite_db.get_target(mem, tid)["group"] == "new"
    assert sqlite_db.count_targets_in_group(mem, "new") == 1
    assert sqlite_db.count_targets_in_group(mem, "old") == 0


def test_group_rename_collision_rolls_back_target_move(mem):
    a = sqlite_db.create_group(mem, name="a")
    sqlite_db.create_group(mem, name="b")
    tid = sqlite_db.create_target(
        mem, {"name": "t", "host": "example.org", "interval_seconds": 60, "retention_days": 30, "group": "a"}
    )
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_db.update_group(mem, group_id=a, patch={"name": "b"})
    assert sqlite_db.get_target(mem, tid)["group"] == "a"


def test_group_description_only_update_leaves_targets(mem):
    gid = sqlite_db.create_group(mem, name="g")
    tid = sqlite_db.create_target(
        mem, {"name": "t", "host": "example.org", "interval_seconds": 60, "retention_days": 30, "group": "g"}
    )
    sqlite_db.update_group(mem, group_id=gid, patch={"description": "d"})
    assert sqlite_db.get_target(mem, tid)["group"] == "g"


# ─── P3: explicit null group name / bool and fractional TCP ports ─────────────


def test_group_update_rejects_explicit_null_name_but_allows_omission():
    with pytest.raises(ValidationError):
        GroupUpdate(name=None)
    assert GroupUpdate(description="x").model_dump(exclude_unset=True) == {"description": "x"}


@pytest.mark.parametrize("bad", [[True], [1.9], [80, False], [80.5]])
def test_tcp_port_list_rejects_bool_and_fractional(bad):
    with pytest.raises(ValueError):
        normalize_ports(bad)


def test_tcp_port_list_accepts_ints_numeric_strings_and_whole_floats():
    assert normalize_ports([80, "443", 8080.0]) == "80,443,8080"


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("443.5"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Fraction(887, 2),
        float("nan"),
        float("inf"),
        443.5,
        True,
        b"80",
        "80.5",
        "٨٠",
        0,
        65536,
        Decimal(65536),
        -1,
    ],
)
def test_tcp_port_parser_rejects_fractions_and_unsupported_types_before_conversion(bad):
    with pytest.raises(ValueError, match="Invalid TCP port"):
        parse_port_list([bad])


@pytest.mark.parametrize("good", [443, "443", " 443 ", 443.0, Decimal(443), Decimal("4.43E+2"), Fraction(443, 1)])
def test_tcp_port_parser_accepts_whole_numbers_of_every_supported_type(good):
    assert parse_port_list([good]) == [443]
    assert parse_port(good) == 443


# ─── G2: compaction merge keeps distinct points sharing a timestamp ───────────

NOW = datetime(2026, 6, 10, 15, 30, tzinfo=UTC)


def _unlink_fails(*_a, **_k):
    raise OSError("busy")


def _line(ts: datetime, value: float) -> str:
    return json.dumps({"ts": ts.isoformat(), "value": value}) + "\n"


def test_merge_keeps_distinct_values_at_same_timestamp_and_drops_identical_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(tsdb, "utcnow", lambda: NOW)
    day = NOW.replace(hour=0, minute=0) - timedelta(days=3)
    t0, t1 = day + timedelta(hours=1), day + timedelta(hours=2)
    path = tsdb._series_path(tmp_path, 1, "ping_ms")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_line(t0, 1.0) + _line(t1, 2.0), encoding="utf-8")
    tsdb.housekeep_target(tmp_path, 1, 365, throttle=False)  # day is closed into a segment
    # Late lines for the closed day: same ts with another value, plus an exact repeat.
    with path.open("a", encoding="utf-8") as f:
        f.write(_line(t1, 99.0) + _line(t0, 1.0))
    tsdb.housekeep_target(tmp_path, 1, 365, throttle=False)  # merge branch
    pts = tsdb.query(tmp_path, target_id=1, metric="ping_ms", limit=1000)
    assert sorted((p.ts, p.value) for p in pts) == [(t0, 1.0), (t1, 2.0), (t1, 99.0)]


# ─── G5: removal reports leftovers ───────────────────────────────────────────


def test_delete_target_and_purge_report_success(tmp_path):
    (tmp_path / "target_7").mkdir()
    (tmp_path / "target_7" / "x.jsonl").write_text("{}\n")
    assert tsdb.delete_target(tmp_path, 7) is True
    assert tsdb.delete_target(tmp_path, 7) is True  # repeatable
    assert tsdb.purge_tsdb(tmp_path) is True and tmp_path.is_dir()


def test_delete_target_reports_remaining_data(tmp_path, monkeypatch, caplog):
    tdir = tmp_path / "target_8"
    tdir.mkdir()
    (tdir / "x.jsonl").write_text("{}\n")

    def boom(*_a, **_k):
        raise OSError("busy")

    monkeypatch.setattr(tsdb.shutil, "rmtree", boom)
    monkeypatch.setattr(Path, "unlink", _unlink_fails)
    assert tsdb.delete_target(tmp_path, 8) is False
    assert tdir.exists()
    assert "incomplete" in caplog.text
    monkeypatch.undo()
    assert tsdb.delete_target(tmp_path, 8) is True  # retry succeeds


def test_purge_reports_remaining_data(tmp_path, monkeypatch):
    (tmp_path / "target_1").mkdir()
    (tmp_path / "target_1" / "x").write_text("1")

    def boom(*_a, **_k):
        raise OSError("busy")

    monkeypatch.setattr(tsdb.shutil, "rmtree", boom)
    monkeypatch.setattr(Path, "unlink", _unlink_fails)
    assert tsdb.purge_tsdb(tmp_path) is False


# ─── G3: concurrent grouped deliveries are all counted ───────────────────────


def _count_of(conn, key):
    return conn.execute("SELECT event_count FROM notifications WHERE idempotency_key = ?", (key,)).fetchone()[0]


def test_lost_insert_race_still_counts_the_delivery(tmp_path, monkeypatch):
    path = tmp_path / "n.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    key = f"delivery.email.sent:{int(now.timestamp()) // inbox.SENT_GROUP_SECONDS}"
    real = inbox._bump_grouped_delivery
    calls = {"n": 0}

    def racing(c, k, label):
        calls["n"] += 1
        if calls["n"] == 1:
            # The other delivery inserts the group between our check and our insert.
            inbox.emit(
                c,
                event_type="delivery.email.sent",
                category="delivery",
                severity="success",
                title="Email notifications sent",
                idempotency_key=k,
                audience=inbox.AUDIENCE_ADMINS,
                created_at=now,
            )
            return False
        return real(c, k, label)

    monkeypatch.setattr(inbox, "_bump_grouped_delivery", racing)
    inbox.emit_delivery_sent(conn, "email", now=now)
    assert _count_of(conn, key) == 2
    sqlite_db.close_connection(conn)


def test_parallel_deliveries_are_all_counted(tmp_path):
    path = tmp_path / "n.db"
    boot = sqlite_db.connect(path)
    sqlite_db.init_schema(boot)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    key = f"delivery.signal.sent:{int(now.timestamp()) // inbox.SENT_GROUP_SECONDS}"
    barrier = threading.Barrier(4, timeout=10)

    def work():
        c = sqlite_db.connect(path)
        barrier.wait()
        inbox.emit_delivery_sent(c, "signal", now=now)
        sqlite_db.close_connection(c)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)
    assert _count_of(boot, key) == 4
    sqlite_db.close_connection(boot)


# ─── DRY contract tests (no behaviour change) ────────────────────────────────


def test_notification_categories_match_schema_check(mem):
    sql = mem.execute("SELECT sql FROM sqlite_schema WHERE name = 'notifications'").fetchone()[0]
    in_check = sql.split("category IN (", 1)[1].split(")", 1)[0]
    assert tuple(s.strip().strip("'") for s in in_check.split(",")) == inbox.CATEGORIES


def test_rollup_suffixes_match_rollup_config():
    from app.db import tsdb_segments as seg

    assert set(seg._ROLLUP_SUFFIXES) == set(tsdb.ROLLUP_CONFIG) - {"raw"}


def test_dashboard_row_bound_matches_schema_check(mem):
    from app.db import dashboard_layouts

    sql = mem.execute("SELECT sql FROM sqlite_schema WHERE name = 'dashboard_layouts'").fetchone()
    assert f"y + h <= {dashboard_layouts.DASHBOARD_MAX_Y}" in sql[0]


@pytest.mark.parametrize(
    ("raw", "stored", "ports"),
    [
        ("80,443", "80,443", [80, 443]),
        ("8080 443 443 80", "80,443,8080", [80, 443, 8080]),
        ([8080, "80"], "80,8080", [80, 8080]),
        (22, "22", [22]),
    ],
)
def test_tcp_port_rule_is_shared_by_model_and_scheduler(raw, stored, ports):
    from app.monitor.scheduler import _stored_tcp_ports

    assert normalize_ports(raw) == stored
    assert _stored_tcp_ports(stored) == ports == _stored_tcp_ports(raw)


@pytest.mark.parametrize("bad", ["80,abc", "0", "70000", "80.5", "\u00b2", "80;443", [80, True]])
def test_invalid_port_list_is_rejected_by_model_and_never_defaults_to_port_80(bad):
    from app.monitor.scheduler import _stored_tcp_ports

    with pytest.raises(ValueError):
        normalize_ports(bad)
    assert _stored_tcp_ports(bad) == []


def test_empty_port_value_means_default_in_model_and_scheduler():
    from app.monitor.scheduler import _stored_tcp_ports

    assert normalize_ports("", default="80") == "80"
    assert _stored_tcp_ports("") == _stored_tcp_ports(None) == [80]
