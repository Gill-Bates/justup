#!/usr/bin/env python3
#
# tests/test_tsdb_segments.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG005
# ARG005: the value callbacks of _write_legacy have a fixed (index, timestamp) signature.
"""Compressed TSDB segments: transparent reads, crash safety, migration, retention and concurrency."""

from __future__ import annotations

import fcntl
import gzip
import itertools
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import tsdb
from app.db import tsdb_segments as seg
from app.utils import maintenance
from app.utils.storage_stats import compute_storage_stats
from tests import _support  # noqa: F401

NOW = datetime(2026, 6, 10, 15, 30, tzinfo=UTC)  # a Wednesday; today starts at 2026-06-10T00:00Z
TODAY = NOW.replace(hour=0, minute=0)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(tsdb, "utcnow", lambda: NOW)


def _write_legacy(
    root: Path, metric: str, start: datetime, end: datetime, step: timedelta, *, tid: int = 1, value=None
):
    """Chronological legacy JSONL file as an older release wrote it; returns the number of lines."""
    path = tsdb._series_path(root, tid, metric)
    path.parent.mkdir(parents=True, exist_ok=True)
    n, t = 0, start
    with path.open("w", encoding="utf-8") as f:
        while t < end:
            v = float(n) if value is None else value(n, t)
            f.write(json.dumps({"ts": t.isoformat(), "value": v}) + "\n")
            t += step
            n += 1
    return n


def _all(root: Path, metric: str, tid: int = 1, **kw):
    pts = tsdb.query(root, target_id=tid, metric=metric, limit=10**7, **kw)
    return [(p.ts.isoformat(), p.value) for p in pts]


def _segments(root: Path, metric: str = "ping_ms", tid: int = 1):
    p = tsdb._series_path(root, tid, metric)
    return seg.list_segments(p.parent, p.stem)


def _housekeep(root: Path, retention: int = 365, **kw):
    kw.setdefault("throttle", False)
    return tsdb.housekeep_target(root, 1, retention, **kw)


@pytest.fixture
def legacy(tmp_path):
    """Three weeks of 10-minute raw data in the old single-file layout."""
    _write_legacy(tmp_path, "ping_ms", NOW - timedelta(days=21), NOW, timedelta(minutes=10))
    return tmp_path


# --- 1, 2: appends stay plain, closed days are compressed ----------------------------------------------------


def test_new_points_append_to_the_plain_active_file_and_old_days_are_compressed(legacy):
    before = _all(legacy, "ping_ms")
    result = _housekeep(legacy)
    assert result.complete and result.compaction.lines > 0
    keys = [s.key for s in _segments(legacy)]
    assert keys == sorted(keys) and all("W" in k for k in keys)
    active = tsdb._series_path(legacy, 1, "ping_ms")
    first = json.loads(active.read_text(encoding="utf-8").splitlines()[0])
    assert datetime.fromisoformat(first["ts"]) >= TODAY  # only the current day stays uncompressed
    tsdb.append_point(legacy, target_id=1, metric="ping_ms", value=-1.0, retention_days=365, at=NOW)
    assert _all(legacy, "ping_ms") == [*before, (NOW.isoformat(), -1.0)]
    assert sum(s.path.stat().st_size for s in _segments(legacy)) < 0.5 * sum(len(f"{t}{v}") + 30 for t, v in before)


def test_reads_are_identical_before_and_after_compression_for_any_window(legacy):
    full = _all(legacy, "ping_ms")
    windows = [
        {},
        {"since": NOW - timedelta(days=9), "until": NOW - timedelta(days=3)},
        {"since": TODAY - timedelta(hours=1)},
        {"until": NOW - timedelta(days=15)},
        {"since": NOW - timedelta(days=2), "limit": 5},
        {"limit": 7, "latest": True},
        {"since": NOW - timedelta(days=12), "limit": 9, "latest": True},
    ]
    expected = []
    for window in windows:
        kw = {"limit": 10**7, **window}
        expected.append([(p.ts.isoformat(), p.value) for p in tsdb.query(legacy, target_id=1, metric="ping_ms", **kw)])
    _housekeep(legacy)
    assert _all(legacy, "ping_ms") == full
    for window, exp in zip(windows, expected, strict=True):
        kw = {"limit": 10**7, **window}
        got = [(p.ts.isoformat(), p.value) for p in tsdb.query(legacy, target_id=1, metric="ping_ms", **kw)]
        assert got == exp, window


def test_a_narrow_query_opens_only_the_overlapping_segments(legacy, monkeypatch):
    _housekeep(legacy)
    opened: list[str] = []
    real = seg.open_segment
    monkeypatch.setattr(seg, "open_segment", lambda path: opened.append(path.name) or real(path))
    tsdb.query(legacy, target_id=1, metric="ping_ms", since=NOW - timedelta(hours=6), limit=1000)
    assert opened == []  # the last 6 hours live in the active file
    tsdb.query(legacy, target_id=1, metric="ping_ms", since=NOW - timedelta(days=18), until=NOW - timedelta(days=17))
    assert len(opened) == 1


