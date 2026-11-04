#!/usr/bin/env python3
#
# tests/test_downtime_view.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Downtime history view model and fragment: status terms, compact ranges, durations, empty states."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.api.frontend import templates
from app.utils.downtime_view import build_history, display_seconds, format_duration, format_precise, format_range
from tests import _support  # noqa: F401 - imported for its sys.path side effect

NOW = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)


def utc(*args: int, micro: int = 0) -> datetime:
    return datetime(*args, microsecond=micro, tzinfo=UTC)


def entry(kind: str, start: datetime, end: datetime | None, *services: str) -> dict:
    return {"kind": kind, "start": start, "end": end, "duration_seconds": None, "services": list(services)}


def render(history, *, has_data: bool = True, use_utc: bool = True) -> str:
    return templates.get_template("fragments/downtime_history.html").render(
        history=history, has_data=has_data, use_utc=use_utc, range_label="7d"
    )


def items(html: str) -> dict[str, str]:
    return {
        m.group(1): m.group(0)
        for m in re.finditer(r'<li class="downtime-item downtime-item--(\w+).*?</li>', html, re.DOTALL)
    }


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (45, "45s"), (60, "1m"), (270, "4m 30s"), (600, "10m"), (839, "14m"), (3600, "1h"),
     (10379, "2h 53m"), (10380, "2h 53m"), (3599, "1h"), (90000, "1d 1h"), (86400, "1d")],
)  # fmt: skip
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_duration_follows_the_displayed_seconds_not_the_jittered_microseconds():
    # 18:57:06.9 to 21:50:06.1 is 2 h 52 min 59.2 s exactly, but the shown times are 18:57:06 and 21:50:06:
    # the list must say 2 h 53 m, as the user subtracts the two displayed times.
    start, end = utc(2026, 11, 4, 18, 57, 6, micro=900000), utc(2026, 11, 4, 21, 50, 6, micro=100000)
    assert (end - start).total_seconds() < 10380
    assert display_seconds(start, end) == 10380
    [event] = build_history([entry("gap", start, end, "TCP:25")], now=NOW).events
    assert event.duration_text == "2h 53m"
    assert event.precise_text.endswith("2h 53m 0s")


@pytest.mark.parametrize(
    ("start", "end", "text"),
    [
        (utc(2026, 11, 4, 18, 57, 6), utc(2026, 11, 4, 21, 50, 6), "4 Nov · 18:57–21:50"),
        (utc(2026, 11, 4, 23, 50), utc(2026, 11, 5, 0, 15), "4 Nov 23:50 → 5 Nov 00:15"),
        (utc(2026, 12, 31, 23, 50), utc(2027, 1, 1, 0, 15), "31 Dec 2026 23:50 → 1 Jan 2027 00:15"),
        (utc(2025, 3, 2, 10, 0), utc(2025, 3, 2, 10, 5), "2 Mar 2025 · 10:00–10:05"),
        (utc(2026, 11, 4, 10, 0, 5), utc(2026, 11, 4, 10, 0, 50), "4 Nov · 10:00"),
        (utc(2026, 11, 4, 13, 6), None, "4 Nov · since 13:06"),
    ],
)
def test_format_range(start, end, text):
    assert format_range(start, end, now=NOW) == text


def test_precise_text_carries_seconds_and_zone():
    text = format_precise(utc(2026, 11, 4, 18, 57, 6), utc(2026, 11, 4, 21, 50, 6))
    assert text == "2026-11-04 18:57:06 UTC → 2026-11-04 21:50:06 UTC · 2h 53m 0s"
    assert format_precise(utc(2026, 11, 4, 13, 6), None) == "2026-11-04 13:06:00 UTC → ongoing"


def test_summary_keeps_data_gaps_apart_from_downtimes():
    start = utc(2026, 11, 4, 10, 0)
    history = build_history(
        [
            entry("gap", start, start + timedelta(hours=1), "TCP:25"),
            entry("unknown", start, start + timedelta(minutes=5), "UDP:53"),
            entry("down", start, start + timedelta(minutes=14), "HTTP", "TCP:443"),
        ],
        now=NOW,
    )
    assert (history.summary.downtimes, history.summary.data_gaps, history.summary.ongoing) == (1, 2, False)
    assert len(history.events) == 3


