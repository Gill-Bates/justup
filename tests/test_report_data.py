#!/usr/bin/env python3
#
# tests/test_report_data.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Report data collection of the synchronous PDF endpoint and the background PDF worker.

All contracts run against both collectors (parity): priority metric, port range and point cap
come from app/utils/report_metrics.py and app/utils/uptime_metric.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import reports
from app.api.auth import get_current_user
from app.db import sqlite as sqlite_db
from app.db import tsdb as tsdb_db
from app.services import pdf_report
from app.services.pdf_worker import PDFJobRequest, PDFWorker
from app.utils import crypto
from app.utils.availability_series import AvailabilitySeries
from app.utils.deps import get_conn
from app.utils.metric_limits import choose_points_limit
from app.utils.rate_limit import RateLimitExceededError, rate_limit_exceeded_handler, reset_all
from app.utils.report_metrics import fetch_downtime_periods
from tests import _support  # noqa: F401 - imported for its sys.path side effect

FROM = datetime(2026, 1, 1, tzinfo=UTC)
TO = FROM + timedelta(days=1)


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    reset_all()


def _points(count: int, value: float = 1.0) -> list[SimpleNamespace]:
    return [SimpleNamespace(ts=FROM + timedelta(seconds=i), value=value) for i in range(count)]


class _FakeTsdb:
    """Stands in for the TSDB: serves fixed series and records which metrics were queried."""

    def __init__(self, series: dict[str, list[SimpleNamespace]]) -> None:
        self.series = series
        self.queried: list[str] = []
        self.availability_metrics: tuple[str, ...] = ()

    def query_auto_resolution(self, _tsdb_dir, *, metric, **_kwargs):
        self.queried.append(metric)
        return self.series.get(metric, [])

    def query_availability_runs(self, _tsdb_dir, *, metrics, interval_seconds, **_kwargs):
        """One sample per timestamp across the requested series; down if any of them is down."""
        self.availability_metrics = tuple(metrics)
        samples: dict[datetime, int] = {}
        for metric in metrics:
            for point in self.series.get(metric, []):
                samples[point.ts] = min(samples.get(point.ts, 1), int(point.value))
        points = [{"ts": ts.isoformat(), "value": value} for ts, value in sorted(samples.items())]
        return AvailabilitySeries.from_points(points, interval_seconds)

    def query_display_series(self, _tsdb_dir, **_kwargs):
        return {"points": [], "status_counts": {}}


@pytest.fixture
def fake_tsdb(monkeypatch: pytest.MonkeyPatch):
    def install(series: dict[str, list[SimpleNamespace]]) -> _FakeTsdb:
        fake = _FakeTsdb(series)
        monkeypatch.setattr(tsdb_db, "query_auto_resolution", fake.query_auto_resolution)
        monkeypatch.setattr(tsdb_db, "query_availability_runs", fake.query_availability_runs)
        monkeypatch.setattr(tsdb_db, "query_display_series", fake.query_display_series)
        return fake

    return install


def _create_target(db_path: Path, **flags) -> dict:
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.init_schema(conn)
        target_id = sqlite_db.create_target(
            conn,
            {"name": "t", "host": "t.example.com", "interval_seconds": 60, "retention_days": 30, **flags},
        )
        return dict(sqlite_db.get_target(conn, target_id))
    finally:
        sqlite_db.close_connection(conn)


def _collect_via_endpoint(tmp_path: Path, monkeypatch, **flags) -> pdf_report.ReportData:
    db_path = tmp_path / "db" / "justup.db"
    target = _create_target(db_path, **flags)
    captured: list[pdf_report.ReportData] = []
    monkeypatch.setattr(reports, "generate_pdf_report", lambda data, _size: captured.append(data) or b"%PDF-1.4")

    conn = sqlite_db.connect(db_path)
    try:
        app = FastAPI()
        app.add_exception_handler(RateLimitExceededError, rate_limit_exceeded_handler)
        app.state.tsdb_dir = tmp_path
        app.include_router(reports.router)
        app.dependency_overrides[get_conn] = lambda: conn
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_admin": True}
        resp = TestClient(app).get(
            f"/reports/targets/{target['id']}/pdf", params={"since": FROM.isoformat(), "until": TO.isoformat()}
        )
        assert resp.status_code == 200
    finally:
        sqlite_db.close_connection(conn)
    return captured[0]