def test_series_that_only_exist_as_segments_are_still_listed_and_queryable(legacy):
    _housekeep(legacy)
    active = tsdb._series_path(legacy, 1, "ping_ms")
    active.unlink()
    assert tsdb.list_metrics(legacy, 1) == ["ping_ms"]
    assert tsdb.query(legacy, target_id=1, metric="ping_ms", limit=3, latest=True)


# --- 3: concurrent readers and writers -----------------------------------------------------------------------


def test_concurrent_append_read_and_compaction_lose_and_duplicate_nothing(legacy):
    original = _all(legacy, "ping_ms")
    errors: list[BaseException] = []
    stop = threading.Event()
    appended = [NOW + timedelta(seconds=i) for i in range(1, 301)]

    def writer():
        try:
            for ts in appended:
                tsdb.append_point(legacy, target_id=1, metric="ping_ms", value=-1.0, retention_days=365, at=ts)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def compactor():
        try:
            while not stop.is_set():
                _housekeep(legacy)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            while not stop.is_set():
                got = _all(legacy, "ping_ms")
                stamps = [t for t, _ in got]
                assert stamps == sorted(stamps)
                assert len(stamps) == len(set(stamps)), "a point was visible twice"
                assert len(stamps) >= len(original), "a point was missing"
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=f) for f in (writer, compactor, reader)]
    for t in threads:
        t.start()
    threads[0].join(30)
    stop.set()
    for t in threads[1:]:
        t.join(30)
    assert not errors, errors
    final = _all(legacy, "ping_ms")
    assert len(final) == len(original) + len(appended)
    assert [t for t, _ in final] == sorted(t for t, _ in final)


# --- 4, 5: crash and restart ---------------------------------------------------------------------------------


def _tmp_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.tmp"))


def test_failure_before_publish_keeps_the_original_file_and_leaves_no_temp_files(legacy, monkeypatch):
    active = tsdb._series_path(legacy, 1, "ping_ms")
    raw_before = active.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(tsdb, "_plan_segment", boom)
    result = _housekeep(legacy)
    assert not result.complete
    assert active.read_bytes() == raw_before
    assert _segments(legacy) == [] and _tmp_files(legacy) == []


def test_kill_after_publishing_segments_is_repaired_by_the_next_run_without_duplicates(legacy, monkeypatch):
    expected = _all(legacy, "ping_ms")

    real_fsync_dir = seg.fsync_dir

    def crash_on_first(_directory):
        # The first call comes right after the segments were renamed in and before the active file is replaced.
        monkeypatch.setattr(seg, "fsync_dir", real_fsync_dir)
        raise OSError("killed between segment publish and active-file rewrite")

    with monkeypatch.context() as m:
        m.setattr(seg, "fsync_dir", crash_on_first)
        assert not _housekeep(legacy).complete
    assert _segments(legacy)  # published ...
    assert len(_all(legacy, "ping_ms")) > len(expected)  # ... and the days are visible twice until repaired
    assert _housekeep(legacy).complete
    assert _all(legacy, "ping_ms") == expected
    assert _tmp_files(legacy) == []


def test_orphaned_temp_files_are_removed_only_when_stale(legacy):
    tdir = legacy / "target_1"
    old, fresh = tdir / ".x.old.tmp", tdir / ".x.fresh.tmp"
    for f in (old, fresh):
        f.write_bytes(b"partial")
    stale = time.time() - 2 * tsdb._STALE_TEMP_SECONDS
    os.utime(old, (stale, stale))
    _housekeep(legacy)
    assert not old.exists() and fresh.exists()


# --- 6: migration of existing installs ------------------------------------------------------------------------


def test_migration_is_incremental_resumable_idempotent_and_lossless(tmp_path, monkeypatch):
    names = ["ping_ms", "ping_up", "http_ms", "http_up", "tcp_up_443"]
    for m in names:
        _write_legacy(tmp_path, m, NOW - timedelta(days=30), NOW, timedelta(minutes=10))
    expected = {m: _all(tmp_path, m) for m in names}
    done = {"n": 0}
    real = tsdb._compact_series

    def counting(active, hot):
        done["n"] += 1
        return real(active, hot)

    monkeypatch.setattr(tsdb, "_compact_series", counting)
    runs = 0
    while True:
        runs += 1
        done["n"] = 0
        # a stop request after one series stands in for a small time budget
        if tsdb.compact_target(tmp_path, 1, should_stop=lambda: done["n"] >= 1, throttle=False).complete:
            break
        assert {m: _all(tmp_path, m) for m in names} == expected  # readable at every intermediate step
        assert runs < 20
    assert runs == len(names)  # exactly one series per run
    assert {m: _all(tmp_path, m) for m in names} == expected
    again = tsdb.compact_target(tmp_path, 1, throttle=False)
    assert again.complete and again.lines == 0 and again.series == 0


def test_a_finished_series_costs_one_small_read(legacy, monkeypatch):
    _housekeep(legacy)
    calls: list[str] = []
    monkeypatch.setattr(tsdb, "_compact_series", lambda active, hot: calls.append(active.name) or 0)
    assert tsdb.compact_target(legacy, 1, throttle=False).complete
    assert calls == []  # skipped by the head check, nothing scanned


