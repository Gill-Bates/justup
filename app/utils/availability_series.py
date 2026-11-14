#!/usr/bin/env python3
#
# app/utils/availability_series.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Run-length form of an availability sample stream, for reports over long periods.

One sample per check cycle makes a 365-day report at a 30 s interval about a million samples. A run
keeps the exact sample count and the real time bounds of consecutive samples with the same state, so
the counts (up, total) and every time-based result stay exact. The runs need memory in the order of the
number of state changes. The median sample spacing comes from a histogram of at most ``_MAX_BINS`` bins:
it is exact (microsecond) while that many distinct spacings suffice and otherwise within one bin width
(``AvailabilitySeries.median_error``) of the true median. ``from_points`` folds a time-ordered ``Sequence``
while parsing, without a second copy of the points. Any other iterable (for example a generator) and an
unordered sequence are parsed into a list and sorted first, so they are held in memory in full.

A run ends at a state change, at an UNKNOWN sample, and at a measurement gap (the shared rule in
``availability_rules``). Time between two runs that is a gap is unmeasured: it is never part of a
run, so an outage is never extended across it. Samples inside a run are at most one gap threshold
apart, which is why the first and last sample of a run are enough for the outage rule.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .availability_rules import gap_threshold_seconds, is_gap

_log = logging.getLogger(__name__)

_MICROSECOND = timedelta(microseconds=1)
_MAX_BINS = 2048  # bounds the spacing histogram; a bin is ~100 bytes, so well under 1 MiB


@dataclass(frozen=True, slots=True)
class AvailabilityRun:
    """``count`` consecutive samples with one state, from ``first`` to ``last``; ``value`` None is UNKNOWN."""

    value: int | None
    count: int
    first: datetime
    last: datetime


@dataclass(frozen=True)
class AvailabilitySeries:
    """Ordered runs with a median spacing; ``median_error`` bounds any histogram approximation."""

    runs: tuple[AvailabilityRun, ...] = ()
    interval_seconds: float = 60.0
    median_step: timedelta | None = None
    median_error: timedelta = timedelta(0)  # 0 = exact; else the median is within this of the true one

    def __bool__(self) -> bool:
        return bool(self.runs)

    def __len__(self) -> int:
        """Number of samples including UNKNOWN ones, like the list of points this form replaces."""
        return sum(run.count for run in self.runs)

    @property
    def total(self) -> int:
        """Measured samples (UP or DOWN)."""
        return sum(run.count for run in self.runs if run.value is not None)

    @property
    def up(self) -> int:
        return sum(run.count for run in self.runs if run.value == 1)

    @property
    def measured_runs(self) -> list[AvailabilityRun]:
        return [run for run in self.runs if run.value is not None]

    @property
    def measured_first(self) -> datetime | None:
        measured = self.measured_runs
        return measured[0].first if measured else None

    @property
    def measured_last(self) -> datetime | None:
        measured = self.measured_runs
        return measured[-1].last if measured else None

    @property
    def last_sample(self) -> datetime | None:
        """Time of the newest sample, UNKNOWN ones included."""
        return self.runs[-1].last if self.runs else None

    def outage_points(self) -> Iterator[tuple[datetime, int | None]]:
        """``(ts, 1 | 0 | None)`` for ``measured_outages``, generated lazily without materialising samples.

        Inside a run neighbours are never a gap apart and share one state. The first and last sample are real;
        between them one stand-in point per gap threshold keeps the shared outage rule from seeing a gap that
        only exists because the samples in between were folded away.
        """
        step = timedelta(seconds=gap_threshold_seconds(self.interval_seconds))
        for run in self.runs:
            yield run.first, run.value
            if run.count > 1:
                if step > timedelta(0):
                    stand_in = run.first + step
                    while stand_in < run.last:
                        yield stand_in, run.value
                        stand_in += step
                yield run.last, run.value

    def gaps(self) -> list[tuple[datetime, datetime]]:
        """Unmeasured spans: ``(last sample of a run, first sample of the next)`` where the rule sees a gap."""
        return [
            (a.last, b.first)
            for a, b in zip(self.runs, self.runs[1:], strict=False)
            if is_gap((b.first - a.last).total_seconds(), self.interval_seconds)
        ]

    @classmethod
    def from_points(cls, points: Iterable[Mapping[str, Any]], interval_seconds: float) -> AvailabilitySeries:
        """Build from ``{"ts", "value"}`` dicts; a value other than 0/1 is UNKNOWN, an unreadable ts is skipped."""
        skipped = 0

        def parse(items: Iterable[Mapping[str, Any]]) -> Iterator[tuple[datetime, int | None]]:
            nonlocal skipped
            for point in items:
                try:
                    ts = datetime.fromisoformat(point["ts"])
                except (KeyError, TypeError, ValueError):
                    skipped += 1
                    continue
                value = point.get("value")
                yield ts, int(value) if value in (0, 1) else None

        points = points or ()
        series: AvailabilitySeries | None = None
        if isinstance(points, Sequence):
            # A time-ordered sequence is folded while parsing; a disorder falls back to the sorted path.
            builder = AvailabilityBuilder(interval_seconds)
            previous: datetime | None = None
            ordered = True
            for ts, value in parse(points):
                if previous is not None and ts < previous:
                    ordered = False
                    break
                previous = ts
                builder.add(ts, value)
            if ordered:
                series = builder.build()
        if series is None:
            skipped = 0
            builder = AvailabilityBuilder(interval_seconds)
            for ts, value in sorted(parse(points), key=lambda item: item[0]):
                builder.add(ts, value)
            series = builder.build()
        if skipped:
            _log.warning("Availability series: skipped %d datapoints with invalid/missing timestamps", skipped)
        return series