def _collect_via_worker(tmp_path: Path, monkeypatch, **flags) -> pdf_report.ReportData:
    db_path = tmp_path / "db" / "justup.db"
    target = _create_target(db_path, **flags)
    captured: list[pdf_report.ReportData] = []
    monkeypatch.setattr(pdf_report, "generate_pdf_report", lambda data, _size: captured.append(data) or b"%PDF-1.4")

    async def run() -> None:
        worker = PDFWorker(storage_dir=tmp_path / "pdf", cpu_workers=1, io_workers=1)
        stop = asyncio.Event()
        loop_task = asyncio.create_task(worker.run_loop(sqlite_db.connect, stop_event=stop))
        try:
            await worker.submit(
                PDFJobRequest(
                    job_id="job-1",
                    target_id=target["id"],
                    target_dict=target,
                    from_date=FROM,
                    to_date=TO,
                    page_size="a4",
                    db_path=db_path,
                    tsdb_dir=tmp_path,
                )
            )
            await asyncio.wait_for(worker.queue.join(), 30)
        finally:
            stop.set()
            await asyncio.wait_for(loop_task, 5)
            worker.shutdown()

    asyncio.run(run())
    return captured[0]


@pytest.fixture(params=[_collect_via_endpoint, _collect_via_worker], ids=["endpoint", "worker"])
def collect(request):
    return request.param


def test_uptime_combines_every_enabled_check_even_when_one_has_no_data(collect, fake_tsdb, tmp_path, monkeypatch):
    fake = fake_tsdb({"ping_up": _points(5), "ping_ms": _points(5, 12.0)})
    data = collect(tmp_path, monkeypatch, enable_http_check=1, enable_ping=1)
    assert fake.availability_metrics == ("http_up", "ping_up")
    assert len(data.uptime_data) == 5  # a check without points does not count against the cycle
    assert len(data.ping_data) == 5


def test_uptime_uses_only_the_enabled_checks_when_http_is_off(collect, fake_tsdb, tmp_path, monkeypatch):
    fake = fake_tsdb({"ping_up": _points(5)})
    data = collect(tmp_path, monkeypatch, enable_http_check=0, enable_ping=1)
    assert fake.availability_metrics == ("ping_up",)
    assert len(data.uptime_data) == 5


def test_out_of_range_tcp_ports_are_dropped(collect, fake_tsdb, tmp_path, monkeypatch):
    fake = fake_tsdb({"tcp_ms_80": _points(3), "tcp_ms_443": _points(3), "tcp_ms_99999": _points(3)})
    data = collect(tmp_path, monkeypatch, enable_tcp_connect=1, tcp_ports="80,99999,443")
    assert set(data.tcp_port_data) == {80, 443}
    assert "tcp_ms_99999" not in fake.queried


def test_oversized_series_are_capped_to_the_profile_limit(collect, fake_tsdb, tmp_path, monkeypatch):
    limit = choose_points_limit(metric="http_ms", since=FROM, until=TO, profile="pdf")
    fake_tsdb({"http_ms": _points(limit + 500)})
    data = collect(tmp_path, monkeypatch, enable_http_check=1)
    assert len(data.http_data) == limit


def test_disabled_checks_are_not_queried(collect, fake_tsdb, tmp_path, monkeypatch):
    fake = fake_tsdb({"http_ms": _points(3), "ping_ms": _points(3), "cert_days_left": _points(3)})
    data = collect(tmp_path, monkeypatch, enable_http_check=0, enable_ping=0, enable_tcp_connect=0)
    assert (data.http_data, data.ping_data, data.tcp_data, data.cert_data) == ([], [], [], [])
    assert not {"http_ms", "ping_ms", "cert_days_left"} & set(fake.queried)


def test_certificate_series_is_collected_when_enabled(collect, fake_tsdb, tmp_path, monkeypatch):
    fake_tsdb({"cert_days_left": _points(4, 30.0)})
    data = collect(tmp_path, monkeypatch, enable_http_check=0, enable_cert_expiration=1)
    assert [p["value"] for p in data.cert_data] == [30.0] * 4


def test_status_counts_are_flagged_exact(collect, fake_tsdb, tmp_path, monkeypatch):
    fake_tsdb({"http_ms": _points(2), "http_up": _points(2)})
    data = collect(tmp_path, monkeypatch, enable_http_check=1)
    assert data.status_counts_exact is True


def _spy_downtime(monkeypatch, result):
    """Replace the exact downtime query; ``result`` is returned, or raised when it is an exception."""
    calls: list[dict] = []

    def query(_tsdb_dir, **kwargs):
        calls.append(kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tsdb_db, "query_downtime_periods", query)
    return calls


