#!/usr/bin/env python3
#
# tests/test_tsdb_query.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""TSDB query(): bounded-memory streaming must match the legacy readlines() result."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import tsdb
from app.utils.time import parse_utc
from tests import _support  # noqa: F401

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _write(tmp_path: Path, lines: list[str]) -> Path:
    p = tsdb._series_path(tmp_path, 1, "ping_ms")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _line(i: int) -> str:
    return json.dumps({"ts": (BASE + timedelta(minutes=i)).isoformat(), "value": i})


def _reference(p: Path, since, until, limit, latest):
    """Legacy implementation: read everything, filter, slice."""
    out = []
    for raw in p.read_text(encoding="utf-8").splitlines(keepends=True):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            ts = parse_utc(obj["ts"])
            if ts is None:
                continue
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        out.append((ts, obj.get("value")))
    return out[-limit:] if latest else out[:limit]


def _q(tmp_path, **kw):
    pts = tsdb.query(tmp_path, target_id=1, metric="ping_ms", **kw)
    return [(pt.ts, pt.value) for pt in pts]


@pytest.mark.parametrize("latest", [False, True])
@pytest.mark.parametrize("limit", [1, 7, 500, 50_000])
@pytest.mark.parametrize(
    "window",
    [(None, None), (100, None), (None, 300), (1000, 1010), (4990, None), (9999, 12000)],
)
def test_matches_reference_with_filters(tmp_path, latest, limit, window):
    lines = []
    for i in range(5000):
        lines.append(_line(i))
        # Fast path (latest, no filter) counts raw tail lines; keep the tail clean.
        if i % 500 == 0 and i < 4000:
            lines.append("{broken json")
            lines.append(json.dumps({"ts": "garbage", "value": 1}))
            lines.append(json.dumps({"value": 1}))
            lines.append("")
    p = _write(tmp_path, lines)
    since = BASE + timedelta(minutes=window[0]) if window[0] is not None else None
    until = BASE + timedelta(minutes=window[1]) if window[1] is not None else None
    got = _q(tmp_path, since=since, until=until, limit=limit, latest=latest)
    assert got == _reference(p, since, until, limit, latest)


def test_large_file_early_stop_reads_few_lines(tmp_path, monkeypatch):
    _write(tmp_path, [_line(i) for i in range(20_000)])
    parsed: list[int] = []
    real = tsdb._parse_point
    monkeypatch.setattr(tsdb, "_parse_point", lambda *a: parsed.append(1) or real(*a))
    got = _q(tmp_path, since=BASE, limit=3)
    assert [v for _, v in got] == [0, 1, 2]
    assert len(parsed) <= 10  # a full scan would parse all 20 000 lines


def test_missing_file_and_nonpositive_limit(tmp_path):
    assert _q(tmp_path, limit=5) == []
    _write(tmp_path, [_line(0)])
    assert _q(tmp_path, since=BASE, limit=0) == []


def test_concurrent_append_and_prune_keep_queries_consistent(tmp_path):
    now = tsdb.utcnow()
    for i in range(200):
        tsdb.append_point(
            tmp_path, target_id=1, metric="ping_ms", value=i, retention_days=30, at=now - timedelta(seconds=400 - i)
        )
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        try:
            for i in range(200, 400):
                if stop.is_set():
                    break
                tsdb.append_point(
                    tmp_path, target_id=1, metric="ping_ms", value=i, retention_days=30, at=now + timedelta(seconds=i)
                )
                if i % 50 == 0:
                    tsdb.prune_series(tmp_path, target_id=1, metric="ping_ms", retention_days=30)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                pts = tsdb.query(
                    tmp_path, target_id=1, metric="ping_ms", since=now - timedelta(days=1), limit=50, latest=True
                )
                ts = [pt.ts for pt in pts]
                assert ts == sorted(ts) and len(pts) <= 50
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, daemon=True), threading.Thread(target=reader, daemon=True)]
    try:
        for thread in threads:
            thread.start()
        threads[0].join(timeout=10)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, f"TSDB concurrency workers did not stop: {alive}"
    assert not errors
    assert len(tsdb.query(tmp_path, target_id=1, metric="ping_ms", limit=1000)) == 400


def _availability_series(tmp_path: Path, samples: dict[str, list[int]], *, skew_seconds: float = 0.01) -> None:
    """One point per cycle and metric, 30 s apart; each further metric lands a few ms after the previous one."""
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)  # inside retention
    for offset, (metric, values) in enumerate(samples.items()):
        for cycle, value in enumerate(values):
            at = start + timedelta(seconds=30 * cycle + skew_seconds * offset)
            tsdb.append_point(tmp_path, target_id=1, metric=metric, value=value, retention_days=30, at=at)