def test_the_time_budget_stops_between_series_and_reports_pending_work(tmp_path):
    for m in ("a_ms", "b_ms", "c_ms"):
        _write_legacy(tmp_path, m, NOW - timedelta(days=10), NOW, timedelta(minutes=30))
    res = tsdb.compact_target(tmp_path, 1, budget_seconds=0.0, throttle=False)
    assert not res.complete and res.series == 0


# --- 7, 9: rollup boundaries, numeric aggregation ------------------------------------------------------------


def test_daily_rollups_have_no_gap_and_no_duplicate_at_the_handoff(tmp_path, monkeypatch):
    # 60 days of 1-minute raw data reaching past "now"; each daily run hands one more day over.
    _write_legacy(tmp_path, "ping_ms", NOW - timedelta(days=60), NOW + timedelta(days=5), timedelta(minutes=1))
    for day in range(3):
        clock = NOW + timedelta(days=day)
        monkeypatch.setattr(tsdb, "utcnow", lambda c=clock: c)
        tsdb.housekeep_target(tmp_path, 1, 365, throttle=False)
    stamps = [datetime.fromisoformat(t) for t, _ in _all(tmp_path, "ping_ms@5m_median")]
    assert stamps == sorted(set(stamps)), "duplicate or unordered rollup bucket"
    assert all((b - a) == timedelta(minutes=5) for a, b in itertools.pairwise(stamps)), "gap between buckets"
    assert all(s.minute % 5 == 0 and s.second == 0 for s in stamps)


def test_only_complete_buckets_are_rolled_up_and_values_are_the_median(tmp_path):
    cut = NOW - timedelta(days=29)  # handoff for a 365 day target
    bucket = cut.replace(minute=cut.minute - cut.minute % 5, second=0, microsecond=0)
    path = tsdb._series_path(tmp_path, 1, "ping_ms")
    path.parent.mkdir(parents=True)
    rows = [(bucket - timedelta(minutes=5) + timedelta(seconds=30 * i), v) for i, v in enumerate((10, 20, 90))]
    rows += [(bucket + timedelta(seconds=30), 1000)]  # straddles the handoff: its bucket is not complete yet
    path.write_text("".join(json.dumps({"ts": t.isoformat(), "value": v}) + "\n" for t, v in rows), encoding="utf-8")
    created = tsdb.run_rollups(tmp_path, 1, 365)
    assert created == {"ping_ms@5m_median": 1}
    assert _all(tmp_path, "ping_ms@5m_median") == [((bucket - timedelta(minutes=5)).isoformat(), 20.0)]
    assert tsdb.run_rollups(tmp_path, 1, 365) == {}  # idempotent


# --- 8, 10: availability minimum, categorical http_status -----------------------------------------------------


def test_binary_availability_keeps_an_outage_in_every_rollup_and_in_segments(tmp_path):
    start = (NOW - timedelta(days=40)).replace(minute=0, second=0, microsecond=0)
    _write_legacy(
        tmp_path,
        "tcp_up_443",
        start,
        start + timedelta(hours=2),
        timedelta(minutes=1),
        value=lambda i, t: 0 if i == 61 else 1,
    )
    tsdb.run_rollups(tmp_path, 1, 3650)
    rolled = dict(_all(tmp_path, "tcp_up_443@5m_median"))
    assert rolled[(start + timedelta(minutes=60)).isoformat()] == 0.0  # the minute-61 outage survives
    assert sum(1 for v in rolled.values() if v == 0.0) == 1
    _housekeep(tmp_path)
    assert dict(_all(tmp_path, "tcp_up_443@5m_median")) == rolled


def test_http_status_is_never_rolled_up_and_its_distribution_survives_compression(tmp_path):
    start = NOW - timedelta(days=29, hours=12)
    codes = [200, 200, 503, 404, 200]
    _write_legacy(tmp_path, "http_status", start, NOW, timedelta(minutes=10), value=lambda i, t: codes[i % 5])
    since, until = NOW - timedelta(days=40), NOW

    def dist():
        out = tsdb.query_display_series(
            tmp_path, target_id=1, metric="http_status", since=since, until=until, bucket_seconds=3600
        )
        return out["status_counts"], out["raw_status_only"]

    before = dist()
    assert tsdb.run_rollups(tmp_path, 1, 365) == {}
    assert not [m for m in tsdb.list_metrics(tmp_path, 1) if "@" in m]
    _housekeep(tmp_path)
    assert dist() == before and before[1] is True
    assert before[0]["503"] > 0 and before[0]["404"] > 0


def test_a_legacy_http_status_rollup_is_still_read_and_compressed(tmp_path):
    _write_legacy(
        tmp_path,
        "http_status@5m_median",
        NOW - timedelta(days=100),
        NOW - timedelta(days=60),
        timedelta(minutes=5),
        value=lambda i, t: 200.0,
    )
    expected = _all(tmp_path, "http_status@5m_median")
    _housekeep(tmp_path)
    assert _all(tmp_path, "http_status@5m_median") == expected
    assert _segments(tmp_path, "http_status@5m_median")


# --- 11, 12: retention and target lifecycle -------------------------------------------------------------------


