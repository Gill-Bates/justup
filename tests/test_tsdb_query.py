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


def test_large_file_early_stop_reads_few_lines(tmp_path):
    p = _write(tmp_path, [_line(i) for i in range(20_000)])
    got = _q(tmp_path, since=BASE, limit=3)
    assert [v for _, v in got] == [0, 1, 2]
    assert p.exists()


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

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(tsdb.query(tmp_path, target_id=1, metric="ping_ms", limit=1000)) == 400