def test_availability_is_down_when_any_enabled_metric_is_down(tmp_path):
    _availability_series(tmp_path, {"http_up": [1, 1, 1, 1], "ping_up": [0, 0, 0, 0]})
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up")) == (0, 4)
    # The leading metric alone is what used to be reported as uptime.
    assert tsdb.count_binary_points(tmp_path, target_id=1, metric="http_up") == (4, 4)


def test_availability_counts_a_cycle_once_and_needs_every_metric_up(tmp_path):
    _availability_series(tmp_path, {"http_up": [1, 1, 0, 1], "ping_up": [1, 0, 1, 1], "tcp_up": [1, 1, 1, 1]})
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up", "tcp_up")) == (2, 4)


def test_availability_ignores_metrics_that_have_no_point_in_a_cycle(tmp_path):
    _availability_series(tmp_path, {"http_up": [1, 1, 1], "ping_up": [1]})
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up")) == (3, 3)
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up", "tcp_up")) == (3, 3)


def test_availability_without_any_series_is_empty(tmp_path):
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up")) == (0, 0)


def test_availability_ignores_series_of_checks_that_are_no_longer_enabled(tmp_path):
    _availability_series(tmp_path, {"http_up": [1, 1, 1], "ping_up": [0, 0, 0]})
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up",)) == (3, 3)


def _shared_cycles(tmp_path: Path, samples: dict[str, list[int]], *, start: datetime | None = None) -> datetime:
    """Points written the way the scheduler does: all metrics of one cycle carry the same timestamp."""
    start = start or datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    for metric, values in samples.items():
        for cycle, value in enumerate(values):
            at = start + timedelta(seconds=30 * cycle)
            tsdb.append_point(tmp_path, target_id=1, metric=metric, value=value, retention_days=30, at=at)
    return start


def test_cycle_points_with_a_shared_timestamp_form_one_sample_for_any_number_of_services(tmp_path):
    _shared_cycles(tmp_path, {"http_up": [1, 1, 0], "ping_up": [1, 0, 1], "tcp_up": [1, 1, 1]})
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up", "tcp_up")) == (1, 3)


def test_legacy_points_stamped_individually_within_two_seconds_still_form_one_sample(tmp_path):
    _availability_series(tmp_path, {"http_up": [1, 1], "ping_up": [0, 1]}, skew_seconds=1.9)
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up")) == (1, 2)


def test_legacy_points_more_than_two_seconds_apart_are_documented_as_separate_samples(tmp_path):
    # Fallback limit for data written before the shared cycle timestamp: grouping is by time window only.
    _availability_series(tmp_path, {"http_up": [1], "ping_up": [0]}, skew_seconds=3.0)
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up")) == (1, 2)


def test_shared_and_legacy_cycles_in_one_window_are_each_counted_once(tmp_path):
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    _shared_cycles(tmp_path, {"http_up": [1, 0], "ping_up": [0, 0]}, start=start)  # series files are append-only
    _availability_series(tmp_path, {"http_up": [1, 1], "ping_up": [1, 0]})
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up")) == (1, 4)


def test_a_check_enabled_later_does_not_count_against_earlier_cycles(tmp_path):
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    _shared_cycles(tmp_path, {"http_up": [1, 1, 1, 1]}, start=start)
    for cycle in (2, 3):
        at = start + timedelta(seconds=30 * cycle)
        tsdb.append_point(tmp_path, target_id=1, metric="ping_up", value=0, retention_days=30, at=at)
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up")) == (2, 4)


def test_a_window_edge_between_two_cycles_never_splits_a_cycle(tmp_path):
    start = _shared_cycles(tmp_path, {"http_up": [1, 1, 1], "ping_up": [1, 0, 1]})
    since = start + timedelta(seconds=30)
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up"), since=since) == (1, 2)
    assert tsdb.count_availability(
        tmp_path, target_id=1, metrics=("http_up", "ping_up"), since=since + timedelta(seconds=1)
    ) == (1, 1)


def test_cycles_are_unchanged_by_compaction_into_segments(tmp_path):
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(days=5)
    _shared_cycles(tmp_path, {"http_up": [1, 1, 0, 1], "ping_up": [1, 0, 0, 1]}, start=start)
    metrics = ("http_up", "ping_up")
    before = tsdb.count_availability(tmp_path, target_id=1, metrics=metrics)
    tsdb.housekeep_target(tmp_path, 1, 30, throttle=False)
    assert before == (2, 4)
    assert tsdb.count_availability(tmp_path, target_id=1, metrics=metrics) == before


def test_availability_aggregation_streams_a_large_series_in_bounded_time(tmp_path):
    cycles = 20000
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(days=20)
    for metric in ("http_up", "ping_up"):
        p = tsdb._series_path(tmp_path, 1, metric)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for i in range(cycles):
                ts = (start + timedelta(seconds=60 * i)).isoformat()
                f.write(json.dumps({"ts": ts, "value": int(i % 10 != 0 or metric == "http_up")}) + "\n")
    result = tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up", "ping_up"))
    assert result == (cycles - cycles // 10, cycles)