@pytest.mark.parametrize("retention", [7, 45, 365])
def test_segments_follow_the_target_retention(tmp_path, retention):
    _write_legacy(tmp_path, "ping_ms", NOW - timedelta(days=30), NOW, timedelta(minutes=30))
    _write_legacy(
        tmp_path, "ping_ms@5m_median", NOW - timedelta(days=170), NOW - timedelta(days=30), timedelta(hours=1)
    )
    raw_cut = NOW - timedelta(days=min(30, retention))
    five_cut = NOW - timedelta(days=min(180, retention))
    raw_expected = [(t, v) for t, v in _all(tmp_path, "ping_ms") if datetime.fromisoformat(t) >= raw_cut]
    five_expected = [(t, v) for t, v in _all(tmp_path, "ping_ms@5m_median") if datetime.fromisoformat(t) >= five_cut]
    tsdb.housekeep_target(tmp_path, 1, retention, throttle=False)
    assert _all(tmp_path, "ping_ms") == raw_expected  # nothing dropped early, nothing kept too long
    five = _all(tmp_path, "ping_ms@5m_median")
    assert five[: len(five_expected)] == five_expected  # rollups written by this run only come after
    assert all(datetime.fromisoformat(t) >= five_cut for t, _ in five)


def test_pruning_deletes_whole_expired_segments_and_trims_the_boundary_one(legacy):
    _housekeep(legacy)
    keys_before = [s.key for s in _segments(legacy)]
    tsdb.prune_series(legacy, target_id=1, metric="ping_ms", retention_days=10)
    kept = _all(legacy, "ping_ms")
    assert kept and datetime.fromisoformat(kept[0][0]) >= NOW - timedelta(days=10)
    keys_after = [s.key for s in _segments(legacy)]
    assert keys_after == keys_before[-len(keys_after) :] and len(keys_after) < len(keys_before)


def test_deleted_and_disabled_targets(tmp_path):
    _write_legacy(tmp_path, "ping_ms", NOW - timedelta(days=10), NOW, timedelta(minutes=30), tid=4)
    assert tsdb.housekeep_target(tmp_path, 4, 365, throttle=False).complete  # a disabled target still compresses
    assert _segments(tmp_path, tid=4)
    tsdb.delete_target(tmp_path, 4)
    assert not (tmp_path / "target_4").exists()
    result = tsdb.housekeep_target(tmp_path, 4, 365, throttle=False)  # housekeeping after deletion is a no-op
    assert result.complete and result.rollups == {}
    assert not (tmp_path / "target_4").exists()


# --- 13, 14: multi-resolution reads, reports -----------------------------------------------------------------


def _three_resolutions(root: Path):
    _write_legacy(root, "ping_ms", NOW - timedelta(days=30), NOW, timedelta(minutes=5), value=lambda i, t: 10 + (i % 7))
    _write_legacy(
        root,
        "ping_ms@5m_median",
        NOW - timedelta(days=180),
        NOW - timedelta(days=30),
        timedelta(minutes=5),
        value=lambda i, t: 20 + (i % 5),
    )
    _write_legacy(
        root,
        "ping_ms@1h_median",
        NOW - timedelta(days=700),
        NOW - timedelta(days=180),
        timedelta(hours=1),
        value=lambda i, t: 30 + (i % 3),
    )
    _write_legacy(
        root,
        "ping_up",
        NOW - timedelta(days=30),
        NOW,
        timedelta(minutes=5),
        value=lambda i, t: 0 if i % 997 == 0 and i else 1,
    )


@pytest.mark.parametrize("span_days", [1, 40, 200, 600])
def test_chart_queries_over_several_resolutions_are_unchanged_by_compression(tmp_path, span_days):
    _three_resolutions(tmp_path)
    since = NOW - timedelta(days=span_days)

    def snapshot():
        pts = tsdb.query_auto_resolution(tmp_path, target_id=1, metric="ping_ms", since=since, until=NOW, limit=800)
        disp = tsdb.query_display_series(
            tmp_path, target_id=1, metric="ping_ms", since=since, until=NOW, bucket_seconds=3600
        )
        avail = tsdb.query_availability_series(tmp_path, target_id=1, since=since, until=NOW, enabled_services={"PING"})
        return [(p.ts, p.value) for p in pts], disp, avail

    expected = snapshot()
    assert expected[0]
    tsdb.compact_target(tmp_path, 1, throttle=False)
    assert snapshot() == expected


def test_downtime_periods_and_availability_are_unchanged_by_compression(tmp_path):
    down = NOW - timedelta(days=12)
    _write_legacy(
        tmp_path,
        "ping_up",
        NOW - timedelta(days=20),
        NOW,
        timedelta(minutes=1),
        value=lambda i, t: 0 if down <= t < down + timedelta(minutes=10) else 1,
    )
    expected = tsdb.query_downtime_periods(tmp_path, target_id=1, since=NOW - timedelta(days=20), until=NOW)
    assert expected and expected[0].failed_services == ["Ping"]
    _housekeep(tmp_path)
    assert tsdb.query_downtime_periods(tmp_path, target_id=1, since=NOW - timedelta(days=20), until=NOW) == expected


