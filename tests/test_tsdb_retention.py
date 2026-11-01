#!/usr/bin/env python3
#
# tests/test_tsdb_retention.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Per-target TSDB retention boundaries and rollup handoff behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import tsdb
from tests import _support  # noqa: F401


def _write_series(tsdb_dir: Path, metric: str, points: list[tuple[datetime, int]]) -> None:
    path = tsdb._series_path(tsdb_dir, 7, metric)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps({"ts": ts.isoformat(), "value": value}) + "\n" for ts, value in points),
        encoding="utf-8",
    )


def _append_series(tsdb_dir: Path, metric: str, point: tuple[datetime, int]) -> None:
    path = tsdb._series_path(tsdb_dir, 7, metric)
    with path.open("a", encoding="utf-8") as series:
        series.write(json.dumps({"ts": point[0].isoformat(), "value": point[1]}) + "\n")


@pytest.mark.parametrize("target_retention_days", [1, 45, 365, 3650, 5000])
def test_each_resolution_is_pruned_at_its_target_limited_cap(tmp_path, monkeypatch, target_retention_days):
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    monkeypatch.setattr(tsdb, "utcnow", lambda: now)
    for metric, configured_cap in (
        ("http_ms", 30),
        ("http_ms@5m_median", 180),
        ("http_ms@1h_median", 3650),
    ):
        effective = min(configured_cap, max(1, min(target_retention_days, 3650)))
        inside = now - timedelta(days=max(0.5, effective - 0.5))
        outside = now - timedelta(days=effective + 0.5)
        _write_series(tmp_path, metric, [(outside, 0), (inside, 1)])

    tsdb.prune_all_series(tmp_path, 7, target_retention_days)

    for metric in ("http_ms", "http_ms@5m_median", "http_ms@1h_median"):
        assert [point.value for point in tsdb.query(tmp_path, target_id=7, metric=metric)] == [1]


def test_rollups_handoff_at_target_retention_boundaries(tmp_path, monkeypatch):
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    monkeypatch.setattr(tsdb, "utcnow", lambda: now)
    _write_series(tmp_path, "http_ms", [(now - timedelta(days=30), 123)])

    first = tsdb.run_rollups(tmp_path, 7, retention_days=45)
    assert first == {"http_ms@5m_median": 1}
    assert tsdb.list_metrics(tmp_path, 7) == ["http_ms", "http_ms@5m_median"]

    # Rollups only convert complete buckets, so the 5m point must lie a full hour before the handoff.
    _append_series(tmp_path, "http_ms@5m_median", (now - timedelta(days=44, hours=2), 100))
    second = tsdb.run_rollups(tmp_path, 7, retention_days=45)
    assert second == {"http_ms@1h_median": 1}
    assert "http_ms5m_median@5m_median" not in tsdb.list_metrics(tmp_path, 7)
    assert len(tsdb.query(tmp_path, target_id=7, metric="http_ms@1h_median")) == 1