class AvailabilityBuilder:
    """Fold a time-ordered stream of ``(ts, 0 | 1 | None)`` samples into an ``AvailabilitySeries``."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = float(interval_seconds)
        self._runs: list[AvailabilityRun] = []
        self._value: int | None = None
        self._count = 0
        self._first: datetime | None = None
        self._last: datetime | None = None
        self._prev_measured: datetime | None = None
        # Histogram of measured-sample spacings: bin index -> count, bins ``_width`` microseconds wide.
        self._bins: dict[int, int] = {}
        self._width = 1
        self._step_count = 0

    def add(self, ts: datetime, value: int | None) -> None:
        if value is not None:
            if self._prev_measured is not None and ts > self._prev_measured:
                self._count_step((ts - self._prev_measured) // _MICROSECOND)
            self._prev_measured = ts
        if (
            self._first is not None
            and self._last is not None
            and value == self._value
            and not is_gap((ts - self._last).total_seconds(), self._interval)
        ):
            self._count += 1
            self._last = ts
            return
        self._close()
        self._value, self._count, self._first, self._last = value, 1, ts, ts

    def _count_step(self, micros: int) -> None:
        index = micros // self._width
        self._bins[index] = self._bins.get(index, 0) + 1
        self._step_count += 1
        while len(self._bins) > _MAX_BINS:
            merged: dict[int, int] = {}
            for key, n in self._bins.items():
                merged[key // 2] = merged.get(key // 2, 0) + n
            self._bins, self._width = merged, self._width * 2

    def _close(self) -> None:
        if self._first is not None and self._last is not None:
            self._runs.append(AvailabilityRun(self._value, self._count, self._first, self._last))
        self._first = self._last = None
        self._count = 0

    def build(self) -> AvailabilitySeries:
        self._close()
        median = None
        error = timedelta(0)
        if self._step_count:
            rank, seen = self._step_count // 2, 0  # same element as ordered[len // 2]
            for index in sorted(self._bins):
                seen += self._bins[index]
                if seen > rank:
                    micros = index * self._width + (self._width // 2 if self._width > 1 else 0)
                    median = micros * _MICROSECOND
                    break
            if self._width > 1:
                error = (self._width // 2 + 1) * _MICROSECOND
        return AvailabilitySeries(tuple(self._runs), self._interval, median, error)