def test_pdf_report_is_built_from_compressed_history(tmp_path, monkeypatch):
    from app.services import pdf_report
    from app.services.pdf_report import PDFReportBuilder, ReportData

    _three_resolutions(tmp_path)
    tsdb.compact_target(tmp_path, 1, throttle=False)
    since, until = NOW - timedelta(days=120), NOW

    def fetch(metric):
        pts = tsdb.query_auto_resolution(tmp_path, target_id=1, metric=metric, since=since, until=until, limit=2000)
        return [{"ts": p.ts.isoformat(), "value": p.value} for p in pts]

    http, up = fetch("ping_ms"), fetch("ping_up")
    assert http and up
    target = {
        "name": "Example",
        "host": "www.example.com",
        "http_url": "https://www.example.com/",
        "enable_http_check": True,
        "enable_ping": True,
        "enable_tcp_connect": False,
        "interval_seconds": 300,
        "sla_enabled": False,
    }
    data = ReportData(
        target=target,
        from_date=since,
        to_date=until,
        http_data=http,
        uptime_data=up,
        ping_data=http,
        tcp_data=[],
        cert_data=[],
        status_code_data=[],
        tcp_port_data=None,
    )
    info = {"ip": "203.0.113.5", "asn": "AS64500", "provider": "Example", "location": "Berlin", "country_code": "DE"}
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: info)
    builder = PDFReportBuilder(data, "a4")
    monkeypatch.setattr(builder, "_get_whatweb_data", lambda: None)
    monkeypatch.setattr(builder, "_get_ssl_data", lambda: None)
    monkeypatch.setattr(builder, "_get_mtr_data", lambda **_k: ([], [], False))
    assert builder.build().startswith(b"%PDF")


# --- 15: backup and restore with both formats -----------------------------------------------------------------


def test_snapshot_copies_active_files_and_segments_consistently(tmp_path):
    root = tmp_path / "tsdb"
    _write_legacy(root, "ping_ms", NOW - timedelta(days=20), NOW, timedelta(minutes=10))  # will be compressed
    _write_legacy(root, "http_ms", NOW - timedelta(days=5), NOW, timedelta(minutes=10))  # stays legacy plain
    stops = iter([False, True])
    tsdb.compact_target(root, 1, throttle=False, should_stop=lambda: next(stops))
    (root / "target_1" / ".x.tmp").write_bytes(b"temp")
    expected = {m: _all(root, m) for m in ("ping_ms", "http_ms")}
    copied = tsdb.snapshot_tsdb(root, tmp_path / "snap")
    names = sorted(p.name for p in (tmp_path / "snap" / "target_1").iterdir())
    assert copied == len(names) and any(n.endswith(".jsonl.gz") for n in names) and "http_ms.jsonl" in names
    assert not any(n.endswith((".lock", ".tmp")) for n in names)
    assert {m: _all(tmp_path / "snap", m) for m in expected} == expected


# --- 16, 17, 18: parallel housekeeping, damaged archives, maintenance ------------------------------------------


def test_a_parallel_housekeeping_run_for_the_same_target_is_skipped(legacy):
    fd = os.open(legacy / "target_1" / ".housekeeping.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        res = _housekeep(legacy)
    finally:
        os.close(fd)
    assert res.skipped and not res.complete
    assert _segments(legacy) == []
    assert _housekeep(legacy).complete  # runs once the other one finished


def test_concurrent_housekeeping_threads_end_consistent(legacy):
    expected = _all(legacy, "ping_ms")
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(_housekeep(legacy))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 4 and any(not r.skipped for r in results)
    assert _housekeep(legacy).complete
    assert _all(legacy, "ping_ms") == expected


def _damage(path: Path, keep: int) -> None:
    path.write_bytes(path.read_bytes()[:keep])


def test_a_truncated_segment_is_read_up_to_the_damage_and_never_raises(legacy):
    original = _all(legacy, "ping_ms")
    _housekeep(legacy)
    victim = _segments(legacy)[1].path
    _damage(victim, victim.stat().st_size - 40)
    got = _all(legacy, "ping_ms")
    assert 0 < len(got) < len(original)
    assert set(got) <= set(original)
    assert tsdb.query(legacy, target_id=1, metric="ping_ms", limit=3, latest=True)
    stats = compute_storage_stats(legacy / "x.db", legacy)
    assert stats["tsdb_size_bytes"] > 0


def test_compressing_into_a_damaged_segment_keeps_its_readable_part_and_sets_the_file_aside(tmp_path):
    day = TODAY - timedelta(days=2)  # Monday of the current ISO week; the extra lines below share its segment
    _write_legacy(tmp_path, "ping_ms", day, day + timedelta(hours=12), timedelta(minutes=10))
    tsdb.compact_target(tmp_path, 1, throttle=False)
    victim = _segments(tmp_path)[0].path
    readable_before = len(_all(tmp_path, "ping_ms"))
    _damage(victim, victim.stat().st_size - 12)
    # more closed lines for the same week arrive in the active file
    with tsdb._series_path(tmp_path, 1, "ping_ms").open("a", encoding="utf-8") as f:
        for i in range(6):
            f.write(json.dumps({"ts": (day + timedelta(days=1, minutes=i)).isoformat(), "value": 1.0}) + "\n")
    assert tsdb.compact_target(tmp_path, 1, throttle=False).complete
    assert list(tmp_path.rglob("*.corrupt-*"))
    healthy = _all(tmp_path, "ping_ms")
    assert len(healthy) >= readable_before - 3 + 6  # readable prefix kept, new lines added, file valid again
    assert seg.segment_info(_segments(tmp_path)[0].path)[2] is False


def test_segment_listing_ignores_foreign_names_symlinks_and_bad_periods(tmp_path):
    d = tmp_path
    (d / "ping_ms.2026W41.jsonl.gz").write_bytes(gzip.compress(b'{"ts":"2026-10-05T00:00:00+00:00","value":1}\n'))
    (d / "ping_ms.2026W99.jsonl.gz").write_bytes(b"x")  # impossible week
    (d / "ping_ms.2026W41.jsonl.gz.corrupt-1").write_bytes(b"x")
    (d / "..%2fescape.2026W41.jsonl.gz").write_bytes(b"x")
    (d / "ping_ms.2026W42.jsonl.gz").symlink_to(d / "ping_ms.2026W41.jsonl.gz")
    assert [s.key for s in seg.list_segments(d, "ping_ms")] == ["2026W41"]
    assert seg.segment_stems(d) == {"ping_ms"}


def test_temp_files_are_created_private_and_exclusive(tmp_path):
    tmp, fd = seg.new_temp(tmp_path, "x")
    os.close(fd)
    assert tmp.stat().st_mode & 0o022 == 0  # not group- or world-writable
    with pytest.raises(FileExistsError):
        os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)


