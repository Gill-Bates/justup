#!/usr/bin/env python3
#
# tests/test_downtime_periods.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Downtime from confirmed measurements only: one gap rule for history, graph, widget, uptime and PDF."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.api import metrics
from app.db import tsdb
from app.services import pdf_report
from app.utils.availability_rules import MIN_DOWNTIME_SECONDS, downtime_series, is_gap, measured_outages
from tests import _support  # noqa: F401

# Relative to now: the availability graph picks its TSDB tier (raw vs rollup) by the age of the data.
T0 = (datetime.now(UTC) - timedelta(hours=6)).replace(minute=0, second=0, microsecond=0)
HTTP = [("http_up", "HTTP")]


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def put(tmp: Path, metric: str, points: list[tuple[float, int]]) -> None:
    for minute, value in points:
        tsdb.append_point(tmp, target_id=1, metric=metric, value=value, retention_days=365, at=at(minute))


def periods(tmp: Path, series=HTTP, *, now: datetime | None = None, **kw):
    return tsdb.query_downtime_periods(
        tmp, target_id=1, series=series, check_interval_seconds=60, now=now or at(600), **kw
    )


def test_short_confirmed_outage_is_listed(tmp_path):
    put(tmp_path, "http_up", [(4, 1), (5, 0), (6, 1)])
    [period] = periods(tmp_path)
    assert (period.start, period.end, period.duration_seconds) == (at(5), at(6), 60)
    assert period.failed_services == ["HTTP"]


def test_several_outages_are_separate_periods_newest_first(tmp_path):
    put(tmp_path, "http_up", [(1, 1), (2, 0), (3, 0), (4, 1), (10, 1), (11, 0), (12, 1)])
    found = periods(tmp_path)
    assert [(p.start, p.duration_seconds) for p in found] == [(at(11), 60), (at(2), 120)]


def test_missing_measurements_between_down_and_up_are_not_downtime(tmp_path):
    # DOWN 03:05, nothing until UP 03:20: the 15 minutes in between are unknown, not an outage.
    put(tmp_path, "http_up", [(4, 1), (5, 0), (20, 1)])
    found = periods(tmp_path)
    assert all(p.duration_seconds < 15 * 60 for p in found)
    assert sum(p.duration_seconds or 0 for p in found) == 60  # only the one confirmed measurement


def test_continuous_down_every_minute_gives_the_measured_duration(tmp_path):
    put(tmp_path, "http_up", [(4, 1), *[(m, 0) for m in range(5, 15)], (15, 1)])
    [period] = periods(tmp_path)
    assert period.duration_seconds == 600


def test_gap_below_threshold_is_a_normal_outage(tmp_path):
    assert not is_gap(120, 60)
    put(tmp_path, "http_up", [(4, 1), (5, 0), (7, 1)])
    [period] = periods(tmp_path)
    assert period.duration_seconds == 120


def test_down_gap_down_is_two_outages_not_one_long_one(tmp_path):
    put(tmp_path, "http_up", [(5, 0), (6, 0), (30, 0), (31, 0), (32, 1)])
    found = periods(tmp_path)
    assert [p.duration_seconds for p in found] == [120, 60]
    assert sum(p.duration_seconds or 0 for p in found) < 5 * 60


def test_ongoing_outage_is_open_only_while_fresh(tmp_path):
    put(tmp_path, "http_up", [(1, 1), (2, 0), (3, 0)])
    [ongoing] = periods(tmp_path, now=at(3.5))
    assert ongoing.end is None and ongoing.duration_seconds is None
    [stale] = periods(tmp_path, now=at(120))  # scheduler stopped: nothing grows into the future
    assert stale.end == at(3) and stale.duration_seconds == 60


def test_recovery_closes_the_period(tmp_path):
    put(tmp_path, "http_up", [(1, 0), (2, 0), (3, 1), (4, 1)])
    [period] = periods(tmp_path, now=at(4.5))
    assert period.end == at(3)


def test_late_results_written_out_of_order_are_sorted(tmp_path):
    put(tmp_path, "http_up", [(8, 0), (5, 0), (6, 0), (9, 1)])
    [period] = periods(tmp_path)
    assert (period.start, period.end) == (at(5), at(9))


