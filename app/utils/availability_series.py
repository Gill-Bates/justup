#!/usr/bin/env python3
#
# app/utils/availability_series.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Run-length form of an availability sample stream, for reports over long periods.

One sample per check cycle makes a 365-day report at a 30 s interval about a million samples. A run
keeps the exact sample count and the real time bounds of consecutive samples with the same state, so
the counts (up, total) and every time-based result stay exact while memory depends on the number of
state changes, not on the number of samples.

A run ends at a state change, at an UNKNOWN sample, and at a measurement gap (the shared rule in
``availability_rules``). Time between two runs that is a gap is unmeasured: it is never part of a
run, so an outage is never extended across it. Samples inside a run are at most one gap threshold
apart, which is why the first and last sample of a run are enough for the outage rule.
"""

from __future__ import annotations

import logging
from array import array
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .availability_rules import is_gap

_log = logging.getLogger(__name__)

_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class AvailabilityRun:
    """``count`` consecutive samples with one state, from ``first`` to ``last``; ``value`` None is UNKNOWN."""

    value: int | None
    count: int
    first: datetime
    last: datetime


@dataclass(frozen=True)
class AvailabilitySeries:
    """Ordered runs of one availability stream plus the exact median sample spacing of the measured samples."""

    runs: tuple[AvailabilityRun, ...] = ()
    interval_seconds: float = 60.0
    median_step: timedelta | None = None

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
        """``(ts, 1 | 0 | None)`` for ``measured_outages``: the first and last sample of every run.

        Inside a run neighbours are never a gap apart and share one state, so dropping the samples between
        them leaves the outcome of the shared outage rule unchanged.
        """
        for run in self.runs:
            yield run.first, run.value
            if run.count > 1:
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
        parsed: list[tuple[datetime, int | None]] = []
        skipped = 0
        for point in points or ():
            try:
                ts = datetime.fromisoformat(point["ts"])
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            value = point.get("value")
            parsed.append((ts, int(value) if value in (0, 1) else None))
        if skipped:
            _log.warning("Availability series: skipped %d datapoints with invalid/missing timestamps", skipped)
        builder = AvailabilityBuilder(interval_seconds)
        for ts, value in sorted(parsed, key=lambda item: item[0]):
            builder.add(ts, value)
        return builder.build()


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
        # Microsecond spacings of measured samples; 8 bytes per sample instead of a dict per sample.
        self._steps = array("q")

    def add(self, ts: datetime, value: int | None) -> None:
        if value is not None:
            if self._prev_measured is not None and ts > self._prev_measured:
                self._steps.append((ts - self._prev_measured) // _MICROSECOND)
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

    def _close(self) -> None:
        if self._first is not None and self._last is not None:
            self._runs.append(AvailabilityRun(self._value, self._count, self._first, self._last))
        self._first = self._last = None
        self._count = 0

    def build(self) -> AvailabilitySeries:
        self._close()
        median = None
        if self._steps:
            ordered = sorted(self._steps)
            median = ordered[len(ordered) // 2] * _MICROSECOND
        return AvailabilitySeries(tuple(self._runs), self._interval, median)