def test_housekeeping_stops_for_maintenance_and_leaves_remaining_series_untouched(tmp_path):
    names = ["a_ms", "b_ms", "c_ms", "d_ms"]
    for m in names:
        _write_legacy(tmp_path, m, NOW - timedelta(days=10), NOW, timedelta(minutes=30))
    expected = {m: _all(tmp_path, m) for m in names}
    first_done, outcome = threading.Event(), []
    real = tsdb._compact_series

    def slow(active, hot):
        moved = real(active, hot)
        first_done.set()
        deadline = time.monotonic() + 5
        while not maintenance.is_active() and time.monotonic() < deadline:
            time.sleep(0.005)
        return moved

    tsdb._compact_series = slow
    try:

        def work():
            with maintenance.work_section():
                outcome.append(_housekeep(tmp_path, should_stop=maintenance.is_active))

        t = threading.Thread(target=work)
        t.start()
        assert first_done.wait(5)
        assert maintenance.begin_maintenance(timeout=5)  # drains because the run notices the request
        t.join(5)
    finally:
        tsdb._compact_series = real
        maintenance.end_maintenance()
    assert outcome and not outcome[0].complete and outcome[0].compaction.series == 1
    assert {m: _all(tmp_path, m) for m in names} == expected
    assert _housekeep(tmp_path).complete
    assert {m: _all(tmp_path, m) for m in names} == expected


# --- storage statistics ---------------------------------------------------------------------------------------


def test_storage_stats_count_segments_and_report_the_measured_saving(legacy):
    before = compute_storage_stats(legacy / "x.db", legacy)
    _housekeep(legacy)
    after = compute_storage_stats(legacy / "x.db", legacy)
    assert after["total_points"] == before["total_points"] > 0
    assert after["tsdb_size_bytes"] < 0.5 * before["tsdb_size_bytes"]
    assert after["tsdb_saved_bytes"] > 0 and before["tsdb_saved_bytes"] == 0
    assert compute_storage_stats(legacy / "x.db", legacy) == after  # served from the segment cache


# --- review follow-ups: lock hold time, tail cache, quarantine order, error isolation -------------------------


def test_a_long_running_read_does_not_hold_the_series_lock_against_appends(legacy):
    _housekeep(legacy)
    p = tsdb._series_path(legacy, 1, "ping_ms")
    reader = tsdb._iter_series_lines(p, NOW - timedelta(days=20), None)
    next(reader)  # a reader is mid-way through a decompressing read
    done = threading.Event()

    def append():
        tsdb.append_point(legacy, target_id=1, metric="ping_ms", value=1.0, retention_days=365, at=NOW)
        done.set()

    t = threading.Thread(target=append)
    t.start()
    try:
        assert done.wait(5), "append_point (event loop in production) waited for a reader"
    finally:
        reader.close()
        t.join(5)


def test_a_read_keeps_one_consistent_state_when_compaction_publishes_meanwhile(legacy):
    p = tsdb._series_path(legacy, 1, "ping_ms")
    expected = _all(legacy, "ping_ms")
    reader = tsdb._iter_series_lines(p)
    first = next(reader)  # snapshot taken: the whole history is still in the active file
    _housekeep(legacy)  # moves most of it into segments and replaces the active file
    stamps = [seg.line_ts(first), *(seg.line_ts(line) for line in reader)]
    assert [t.isoformat() for t in stamps] == [t for t, _ in expected]