def test_per_port_histories_are_separate(tmp_path):
    put(tmp_path, "tcp_up_25", [(1, 1), (2, 1), (3, 1)])
    put(tmp_path, "tcp_up_587", [(1, 1), (2, 0), (3, 1)])
    found = periods(tmp_path, series=[("tcp_up_25", "TCP:25"), ("tcp_up_587", "TCP:587")])
    assert [p.failed_services for p in found] == [["TCP:587"]]


def test_services_with_offset_timestamps_merge_into_one_period(tmp_path):
    put(tmp_path, "http_up", [(1, 1), (5, 0), (6, 0), (7, 0), (8, 1)])
    for minute, value in [(1.5, 1), (6.5, 0), (7.5, 0), (8.5, 0), (9.5, 1)]:
        tsdb.append_point(tmp_path, target_id=1, metric="ping_up", value=value, retention_days=365, at=at(minute))
    [period] = periods(tmp_path, series=[("http_up", "HTTP"), ("ping_up", "Ping")])
    assert (period.start, period.end) == (at(5), at(9.5))
    assert period.failed_services == ["HTTP", "Ping"]


def test_disabled_check_with_old_data_is_not_downtime(tmp_path):
    put(tmp_path, "http_up", [(1, 0), (2, 0), (3, 1)])
    row = {"enable_http_check": 0, "enable_ping": 0, "enable_tcp_connect": 0, "enable_udp_scan": 0}
    assert downtime_series(row) == []
    assert periods(tmp_path, series=downtime_series(row)) == []
    row = {
        "enable_http_check": 1,
        "enable_tcp_connect": 1,
        "tcp_ports": "25,587",
        "enable_udp_scan": 1,
        "udp_ports": "53",
    }
    assert downtime_series(row) == [
        ("http_up", "HTTP"),
        ("tcp_up_25", "TCP:25"),
        ("tcp_up_587", "TCP:587"),
        ("udp_up_53", "UDP:53"),
    ]


def test_removed_port_is_not_downtime(tmp_path):
    put(tmp_path, "tcp_up_25", [(1, 0), (2, 0), (3, 1)])
    row = {"enable_tcp_connect": 1, "tcp_ports": "587"}
    assert periods(tmp_path, series=downtime_series(row)) == []


def test_tcp_aggregate_is_used_for_data_older_than_per_port_series(tmp_path):
    put(tmp_path, "tcp_up", [(1, 1), (2, 0), (3, 1)])
    [period] = periods(tmp_path, series=[("tcp_up_80", "TCP:80")])
    assert period.failed_services == ["TCP"]


def test_unknown_marker_ends_an_outage_and_is_never_downtime(tmp_path):
    put(tmp_path, "udp_up_53", [(5, 0), (7, 1)])
    assert periods(tmp_path, series=[("udp_up_53", "UDP:53")])[0].duration_seconds == 120
    put(tmp_path, "udp_unknown_53", [(6, 1)])
    [period] = periods(tmp_path, series=[("udp_up_53", "UDP:53")])
    assert period.duration_seconds == 60  # UNKNOWN at 03:06 is not counted as down


def test_unknown_only_series_has_no_downtime(tmp_path):
    put(tmp_path, "udp_unknown_53", [(m, 1) for m in range(1, 30)])
    assert periods(tmp_path, series=[("udp_up_53", "UDP:53")]) == []


def test_udp_and_tcp_series_with_the_same_port_do_not_collide(tmp_path):
    put(tmp_path, "tcp_up_53", [(1, 1), (2, 1), (3, 1)])
    put(tmp_path, "udp_up_53", [(1, 1), (2, 0), (3, 1)])
    found = periods(tmp_path, series=[("tcp_up_53", "TCP:53"), ("udp_up_53", "UDP:53")])
    assert [p.failed_services for p in found] == [["UDP:53"]]


def test_state_before_since_is_carried_in_but_not_across_a_gap(tmp_path):
    put(tmp_path, "http_up", [(1, 0), (2, 0), (3, 1)])
    [period] = periods(tmp_path, since=at(1.5))
    assert (period.start, period.end) == (at(1.5), at(3))
    put(tmp_path, "http_up", [(40, 1)])
    assert periods(tmp_path, since=at(30)) == []


