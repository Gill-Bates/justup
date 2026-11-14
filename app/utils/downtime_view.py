#!/usr/bin/env python3
#
# app/utils/downtime_view.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""View model of the downtime history: status terms, compact time ranges and durations.

The periods themselves come from the TSDB queries (one gap rule in ``availability_rules``). This module only
names and formats them; it never decides what counts as downtime. ``down`` is the only downtime, ``gap`` (no
measurements) and ``unknown`` (inconclusive result) are unmeasured time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# kind -> (heading, short note shown next to the services). Only "down" is downtime.
KIND_TERMS: dict[str, tuple[str, str]] = {
    "down": ("Downtime", ""),
    "gap": ("No measurements", "Not downtime"),
    "unknown": ("Unknown", "Result inconclusive"),
}


@dataclass(frozen=True)
class DowntimeEvent:
    """One listed period, ready for the template."""

    kind: str
    label: str
    start_iso: str
    end_iso: str
    range_text: str
    precise_text: str
    duration_text: str
    ongoing: bool
    meta: tuple[str, ...]


@dataclass(frozen=True)
class DowntimeSummary:
    """Counts of the listed periods; data gaps cover ``gap`` and ``unknown`` and are never downtime."""

    downtimes: int
    data_gaps: int
    ongoing: bool


@dataclass(frozen=True)
class DowntimeHistory:
    summary: DowntimeSummary
    events: tuple[DowntimeEvent, ...]


def _whole_seconds(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(microsecond=0)


def display_seconds(start: datetime, end: datetime) -> int:
    """Length between the two displayed instants, which are cut to whole seconds.

    The stored period keeps microseconds (the check scheduler jitters); two jittered instants can sit
    59.2 s apart while their displayed seconds say 60 s. The duration follows what the user reads.
    """
    return max(int((_whole_seconds(end) - _whole_seconds(start)).total_seconds()), 0)


def format_duration(seconds: int) -> str:
    """Compact duration: ``45s``, ``4m 30s``, ``14m``, ``2h 53m``, ``1d 3h`` (minutes rounded from 10 m up)."""
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}m {rest}s" if rest else f"{minutes}m"
    total_minutes = (seconds + 30) // 60
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h" if hours else f"{days}d"
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _day(moment: datetime, with_year: bool) -> str:
    text = f"{moment.day} {MONTHS[moment.month - 1]}"
    return f"{text} {moment.year}" if with_year else text


def _clock(moment: datetime) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


def format_range(start: datetime, end: datetime | None, *, now: datetime) -> str:
    """Compact UTC range: ``4 Nov · 18:57–21:50``, ``4 Nov 23:50 → 5 Nov 00:15``, ``4 Nov · since 13:06``.

    The year appears only when the range is not in the current year or crosses a year boundary.
    """
    first = start.astimezone(UTC)
    last = end.astimezone(UTC) if end is not None else None
    with_year = first.year != now.astimezone(UTC).year or (last is not None and last.year != first.year)
    if last is None:
        return f"{_day(first, with_year)} · since {_clock(first)}"
    if first.date() == last.date():
        span = _clock(first) if _clock(first) == _clock(last) else f"{_clock(first)}–{_clock(last)}"
        return f"{_day(first, with_year)} · {span}"
    return f"{_day(first, with_year)} {_clock(first)} → {_day(last, with_year)} {_clock(last)}"


def _exact(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return " ".join(
        f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (secs, "s")) if value or unit == "s"
    )


def _precise(moment: datetime) -> str:
    return f"{moment.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC"


def format_precise(start: datetime, end: datetime | None) -> str:
    """Full UTC timestamps with seconds, for the tooltip and the screen-reader text."""
    if end is None:
        return f"{_precise(start)} → ongoing"
    return f"{_precise(start)} → {_precise(end)} · {_exact(display_seconds(start, end))}"


def _event(entry: Mapping[str, Any], now: datetime) -> DowntimeEvent:
    kind = entry["kind"]
    label, note = KIND_TERMS[kind]
    start: datetime = entry["start"]
    end: datetime | None = entry["end"]
    services = tuple(str(service) for service in entry.get("services") or ())
    return DowntimeEvent(
        kind=kind,
        label=label,
        start_iso=start.astimezone(UTC).isoformat(),
        end_iso=end.astimezone(UTC).isoformat() if end is not None else "",
        range_text=format_range(start, end, now=now),
        precise_text=format_precise(start, end),
        duration_text="Ongoing" if end is None else format_duration(display_seconds(start, end)),
        ongoing=end is None,
        meta=services + ((note,) if note else ()),
    )


def build_history(entries: Iterable[Mapping[str, Any]], *, now: datetime) -> DowntimeHistory:
    """Name and format the periods (``kind``, ``start``, ``end``, ``services``), newest first."""
    ordered: Sequence[Mapping[str, Any]] = sorted(entries, key=lambda entry: entry["start"], reverse=True)
    events = tuple(_event(entry, now) for entry in ordered)
    downtimes = [event for event in events if event.kind == "down"]
    return DowntimeHistory(
        summary=DowntimeSummary(
            downtimes=len(downtimes),
            data_gaps=len(events) - len(downtimes),
            ongoing=any(event.ongoing for event in downtimes),
        ),
        events=events,
    )