def test_latest_right_after_midnight_is_served_from_the_segment_tail_cache(tmp_path, monkeypatch):
    # Everything is older than today, so the active file is empty after compression.
    _write_legacy(tmp_path, "ping_ms", TODAY - timedelta(days=10), TODAY, timedelta(minutes=10))
    expected = _all(tmp_path, "ping_ms")[-5:]
    tsdb.compact_target(tmp_path, 1, throttle=False)
    tsdb._tail_cache.clear()
    decoded = []
    real = seg.iter_lines_from
    monkeypatch.setattr(seg, "iter_lines_from", lambda raw, name: decoded.append(name) or real(raw, name))
    for _ in range(3):
        assert [
            (p.ts.isoformat(), p.value) for p in tsdb.query_latest(tmp_path, target_id=1, metric="ping_ms", count=5)
        ] == expected
    assert len(decoded) == 1  # decoded once, then cached by (path, size, mtime)


def test_a_crash_while_setting_a_damaged_segment_aside_loses_nothing(tmp_path, monkeypatch):
    day = TODAY - timedelta(days=2)
    _write_legacy(tmp_path, "ping_ms", day, day + timedelta(hours=6), timedelta(minutes=10))
    tsdb.compact_target(tmp_path, 1, throttle=False)
    victim = _segments(tmp_path)[0].path
    readable = len(_all(tmp_path, "ping_ms"))
    _damage(victim, victim.stat().st_size - 12)
    with tsdb._series_path(tmp_path, 1, "ping_ms").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": (day + timedelta(days=1)).isoformat(), "value": 2.0}) + "\n")
    real = tsdb._set_aside

    def aside_then_crash(path):
        real(path)
        raise OSError("killed after the damaged file was set aside")

    with monkeypatch.context() as m:
        m.setattr(tsdb, "_set_aside", aside_then_crash)
        assert not tsdb.compact_target(tmp_path, 1, throttle=False).complete
    assert victim.exists() and list(tmp_path.rglob("*.corrupt-*"))  # nothing was removed
    assert tsdb.compact_target(tmp_path, 1, throttle=False).complete
    assert len(_all(tmp_path, "ping_ms")) >= readable - 3 + 1
    assert seg.segment_info(victim)[2] is False


def test_set_aside_files_expire_after_thirty_days(legacy):
    tdir = legacy / "target_1"
    old, new = tdir / "x.2026W01.jsonl.gz.corrupt-1", tdir / "x.2026W02.jsonl.gz.corrupt-2"
    for f in (old, new):
        f.write_bytes(b"x")
    stale = time.time() - tsdb._CORRUPT_KEEP_SECONDS - 60
    os.utime(old, (stale, stale))
    _housekeep(legacy)
    assert not old.exists() and new.exists()


def test_one_failing_series_does_not_stop_pruning_of_the_others(tmp_path, monkeypatch):
    for metric in ("a_ms", "b_ms"):
        _write_legacy(tmp_path, metric, NOW - timedelta(days=40), NOW, timedelta(hours=1))
    real = tsdb.prune_series

    def flaky(root, *, target_id, metric, retention_days):
        if metric == "a_ms":
            raise OSError("boom")
        return real(root, target_id=target_id, metric=metric, retention_days=retention_days)

    monkeypatch.setattr(tsdb, "prune_series", flaky)
    tsdb.prune_all_series(tmp_path, 1, 365)
    assert datetime.fromisoformat(_all(tmp_path, "b_ms")[0][0]) >= NOW - timedelta(days=30)
    assert datetime.fromisoformat(_all(tmp_path, "a_ms")[0][0]) < NOW - timedelta(days=30)  # untouched, retried later


def test_trimming_a_segment_streams_and_keeps_only_unexpired_lines(legacy):
    _housekeep(legacy)
    tsdb.prune_series(legacy, target_id=1, metric="ping_ms", retention_days=10)
    boundary = [s for s in _segments(legacy) if s.start < NOW - timedelta(days=10) < s.end]
    assert boundary
    lines = list(seg.iter_lines(boundary[0].path))
    assert lines and all(seg.line_ts(line) >= NOW - timedelta(days=10) for line in lines)


def test_a_set_aside_copy_is_aged_from_the_time_it_was_made_not_from_the_damaged_file(tmp_path):
    day = TODAY - timedelta(days=2)
    _write_legacy(tmp_path, "ping_ms", day, day + timedelta(hours=6), timedelta(minutes=10))
    tsdb.compact_target(tmp_path, 1, throttle=False)
    victim = _segments(tmp_path)[0].path
    _damage(victim, victim.stat().st_size - 12)
    long_ago = time.time() - 90 * 86400
    os.utime(victim, (long_ago, long_ago))  # a segment last written long before the damage was noticed
    with tsdb._series_path(tmp_path, 1, "ping_ms").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": (day + timedelta(days=1)).isoformat(), "value": 2.0}) + "\n")
    tsdb.compact_target(tmp_path, 1, throttle=False)
    assert list(tmp_path.rglob("*.corrupt-*"))
    _housekeep(tmp_path)  # the next housekeeping run must not delete the fresh copy
    assert list(tmp_path.rglob("*.corrupt-*"))