def test_no_artificial_fifteen_minute_downtimes_from_scheduler_pauses(tmp_path):
    # A scheduler that pauses every 15 minutes (restart, maintenance) must not look like downtime.
    points = [(m, 0) for m in range(0, 240, 15)]
    put(tmp_path, "http_up", points)
    assert all((p.duration_seconds or 0) <= MIN_DOWNTIME_SECONDS for p in periods(tmp_path))


def test_graph_history_and_uptime_agree_on_gaps(tmp_path):
    put(tmp_path, "http_up", [(1, 1), *[(m, 0) for m in range(5, 10)], (30, 1), (31, 1)])
    [period] = periods(tmp_path)
    graph = tsdb._availability_transitions(
        tmp_path, target_id=1, metric="http_up", since=at(0), until=at(60), check_interval_seconds=60
    )
    down_from = next(p for p in graph if p["up"] == 0)
    gap_at = next(p for p in graph if p["up"] is None and p["ts"] > down_from["ts"])
    graph_seconds = (datetime.fromisoformat(gap_at["ts"]) - datetime.fromisoformat(down_from["ts"])).total_seconds()
    assert round(graph_seconds) == period.duration_seconds == 240  # same threshold, same end of the outage
    up, total = tsdb.count_availability(tmp_path, target_id=1, metrics=("http_up",))
    assert (up, total) == (3, 8)  # the 20 unmeasured minutes add neither samples nor downtime


def test_dashboard_widget_and_pdf_table_show_the_history_duration(tmp_path):
    values = [(1, 1), (2, 0), (3, 0), (4, 0), (5, 1), (20, 1), (21, 0), (22, 1)]
    put(tmp_path, "http_up", values)
    found = periods(tmp_path)
    summary = metrics._summarize_periods(found, now=at(600))
    events = pdf_report._extract_downtime_events(
        [{"ts": at(m).isoformat(), "value": v} for m, v in values], 60, at(600)
    )
    assert summary["downtime_seconds"] == sum(p.duration_seconds for p in found) == 240
    assert summary["incidents"] == len(found) == len(events) == 2
    assert sum((e.duration_minutes or 0) for e in events) == 4
    kpi = pdf_report._compute_kpis(
        pdf_report.ReportData(
            target={"interval_seconds": 60},
            from_date=at(0),
            to_date=at(600),
            http_data=[],
            uptime_data=[{"ts": at(m).isoformat(), "value": v} for m, v in values],
            ping_data=[],
            tcp_data=[],
            cert_data=[],
            status_code_data=[],
        )
    )
    assert kpi.downtime_minutes == 4


def test_measured_outages_rule_edge_cases():
    base = at(0)
    pts = [(base, 1), (base + timedelta(minutes=1), None), (base + timedelta(minutes=2), 0)]
    # UNKNOWN is neither down nor up, and a lone DOWN is one measured interval
    out = measured_outages(pts, interval_seconds=60, now=base + timedelta(minutes=2, seconds=30))
    assert [(o.start, o.end) for o in out] == [(base + timedelta(minutes=2), None)]
    assert measured_outages([], interval_seconds=60) == []


# ── TSDB metric model for the ternary UDP state ──────────────────────────────


def test_udp_metrics_are_binary_or_flag_and_protocol_specific():
    assert tsdb.is_binary_metric("udp_up") and tsdb.is_binary_metric("udp_up_53") and tsdb.is_binary_metric("tcp_up_53")
    assert not tsdb.is_binary_metric("udp_up_0") and not tsdb.is_binary_metric("udp_up_65536")
    assert tsdb.is_flag_metric("udp_unknown_53") and not tsdb.is_flag_metric("udp_up_53")
    assert tsdb._series_path(Path("/x"), 1, "tcp_up_53") != tsdb._series_path(Path("/x"), 1, "udp_up_53")


