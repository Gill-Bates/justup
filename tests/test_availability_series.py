#!/usr/bin/env python3
#
# tests/test_availability_series.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The run form of the PDF availability data against the former one-dict-per-sample implementation.

The oracle functions below are the former production code (every sample materialised, every
consumer working on the full list). The runs must give identical counts, outages, downtime and
chart blocks for 24 h, 30 d and 365 d, including short single failures, long stable phases,
measurement gaps and UNKNOWN samples.
"""

from __future__ import annotations

import fcntl
import itertools
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import tsdb
from app.services import pdf_report
from app.utils.availability_rules import MIN_DOWNTIME_SECONDS, is_gap, measured_outages
from app.utils.availability_series import AvailabilityBuilder, AvailabilitySeries
from app.utils.downtime_view import display_seconds
from tests import _support  # noqa: F401

END = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)

# (days, check interval seconds): the sample count is limited to keep the suite fast; the 365 day /
# 30 s volume is covered by the measurement noted in the change report, not by a unit test.
PERIODS = [pytest.param(1, 30, id="24h"), pytest.param(30, 120, id="30d"), pytest.param(365, 600, id="365d")]


def _scripted_cycles(days: int, step: int, seed: int = 11) -> list[tuple[datetime, int | None]]:
    """Check cycles (ts, 1 | 0 | None) with single failures, long outages, gaps, holes and UNKNOWN samples."""
    rnd = random.Random(seed)  # noqa: S311 - reproducible test data
    count = days * 86400 // step
    down: set[int] = {rnd.randrange(count) for _ in range(max(2, days // 2))}  # single failed cycles
    for _ in range(max(1, days // 20)):
        first = rnd.randrange(count)
        down.update(range(first, first + rnd.randrange(3, 40)))  # outages of several cycles
    absent: set[int] = set()
    for _ in range(max(1, days // 30)):
        first = rnd.randrange(count)
        absent.update(range(first, first + rnd.randrange(5, 60)))  # measurement gaps above the gap rule
    holes = {rnd.randrange(count) for _ in range(max(2, days // 5))}  # one or two missed cycles (no gap)
    holes |= {h + 1 for h in holes if rnd.random() < 0.5}
    unknown = {rnd.randrange(count) for _ in range(max(2, days // 5))}
    # An outage right before a gap, one right after it and an ongoing one at the very end.
    start_gap = count // 2
    absent.update(range(start_gap, start_gap + 30))
    down.update(range(start_gap - 6, start_gap))
    down.update(range(start_gap + 30, start_gap + 34))
    down.update(range(count - 8, count))
    absent -= set(range(count - 8, count))
    holes -= set(range(count - 8, count))
    cycles: list[tuple[datetime, int | None]] = []
    start = END - timedelta(days=days)
    for i in range(count):
        if i in absent or i in holes:
            continue
        ts = start + timedelta(seconds=i * step, microseconds=rnd.randrange(900_000))
        cycles.append((ts, None if i in unknown else 0 if i in down else 1))
    return cycles


def _series_of(cycles: list[tuple[datetime, int | None]], step: int) -> AvailabilitySeries:
    builder = AvailabilityBuilder(step)
    for ts, value in cycles:
        builder.add(ts, value)
    return builder.build()


# Oracle comparison


def _oracle_events(cycles, step):
    points = list(cycles)  # (ts, 1 | 0 | None) in time order, exactly the former per-sample input
    return [
        pdf_report.DowntimeEvent(
            start=o.start,
            end=o.end,
            duration_minutes=None if o.end is None else display_seconds(o.start, o.end) // 60,
        )
        for o in measured_outages(points, interval_seconds=step)
        if o.end is None or (o.seconds() or 0) >= MIN_DOWNTIME_SECONDS
    ]


def _oracle_downtime_minutes(cycles, step, fallback):
    last = max((ts for ts, _ in cycles), default=fallback)
    total = 0
    for event in _oracle_events(cycles, step):
        if event.end is not None:
            total += event.duration_minutes or 0
        else:
            total += int(max(0.0, (last - event.start).total_seconds()) / 60)
    return total


def _oracle_uptime_blocks(cycles, from_date, to_date):
    """The former status strip: one block per measured sample."""
    points = [(ts, v) for ts, v in cycles if v is not None]
    ts = [t for t, _ in points]
    step = pdf_report._median_step(ts) or timedelta(minutes=1)  # noqa: SLF001
    limit = step * 3
    min_width = (to_date - from_date) * 0.004
    blocks: dict[str, list[tuple[datetime, datetime]]] = {"up": [], "down": []}
    for i, (t, v) in enumerate(points):
        nxt = ts[i + 1] if i + 1 < len(ts) else None
        end = nxt if nxt is not None and nxt - t <= limit else t + step
        end = min(end, to_date)
        start = max(t, from_date)
        if end <= start:
            continue
        key = "up" if v == 1 else "down"
        if key == "down" and end - start < min_width:
            end = start + min_width
        blocks[key].append((start, end))
    return blocks


def _union(spans):
    """Merge touching or overlapping ``(start, end)`` spans, so blocks compare by what they cover."""
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _as_spans(blocks):
    return _union((x, x + w) for x, w in blocks)


def _num_spans(blocks):
    n = pdf_report.mdates.date2num
    return _union((n(a), n(b)) for a, b in blocks)


def _bridge(blocks, tolerance_days):
    """Merge ``(start, width)`` blocks whose hole is at most ``tolerance_days``."""
    merged: list[list[float]] = []
    for x, w in sorted(blocks):
        if merged and x - merged[-1][1] <= tolerance_days:
            merged[-1][1] = max(merged[-1][1], x + w)
        else:
            merged.append([x, x + w])
    return merged


def _assert_same_spans(actual, expected):
    assert len(actual) == len(expected)
    for (a0, a1), (e0, e1) in zip(actual, expected, strict=True):
        assert math.isclose(a0, e0, abs_tol=1e-7)
        assert math.isclose(a1, e1, abs_tol=1e-7)


def _assert_equivalent(series, cycles, step, since, until):
    measured = [v for _, v in cycles if v is not None]
    assert (series.up, series.total, len(series)) == (measured.count(1), len(measured), len(cycles))
    assert pdf_report._availability_pct(series.up, series.total) == pdf_report._availability_pct(  # noqa: SLF001
        measured.count(1), len(measured)
    )
    # Coverage is derived from the total only.
    window = (until - since).total_seconds()
    assert round(min(100.0, series.total * step / window * 100), 1) == round(
        min(100.0, len(measured) * step / window * 100), 1
    )
    assert series.last_sample == max(ts for ts, _ in cycles)
    assert (series.measured_first, series.measured_last) == (
        min(ts for ts, v in cycles if v is not None),
        max(ts for ts, v in cycles if v is not None),
    )
    assert pdf_report._extract_downtime_events(series, step) == _oracle_events(cycles, step)  # noqa: SLF001
    assert pdf_report._downtime_minutes(series, step, fallback=until) == _oracle_downtime_minutes(  # noqa: SLF001
        cycles, step, until
    )
    # The status strip draws the same up and down spans as the one-block-per-sample strip.
    blocks = pdf_report._uptime_blocks(series, since, until, UTC)  # noqa: SLF001
    expected = _oracle_uptime_blocks(cycles, since, until)
    for key in ("up", "down"):
        _assert_same_spans(_as_spans(blocks[key]), _num_spans(expected[key]))
    # The coverage bar bridges holes up to the shared gap rule (the former one drew a hairline per late sample).
    interval = timedelta(seconds=step)
    spans = [(run.first, run.last) for run in series.measured_runs]
    tolerance = 3 * step / 86400
    stamps = [ts for ts, v in cycles if v is not None]
    former_bar = pdf_report._coverage_blocks(stamps, interval, since, until)  # noqa: SLF001
    run_bar = pdf_report._coverage_span_blocks(spans, interval, since, until)  # noqa: SLF001
    _assert_same_spans(_bridge(run_bar, tolerance), _bridge(former_bar, tolerance))
    # Explicit gaps are exactly the unmeasured spans between neighbouring samples.
    stamps = [ts for ts, _ in cycles]
    assert series.gaps() == [(a, b) for a, b in itertools.pairwise(stamps) if is_gap((b - a).total_seconds(), step)]
    assert len(series.runs) < len(cycles) / 4


@pytest.mark.parametrize(("days", "step"), PERIODS)
def test_runs_equal_the_per_sample_results(days, step):
    cycles = _scripted_cycles(days, step)
    series = _series_of(cycles, step)
    _assert_equivalent(series, cycles, step, END - timedelta(days=days), END)
    points = [{"ts": ts.isoformat(), "value": v} for ts, v in cycles]
    assert AvailabilitySeries.from_points(points, step) == series  # list form and streaming form agree


def test_an_outage_is_never_carried_across_a_measurement_gap():
    step = 30
    base = END - timedelta(hours=2)
    cycles = [(base + timedelta(seconds=step * i), 0) for i in range(10)]
    cycles += [(base + timedelta(seconds=step * (i + 200)), 1) for i in range(5)]  # up again after a long gap
    series = _series_of(cycles, step)
    assert [(run.value, run.count) for run in series.runs] == [(0, 10), (1, 5)]
    (event,) = pdf_report._extract_downtime_events(series, step)  # noqa: SLF001
    assert event.end == cycles[9][0]  # proven up to the last DOWN sample, not until the next UP sample
    assert series.gaps() == [(cycles[9][0], cycles[10][0])]


def test_a_missing_value_is_unknown_not_downtime():
    step = 30
    base = END - timedelta(hours=1)
    values = [1, 1, 0, 0, None, None, 0, 0, 1]
    cycles = [(base + timedelta(seconds=step * i), v) for i, v in enumerate(values)]
    series = _series_of(cycles, step)
    assert (series.up, series.total, len(series)) == (3, 7, 9)
    assert pdf_report._extract_downtime_events(series, step) == _oracle_events(cycles, step)  # noqa: SLF001


# TSDB snapshot comparison


def _write_target(root: Path, cycles) -> None:
    """Two availability series; an UNKNOWN cycle is a null value, which both queries ignore."""
    directory = root / "target_1"
    directory.mkdir(parents=True)
    rnd = random.Random(5)  # noqa: S311 - reproducible test data
    with (directory / "http_up.jsonl").open("w") as http, (directory / "ping_up.jsonl").open("w") as ping:
        for ts, value in cycles:
            stamp = ts.isoformat()
            ping.write(f'{{"ts": "{stamp}", "value": {"null" if value is None else value}}}\n')
            # HTTP alone fails now and then; the combined cycle is then down although ping is up.
            http_value = 0 if value == 1 and rnd.random() < 0.01 else value
            http.write(f'{{"ts": "{stamp}", "value": {"null" if http_value is None else http_value}}}\n')


def _former_samples(root, since, until, metrics=("http_up", "ping_up")):
    """The former ``query_availability_samples``: every sample materialised as a dict."""
    return [
        {"ts": ts.isoformat(), "value": value}
        for ts, value in tsdb._availability_cycles(root, 1, metrics, since, until)  # noqa: SLF001
    ]


@pytest.mark.parametrize("compact", [False, True], ids=["active-file", "segments"])
@pytest.mark.parametrize(("days", "step"), PERIODS)
def test_snapshot_runs_equal_the_former_query(tmp_path, days, step, compact):
    cycles = _scripted_cycles(days, step, seed=3)
    _write_target(tmp_path, cycles)
    if compact:
        assert tsdb.compact_target(tmp_path, 1, now=END + timedelta(days=2), throttle=False).complete
    since = END - timedelta(days=days)
    former = _former_samples(tmp_path, since, END)
    series = tsdb.query_availability_runs(
        tmp_path, target_id=1, metrics=("http_up", "ping_up"), interval_seconds=step, since=since, until=END
    )
    points = [(datetime.fromisoformat(p["ts"]), p["value"]) for p in former]
    _assert_equivalent(series, points, step, since, END)
    assert len(series) == len(former)


def test_the_target_lock_is_released_while_reading_and_later_appends_stay_out(tmp_path, monkeypatch):
    step = 30
    cycles = _scripted_cycles(1, step, seed=9)
    _write_target(tmp_path, cycles)
    lock_file = tmp_path / "target_1" / ".batch.lock"
    outcome: list[str] = []
    real_parse = tsdb._parse_point  # noqa: SLF001

    def parse_and_probe(raw_line, since, until):
        if not outcome:
            # A writer takes the exclusive target lock in the middle of the read and appends a new cycle.
            fd = lock_file.open("a+")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                outcome.append("blocked")
            else:
                outcome.append("free")
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()
            assert tsdb.append_points(
                tmp_path,
                target_id=1,
                points=[("http_up", 0), ("ping_up", 0)],
                retention_days=3650,
                at=END - timedelta(seconds=1),
                lock_lost=lambda: False,
            )
        return real_parse(raw_line, since, until)

    monkeypatch.setattr(tsdb, "_parse_point", parse_and_probe)
    series = tsdb.query_availability_runs(
        tmp_path, target_id=1, metrics=("http_up", "ping_up"), interval_seconds=step, since=END - timedelta(days=1)
    )
    monkeypatch.undo()
    assert outcome == ["free"]
    assert series.last_sample is not None and series.last_sample < END - timedelta(seconds=1)
    assert len(series) == len(_former_samples(tmp_path, END - timedelta(days=1), END - timedelta(seconds=2)))


def _probe_target_lock(monkeypatch, lock_file: Path, outcome: list[str], on_first=None) -> None:
    """On the first snapshot read, record whether a writer could take the exclusive target lock."""
    real = tsdb._iter_snapshot_lines  # noqa: SLF001

    def probe(snap, *, bounded=False):
        if not outcome:
            with lock_file.open("a+") as fd:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    outcome.append("blocked")
                else:
                    outcome.append("free")
                    fcntl.flock(fd, fcntl.LOCK_UN)
            if on_first is not None:
                on_first()
        return real(snap, bounded=bounded)

    monkeypatch.setattr(tsdb, "_iter_snapshot_lines", probe)


@pytest.mark.parametrize(
    "reader",
    [
        lambda root, since: tsdb.query(root, target_id=1, metric="http_up", since=since, until=END, limit=10**6),
        lambda root, since: tsdb.query_zero_points(root, target_id=1, metric="http_up", since=since, until=END),
        lambda root, since: tsdb.count_binary_points(root, target_id=1, metric="http_up", since=since, until=END),
        lambda root, since: tsdb.count_availability(
            root, target_id=1, metrics=("http_up", "ping_up"), since=since, until=END
        ),
        lambda root, since: tsdb.query_availability_cycles(
            root, target_id=1, metrics=("http_up", "ping_up"), since=since, until=END
        ),
        lambda root, since: tsdb.query_availability_series(root, target_id=1, since=since, until=END),
        lambda root, since: tsdb.query_downtime_periods(root, target_id=1, since=since, until=END),
    ],
    ids=["query", "zero", "binary", "availability", "cycles", "series", "downtime"],
)
def test_target_reads_release_the_lock_before_evaluating(tmp_path, monkeypatch, reader):
    _write_target(tmp_path, _scripted_cycles(1, 30, seed=9))
    outcome: list[str] = []
    _probe_target_lock(monkeypatch, tmp_path / "target_1" / ".batch.lock", outcome)
    reader(tmp_path, END - timedelta(days=1))
    assert outcome == ["free"]


def test_a_query_sees_the_state_of_its_snapshot_while_appends_continue(tmp_path, monkeypatch):
    _write_target(tmp_path, _scripted_cycles(1, 30, seed=9))
    since = END - timedelta(days=1)

    def read() -> list[tsdb.MetricPoint]:
        return tsdb.query(tmp_path, target_id=1, metric="http_up", since=since, until=END, limit=10**6)

    before = read()

    def append() -> None:
        assert tsdb.append_points(
            tmp_path,
            target_id=1,
            points=[("http_up", 0), ("ping_up", 0)],
            retention_days=3650,
            at=END - timedelta(seconds=1),
            lock_lost=lambda: False,
        )

    outcome: list[str] = []
    _probe_target_lock(monkeypatch, tmp_path / "target_1" / ".batch.lock", outcome, on_first=append)
    during = read()
    monkeypatch.undo()
    assert outcome == ["free"]
    assert during == before
    assert len(read()) == len(before) + 1


@pytest.mark.parametrize("raw", ["0.7", "1.5", "-1", "NaN", "Infinity", "-Infinity", "null", '"abc"', '"1"', "[1]"])
def test_non_binary_values_are_no_availability_state(tmp_path, raw):
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    (tmp_path / "target_1").mkdir()
    values = ["1", raw, "1"]
    lines = [f'{{"ts": "{(base + timedelta(minutes=i)).isoformat()}", "value": {v}}}' for i, v in enumerate(values)]
    (tmp_path / "target_1" / "http_up.jsonl").write_text("\n".join(lines) + "\n")
    window = {"since": base - timedelta(minutes=1), "until": base + timedelta(minutes=10)}
    graph = tsdb._availability_transitions(tmp_path, target_id=1, metric="http_up", **window)  # noqa: SLF001
    assert tsdb.count_binary_points(tmp_path, target_id=1, metric="http_up", **window) == (2, 2)
    assert {point["up"] for point in graph} <= {1, None}
    assert all(point["up"] == 1 for point in graph if point["ts"] == base.isoformat())


def test_binary_values_are_availability_state(tmp_path):
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    (tmp_path / "target_1").mkdir()
    lines = [f'{{"ts": "{(base + timedelta(minutes=i)).isoformat()}", "value": {v}}}' for i, v in enumerate("101")]
    (tmp_path / "target_1" / "http_up.jsonl").write_text("\n".join(lines) + "\n")
    window = {"since": base - timedelta(minutes=1), "until": base + timedelta(minutes=10)}
    graph = tsdb._availability_transitions(tmp_path, target_id=1, metric="http_up", **window)  # noqa: SLF001
    assert tsdb.count_binary_points(tmp_path, target_id=1, metric="http_up", **window) == (2, 3)
    assert 0 in {point["up"] for point in graph}


def _true_median(cycles):
    steps = sorted(b - a for (a, _), (b, _) in itertools.pairwise(cycles) if b > a)
    return steps[len(steps) // 2]


def test_median_is_exact_while_few_distinct_spacings_occur():
    rnd = random.Random(7)  # noqa: S311 - seeded, deterministic test data
    cycles, at = [], END - timedelta(days=2)
    for _ in range(5000):
        at += timedelta(seconds=rnd.choice((29, 30, 30, 30, 31)), microseconds=rnd.choice((0, 5)))
        cycles.append((at, 1))
    series = _series_of(cycles, 30)
    assert series.median_step == _true_median(cycles)
    assert series.median_error == timedelta(0)


def test_median_memory_is_bounded_and_stays_within_its_reported_error():
    rnd = random.Random(11)  # noqa: S311 - seeded, deterministic test data
    builder = AvailabilityBuilder(30)
    at, steps = END - timedelta(days=400), []
    for _ in range(200_000):
        step = timedelta(seconds=30, microseconds=rnd.randrange(900_000))
        at += step
        steps.append(step)
        builder.add(at, 1)
    series = builder.build()
    assert len(builder._bins) <= 2048  # noqa: SLF001
    assert series.median_error > timedelta(0)
    true = sorted(steps[1:])[len(steps[1:]) // 2]
    assert abs(series.median_step - true) <= series.median_error