def test_events_are_newest_first_and_name_their_terms():
    early, late = utc(2026, 11, 4, 8, 0), utc(2026, 11, 4, 12, 0)
    history = build_history(
        [
            entry("down", early, early + timedelta(minutes=14), "HTTP", "TCP:443"),
            entry("unknown", late, late + timedelta(minutes=5), "UDP:53"),
        ],
        now=NOW,
    )
    assert [e.kind for e in history.events] == ["unknown", "down"]
    unknown, down = history.events
    assert (unknown.label, unknown.meta) == ("Unknown", ("UDP:53", "Result inconclusive"))
    assert (down.label, down.meta, down.duration_text) == ("Downtime", ("HTTP", "TCP:443"), "14m")


def test_ongoing_downtime_is_flagged_and_an_open_unknown_is_not_a_downtime():
    start = utc(2026, 11, 4, 13, 6)
    history = build_history([entry("down", start, None, "HTTP"), entry("unknown", start, None, "UDP:53")], now=NOW)
    assert history.summary.ongoing and history.summary.downtimes == 1 and history.summary.data_gaps == 1
    assert all(e.ongoing and e.duration_text == "Ongoing" for e in history.events)
    only_unknown = build_history([entry("unknown", start, None, "UDP:53")], now=NOW)
    assert not only_unknown.summary.ongoing and only_unknown.summary.downtimes == 0


def test_fragment_mixed_has_three_shapes_and_one_figure_row():
    start = utc(2026, 11, 4, 10, 0)
    html = render(
        build_history(
            [
                entry("down", start, start + timedelta(minutes=14), "HTTP"),
                entry("gap", start + timedelta(hours=1), start + timedelta(hours=4), "TCP:25"),
                entry("unknown", start + timedelta(hours=5), start + timedelta(hours=5, minutes=5), "UDP:53"),
            ],
            now=NOW,
        )
    )
    found = items(html)
    assert set(found) == {"down", "gap", "unknown"}
    assert "Downtime" in found["down"] and "Not downtime" not in found["down"]
    assert "No measurements" in found["gap"] and "Not downtime" in found["gap"]
    assert "Unknown" in found["unknown"] and "Result inconclusive" in found["unknown"]
    assert re.search(r"<strong>1</strong> Downtime<", html) and re.search(r"<strong>2</strong> Data gaps<", html)
    assert "uptime" not in "".join(found.values()).lower()
    assert 'class="downtime-scroll"' in html and html.count('class="downtime-scroll"') == 1


def test_fragment_escapes_service_names():
    start = utc(2026, 11, 4, 10, 0)
    html = render(
        build_history([entry("down", start, start + timedelta(minutes=5), "<img src=x onerror=alert(1)>")], now=NOW)
    )
    assert "<img" not in html and "&lt;img" in html


def test_fragment_gaps_only_shows_no_success_claim():
    start = utc(2026, 11, 4, 10, 0)
    html = render(build_history([entry("gap", start, start + timedelta(hours=1), "HTTP")], now=NOW))
    assert re.search(r"<strong>0</strong> Downtimes<", html) and re.search(r"<strong>1</strong> Data gap<", html)
    for claim in ("No outages", "check_circle", "All up", "stayed up"):
        assert claim not in html


def test_fragment_empty_states_are_separate():
    empty = build_history([], now=NOW)
    valid = render(empty, has_data=True)
    assert "No outages in selected range" in valid and re.search(r"<strong>0</strong> Downtimes<", valid)
    assert "<ol" not in valid and "svg" not in valid and 'material-icons">check' not in valid
    nodata = render(empty, has_data=False)
    assert "No measurement data" in nodata and "No checks recorded in this range" in nodata
    assert "Downtimes" not in nodata and "No outages" not in nodata