def test_exact_downtime_periods_reach_the_report_unchanged(collect, fake_tsdb, tmp_path, monkeypatch):
    fake_tsdb({})
    sentinel = [SimpleNamespace(marker="exact")]
    calls = _spy_downtime(monkeypatch, sentinel)
    data = collect(tmp_path, monkeypatch, enable_http_check=1)
    assert data.downtime_periods is sentinel
    assert len(calls) == 1 and calls[0]["limit"] == 2**63 - 1 and calls[0]["check_interval_seconds"] == 60


@pytest.mark.parametrize(
    ("target", "expected"), [({"interval_seconds": 300}, 300), ({"interval_seconds": 0}, 60), ({}, 60)]
)
def test_downtime_query_uses_the_target_interval_with_60_as_default(monkeypatch, tmp_path, target, expected):
    calls = _spy_downtime(monkeypatch, [])
    assert fetch_downtime_periods(tmp_path, 1, target, FROM, TO) == []
    assert calls[0]["check_interval_seconds"] == expected


def test_failing_downtime_query_falls_back_to_sampled_events(collect, fake_tsdb, tmp_path, monkeypatch):
    fake_tsdb({})
    _spy_downtime(monkeypatch, OSError("corrupt series file"))
    data = collect(tmp_path, monkeypatch, enable_http_check=1)
    assert data.downtime_periods is None


def test_both_report_paths_configure_the_downtime_query_alike(fake_tsdb, tmp_path, monkeypatch):
    fake_tsdb({})
    calls = _spy_downtime(monkeypatch, [])
    _collect_via_endpoint(tmp_path / "a", monkeypatch, enable_http_check=1, enable_ping=1)
    _collect_via_worker(tmp_path / "b", monkeypatch, enable_http_check=1, enable_ping=1)
    endpoint, worker = calls
    assert endpoint.pop("series") == worker.pop("series") != []
    assert endpoint == worker


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Zürich Core", "20260105-Zurich_Core-report.pdf"),
        ("a.b.example.com", "20260105-a_b_example_com-report.pdf"),
        ("", "20260105-report-report.pdf"),
        ("日本", "20260105-report-report.pdf"),
        (None, "20260105-report-report.pdf"),
        ("x" * 300, "20260105-" + "x" * 100 + "-report.pdf"),
    ],
)
def test_report_filename_is_ascii_bounded_and_deterministic(name, expected):
    from datetime import UTC, datetime

    from app.utils.report_metrics import report_filename

    assert report_filename(name, datetime(2026, 1, 5, 12, tzinfo=UTC)) == expected


_PARITY_TARGETS = {
    "http_only": {"enable_http_check": 1},
    "ping_multi_tcp": {"enable_ping": 1, "enable_tcp_connect": 1, "tcp_ports": "80,443,8080"},
    "udp_only": {"enable_udp_scan": 1, "udp_ports": "53"},
    "cert": {"enable_http_check": 1, "enable_cert_expiration": 1},
}


@pytest.mark.parametrize("flags", _PARITY_TARGETS.values(), ids=_PARITY_TARGETS.keys())
def test_both_report_paths_pass_identical_report_inputs(flags, fake_tsdb, tmp_path, monkeypatch):
    metrics = ("http_ms", "http_up", "ping_ms", "ping_up", "tcp_ms", "tcp_up", "udp_up", "cert_days_left")
    series = {m: _points(3, 7.0) for m in (*metrics, "tcp_ms_80", "tcp_ms_443", "tcp_ms_8080")}
    fake_tsdb(series)

    def display(_dir, *, metric, **_kwargs):
        counts = {"200": 3, "Error": 1} if metric == "http_status" else {}
        return {"points": [{"metric": metric}], "status_counts": counts}

    monkeypatch.setattr(tsdb_db, "query_display_series", display)
    endpoint = _collect_via_endpoint(tmp_path / "a", monkeypatch, **flags)
    worker = _collect_via_worker(tmp_path / "b", monkeypatch, **flags)
    ep, wk = dict(vars(endpoint)), dict(vars(worker))
    # The endpoint passes None for "no per-port data", the worker an empty dict; both mean the same.
    assert (ep.pop("tcp_port_data") or {}) == (wk.pop("tcp_port_data") or {})
    assert ep.pop("target")["id"] == wk.pop("target")["id"]
    assert ep == wk
    assert endpoint.status_counts_exact is True