def test_rollup_keeps_unknown_visible_and_down_dominant(tmp_path):
    for minute, up, unknown in [(0, 1, 0), (1, 0, 0), (2, 1, 1)]:
        tsdb.append_point(tmp_path, target_id=1, metric="udp_up_53", value=up, retention_days=365, at=at(minute))
        tsdb.append_point(
            tmp_path, target_id=1, metric="udp_unknown_53", value=unknown, retention_days=365, at=at(minute)
        )
    for metric, expected in (("udp_up_53", 0), ("udp_unknown_53", 1)):
        points = tsdb.query(tmp_path, target_id=1, metric=metric, limit=10)
        [bucket] = tsdb._resample_points(points, metric=metric, since=at(0), until=at(10), max_points=1)
        assert bucket.value == expected  # min() for 0/1 state, max() for the UNKNOWN flag


def test_udp_only_target_has_an_uptime_metric_and_availability_series(tmp_path):
    from app.utils.uptime_metric import enabled_availability_metrics, select_uptime_metric

    row = {"enable_udp_scan": 1}
    assert select_uptime_metric(row) == "udp_up" and enabled_availability_metrics(row) == ("udp_up",)
    put(tmp_path, "udp_up_53", [(1, 1), (2, 0), (3, 1)])
    series = tsdb.query_availability_series(
        tmp_path,
        target_id=1,
        since=at(0),
        until=at(10),
        enabled_services={"UDP"},
        udp_ports=[53],
        check_interval_seconds=60,
    )
    assert list(series) == ["UDP:53"] and [p["up"] for p in series["UDP:53"]] == [1, 0, 1]


def test_dashboard_widget_groups_the_ports_of_one_service_but_not_other_services():
    row = {
        "enable_http_check": 1,
        "enable_tcp_connect": 1,
        "tcp_ports": "25,587",
        "enable_udp_scan": 1,
        "udp_ports": "53",
    }
    groups = metrics._service_groups(row)
    assert list(groups) == ["http_up", "tcp_up", "udp_up"]
    assert [label for _m, label in groups["tcp_up"]] == ["TCP:25", "TCP:587"]


def _resampled(start_min: float, hours: float, step_s: int, down: tuple[float, float]) -> list[dict]:
    """A report series thinned to ``step_s`` spacing, DOWN between the two minute offsets."""
    out = []
    t = at(start_min)
    end = at(start_min + hours * 60)
    while t <= end:
        minute = (t - T0).total_seconds() / 60
        out.append({"ts": t.isoformat(), "value": 0 if down[0] <= minute < down[1] else 1})
        t += timedelta(seconds=step_s)
    return out


def test_resampled_pdf_series_gives_one_outage_of_the_true_duration():
    # Every spacing above 3 x the 60 s check interval must not turn the neighbouring points into gaps.
    for step in (270, 300, 3600):
        hours = 40 if step == 3600 else 12
        data = _resampled(0, hours, step, (60, 240) if step != 3600 else (600, 840))
        events = pdf_report._extract_downtime_events(data, 60, now=at(hours * 60 + 1))
        assert len(events) == 1, (step, events)
        expected = 180 if step != 3600 else 240
        assert abs(events[0].duration_minutes - expected) <= step / 60, (step, events)
        assert abs(pdf_report._downtime_minutes(data, 60) - expected) <= step / 60


def test_resampled_pdf_series_still_excludes_a_real_gap():
    data = [
        p
        for p in _resampled(0, 12, 300, (60, 400))
        if not 120 < (datetime.fromisoformat(p["ts"]) - T0).total_seconds() / 60 < 400
    ]
    events = pdf_report._extract_downtime_events(data, 60, now=at(13 * 60))
    assert sum(e.duration_minutes or 0 for e in events) < 120  # the 280 unmeasured minutes are not downtime


def test_udp_series_with_scan_spaced_points_is_one_continuous_outage(tmp_path):
    # UDP scans run at most every 300 s; their points are not a gap against a 60 s target interval.
    put(tmp_path, "udp_up_53", [(0, 1), (5, 0), (10, 0), (15, 0), (20, 1)])
    [period] = periods(tmp_path, [("udp_up_53", "UDP:53")])
    assert (period.start, period.duration_seconds) == (at(5), 15 * 60)


def test_udp_gap_beyond_three_udp_intervals_is_not_downtime(tmp_path):
    put(tmp_path, "udp_up_53", [(0, 1), (5, 0), (60, 1)])
    found = periods(tmp_path, [("udp_up_53", "UDP:53")])
    assert sum(p.duration_seconds or 0 for p in found) < 15 * 60