def _damaged_newest_segment(tmp_path):
    """Compress three days, truncate the newest segment; returns (victim, intact bytes, full series, readable count)."""
    _write_legacy(tmp_path, "ping_ms", TODAY - timedelta(days=3), TODAY, timedelta(minutes=10))
    full = _all(tmp_path, "ping_ms")
    tsdb.compact_target(tmp_path, 1, throttle=False)
    victim = _segments(tmp_path)[-1].path
    intact = victim.read_bytes()
    before = len(full) - seg.segment_info(victim)[0]  # points held by the older segments
    _damage(victim, victim.stat().st_size - 20)
    readable = len(seg.read_lines_safely(victim)[0])
    assert 5 <= readable < seg.segment_info(victim)[0] + 1_000_000  # a real, non-empty prefix
    tsdb._tail_cache.clear()
    return victim, intact, full, before + readable


def test_latest_serves_the_readable_prefix_of_a_damaged_segment(tmp_path):
    _, _, full, end = _damaged_newest_segment(tmp_path)
    got = tsdb.query_latest(tmp_path, target_id=1, metric="ping_ms", count=5)
    assert [(p.ts.isoformat(), p.value) for p in got] == full[end - 5 : end]


def test_a_damaged_segment_is_not_decompressed_again_while_unchanged(tmp_path, monkeypatch):
    _, _, full, end = _damaged_newest_segment(tmp_path)
    decoded = []
    real = seg.iter_lines_from
    monkeypatch.setattr(seg, "iter_lines_from", lambda raw, name: decoded.append(name) or real(raw, name))
    for _ in range(3):
        got = tsdb.query_latest(tmp_path, target_id=1, metric="ping_ms", count=5)
        assert [(p.ts.isoformat(), p.value) for p in got] == full[end - 5 : end]
    assert len(decoded) == 1
    assert [damaged for *_, damaged in tsdb._tail_cache.values()] == [True]  # marked, and bounded by the cache size


def test_a_repaired_segment_is_read_again_after_it_was_cached_as_damaged(tmp_path):
    victim, intact, full, end = _damaged_newest_segment(tmp_path)
    assert len(tsdb.query_latest(tmp_path, target_id=1, metric="ping_ms", count=5)) == 5
    assert victim.stat().st_size != len(intact)
    victim.write_bytes(intact)  # repaired: size and content differ from the cached damaged version
    got = tsdb.query_latest(tmp_path, target_id=1, metric="ping_ms", count=5)
    assert [(p.ts.isoformat(), p.value) for p in got] == full[-5:]
    assert full[-5:] != full[end - 5 : end]


def test_the_quarantine_copy_is_made_outside_the_series_lock_and_is_identical(tmp_path, monkeypatch):
    day = TODAY - timedelta(days=2)
    _write_legacy(tmp_path, "ping_ms", day, day + timedelta(hours=6), timedelta(minutes=10))
    tsdb.compact_target(tmp_path, 1, throttle=False)
    victim = _segments(tmp_path)[0].path
    _damage(victim, victim.stat().st_size - 12)
    damaged_bytes = victim.read_bytes()
    with tsdb._series_path(tmp_path, 1, "ping_ms").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": (day + timedelta(days=1)).isoformat(), "value": 2.0}) + "\n")
    held, copied_while_locked = [], []
    real_enter, real_exit, real_prepare = tsdb._FileLock.__enter__, tsdb._FileLock.__exit__, tsdb._prepare_aside
    monkeypatch.setattr(tsdb._FileLock, "__enter__", lambda self: held.append(1) or real_enter(self))
    monkeypatch.setattr(tsdb._FileLock, "__exit__", lambda self, *a: held.pop() and real_exit(self, *a))
    monkeypatch.setattr(tsdb, "_prepare_aside", lambda *a: copied_while_locked.append(bool(held)) or real_prepare(*a))
    assert tsdb.compact_target(tmp_path, 1, throttle=False).complete
    assert copied_while_locked == [False]
    (aside,) = tmp_path.rglob("*.corrupt-*")
    assert aside.read_bytes() == damaged_bytes
    assert not list(tmp_path.rglob("*.tmp"))


def test_a_segment_changing_during_the_quarantine_copy_drops_the_attempt_cleanly(tmp_path, monkeypatch):
    day = TODAY - timedelta(days=2)
    _write_legacy(tmp_path, "ping_ms", day, day + timedelta(hours=6), timedelta(minutes=10))
    tsdb.compact_target(tmp_path, 1, throttle=False)
    victim = _segments(tmp_path)[0].path
    _damage(victim, victim.stat().st_size - 12)
    with tsdb._series_path(tmp_path, 1, "ping_ms").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": (day + timedelta(days=1)).isoformat(), "value": 2.0}) + "\n")
    real_prepare = tsdb._prepare_aside

    def prepare_then_change(path, directory, sig):
        tmp = real_prepare(path, directory, sig)
        with path.open("ab") as f:
            f.write(b"\0")  # another writer touched the segment after the copy was taken
        return tmp

    monkeypatch.setattr(tsdb, "_prepare_aside", prepare_then_change)
    changed = victim.read_bytes()
    tsdb.compact_target(tmp_path, 1, throttle=False)
    assert victim.read_bytes() == changed + b"\0"
    assert not list(tmp_path.rglob("*.tmp")) and not list(tmp_path.rglob("*.corrupt-*"))
