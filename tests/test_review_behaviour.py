#!/usr/bin/env python3
#
# tests/test_review_behaviour.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001
"""Scheduler cert alerts, TSDB binary metrics, CSRF origin matching, async SSRF host check, cert parsing."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import ssl
import threading
from contextlib import contextmanager
from unittest import mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import tsdb
from app.middleware.csrf import CSRFMiddleware
from app.monitor import checks, scheduler
from app.monitor.scheduler import MonitorScheduler, TargetRuntime
from app.utils.metric_limits import choose_display_bucket_seconds
from tests import _support  # noqa: F401 - imported for its sys.path side effect

# ---------------------------------------------------------------- cert alerts


@pytest.fixture
def sched():
    s = MonitorScheduler.__new__(MonitorScheduler)
    s._rt = {1: TargetRuntime(next_due=dt.datetime.now(dt.UTC))}

    @contextmanager
    def _conn():
        yield mock.MagicMock()

    s._db_conn = _conn
    s._dispatch_alert = mock.AsyncMock()
    return s


def _call(s, days):
    asyncio.run(s._maybe_cert_alert(target_id=1, target_name="t", days_left=days, cert_host="h"))


def test_cert_alert_resolves_after_renewal_and_realerts_on_new_expiry(sched):
    with (
        mock.patch.object(scheduler.sqlite_db, "create_alert") as create,
        mock.patch.object(scheduler.sqlite_db, "resolve_alert") as resolve,
    ):
        _call(sched, 6)
        _call(sched, 5)  # no duplicate
        assert create.call_count == 1
        rt = sched._rt[1]
        assert rt.cert_alert_sent and rt.cert_alert_id
        _call(sched, 8)  # renewed
        assert resolve.call_count == 1
        assert not rt.cert_alert_sent and rt.cert_alert_id is None
        _call(sched, 7)  # new expiry window -> new alert
        assert create.call_count == 2


def test_cert_alert_not_resolved_without_open_alert(sched):
    with mock.patch.object(scheduler.sqlite_db, "resolve_alert") as resolve:
        _call(sched, 30)
        resolve.assert_not_called()


# ------------------------------------------------------------------- tsdb


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tcp_up", True),
        ("tcp_up_80", True),
        ("tcp_up_443", True),
        ("tcp_up_65535", True),
        ("tcp_up_0", False),
        ("tcp_up_65536", False),
        ("tcp_up_http", False),
        ("tcp_ms_443", False),
        ("http_status", False),
        ("ping_up", True),
    ],
)
def test_binary_metric_classification(name, expected):
    assert tsdb.is_binary_metric(name) is expected


def test_downsample_binary_port_bucket_is_zero_but_latency_stays_median(tmp_path):
    now = dt.datetime.now(dt.UTC)
    base = (now - dt.timedelta(days=40)).replace(minute=0, second=0, microsecond=0)
    for i, (up, ms) in enumerate([(0, 10), (1, 20), (1, 90)]):
        at = base + dt.timedelta(seconds=i * 60)
        for metric, value in (("tcp_up_443", up), ("tcp_ms_443", ms)):
            tsdb.append_point(tmp_path, target_id=1, metric=metric, value=value, retention_days=365, at=at)
    for metric in ("tcp_up_443", "tcp_ms_443"):
        n = tsdb.downsample_series(
            tmp_path,
            target_id=1,
            metric=metric,
            source_resolution="raw",
            target_resolution="5m_median",
            since=now - dt.timedelta(days=60),
            until=now - dt.timedelta(days=20),
        )
        assert n == 1
    up = tsdb.query(tmp_path, target_id=1, metric="tcp_up_443@5m_median")
    ms = tsdb.query(tmp_path, target_id=1, metric="tcp_ms_443@5m_median")
    assert [p.value for p in up] == [0]
    assert [p.value for p in ms] == [20]


def test_resample_uses_minimum_for_port_availability_only():
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    end = base + dt.timedelta(seconds=30)

    def run(metric, values):
        pts = [tsdb.MetricPoint(ts=base + dt.timedelta(seconds=i * 10), value=v) for i, v in enumerate(values)]
        out = tsdb._resample_points(pts, metric=metric, since=base, until=end, max_points=1)
        return [p.value for p in out]

    assert 0 in run("tcp_up_443", [0, 1, 1])
    assert 0 not in run("tcp_ms_443", [5, 1, 1])


@pytest.mark.parametrize(
    ("days", "expected"),
    [(1 / 24, 10), (0.25, 60), (1, 300), (7, 1800), (30, 7200), (90, 21600), (365, 86400)],
)
def test_display_resolution_tracks_window_and_plot_width(days, expected):
    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=days)
    assert choose_display_bucket_seconds(since=start, until=end, plot_width=900) == expected
    assert choose_display_bucket_seconds(since=start, until=end, plot_width=320) >= expected


def test_display_buckets_keep_peak_and_single_http_error(tmp_path):
    end = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = end - dt.timedelta(hours=1)
    stamps = [start + dt.timedelta(minutes=10, seconds=i) for i in range(5)]
    for i, stamp in enumerate(stamps):
        tsdb.append_point(
            tmp_path, target_id=1, metric="http_ms", value=3000 if i == 2 else 100 + i, retention_days=365, at=stamp
        )
        tsdb.append_point(
            tmp_path, target_id=1, metric="http_status", value=503 if i == 2 else 200, retention_days=365, at=stamp
        )
    latency = tsdb.query_display_series(
        tmp_path, target_id=1, metric="http_ms", since=start, until=end, bucket_seconds=60
    )
    status = tsdb.query_display_series(
        tmp_path, target_id=1, metric="http_status", since=start, until=end, bucket_seconds=60
    )
    assert latency["points"][0]["value"] == 103
    assert latency["points"][0]["min"] == 100
    assert latency["points"][0]["max"] == 3000
    assert latency["points"][0]["count"] == 5
    assert status["points"][0]["value"] == 503
    assert status["points"][0]["code_counts"] == {"200": 4, "503": 1}
    assert status["status_counts"] == {"200": 4, "503": 1}


def test_display_rollup_bounds_are_unknown_and_status_medians_excluded(tmp_path):
    end = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = end - dt.timedelta(days=45)
    old = end - dt.timedelta(days=40)
    tsdb.append_point(tmp_path, target_id=1, metric="http_ms@5m_median", value=120, retention_days=365, at=old)
    tsdb.append_point(tmp_path, target_id=1, metric="http_status@5m_median", value=351.5, retention_days=365, at=old)
    tsdb.append_point(
        tmp_path, target_id=1, metric="http_status", value=503, retention_days=365, at=end - dt.timedelta(minutes=1)
    )
    latency = tsdb.query_display_series(
        tmp_path, target_id=1, metric="http_ms", since=start, until=end, bucket_seconds=3600
    )
    status = tsdb.query_display_series(
        tmp_path, target_id=1, metric="http_status", since=start, until=end, bucket_seconds=3600
    )
    assert latency["points"][0]["rollup"] is True
    assert latency["points"][0]["min"] is None and latency["points"][0]["max"] is None
    assert status["status_counts"] == {"503": 1}
    assert status["raw_status_only"] is True


def test_availability_compaction_keeps_down_transition_and_gap(tmp_path):
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = now - dt.timedelta(hours=20)
    path = tsdb._series_path(tmp_path, 1, "http_up")
    path.parent.mkdir(parents=True)
    with path.open("w", encoding="utf-8") as stream:
        for i in range(50_050):
            stamp = start + dt.timedelta(seconds=i)
            value = 0 if i == 40_000 else 1
            stream.write(json.dumps({"ts": stamp.isoformat(), "value": value}) + "\n")
        later = start + dt.timedelta(hours=19)
        stream.write(json.dumps({"ts": later.isoformat(), "value": 1}) + "\n")
    series = tsdb.query_availability_series(
        tmp_path, target_id=1, since=start, until=now, limit=50, enabled_services={"HTTP"}, check_interval_seconds=1
    )["HTTP"]
    assert [p["up"] for p in series] == [1, 0, 1, 1, None, 1]
    assert len(series) < 10


# ------------------------------------------------------------------- csrf


def _csrf_client(base="http://testserver"):
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/ui/x")
    def x():
        return {"ok": True}

    return TestClient(app, base_url=base)


def _post(client, origin, host=None):
    headers = {"Origin": origin, "X-CSRF-Token": "tok"} if origin else {"X-CSRF-Token": "tok"}
    if host:
        headers["Host"] = host
    return client.post("/ui/x", headers=headers, cookies={"csrf_token": "tok"})


@pytest.mark.parametrize(
    ("origin", "host", "status"),
    [
        (None, "example.com", 200),
        ("http://example.com", "example.com", 200),
        ("https://example.com", "example.com", 200),
        ("https://example.com:443", "example.com", 200),
        ("http://example.com:8080", "example.com:8080", 200),
        ("http://example.com:9999", "example.com:8080", 403),
        ("http://example.com", "example.com:8080", 403),
        ("http://evil.com", "example.com", 403),
        ("http://example.com.evil.com", "example.com", 403),
        ("http://[::1]:8000", "[::1]:8000", 200),
        ("http://[::1]:8001", "[::1]:8000", 403),
        ("null", "example.com", 403),
        ("garbage", "example.com", 403),
        ("ftp://example.com", "example.com", 403),
    ],
)
def test_origin_matching(origin, host, status):
    assert _post(_csrf_client(), origin, host).status_code == status


def test_missing_csrf_token_is_still_rejected():
    r = _csrf_client().post("/ui/x", headers={"Origin": "http://testserver"})
    assert r.status_code == 403


def test_http_origin_rejected_on_https_request():
    assert _post(_csrf_client("https://testserver"), "http://testserver", "testserver").status_code == 403


# ------------------------------------------------------------------ checks


def test_host_block_check_runs_off_event_loop():
    main_thread = threading.get_ident()
    seen = []

    def slow(host):
        seen.append(threading.get_ident())
        return False

    async def run():
        with mock.patch.object(checks, "_is_host_blocked", slow):
            return await checks._is_host_blocked_async("example.com")

    assert asyncio.run(run()) is False
    assert seen and seen[0] != main_thread


@pytest.mark.parametrize(
    ("ip", "blocked"),
    [
        ("93.184.216.34", False),
        ("10.0.0.1", False),
        ("127.0.0.1", True),
        ("169.254.169.254", True),
        ("::1", True),
        ("2606:2800:220:1::1", False),
    ],
)
def test_async_host_block_policy_for_resolved_ips(ip, blocked):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with mock.patch("socket.getaddrinfo", return_value=[(fam, 1, 6, "", (ip, 0))]):
        assert asyncio.run(checks._is_host_blocked_async("name.example")) is blocked


def test_failing_resolver_fails_closed():
    with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
        assert asyncio.run(checks._is_host_blocked_async("nx.example")) is True


def _make_cert(tmp_path, *, days):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=400))
        .not_valid_after(now + dt.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    c, k = tmp_path / f"c{days}.pem", tmp_path / f"k{days}.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return c, k


@contextmanager
def _tls_server(certfile, keyfile):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.2)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (TimeoutError, OSError):
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        t.join()
        srv.close()


def test_cert_expiration_self_signed_with_ignore_errors(tmp_path):
    c, k = _make_cert(tmp_path, days=30)
    with _tls_server(c, k) as port:
        r = asyncio.run(checks.cert_expiration("127.0.0.1", port, 3, ignore_cert_errors=True, skip_ssrf_check=True))
    assert r.ok, r.detail
    assert 29 < r.value["remaining_days"] < 30.1


def test_cert_expiration_ip_host_without_ignore_errors_is_clean_failure(tmp_path):
    c, k = _make_cert(tmp_path, days=30)
    with _tls_server(c, k) as port:
        r = asyncio.run(checks.cert_expiration("127.0.0.1", port, 3, ignore_cert_errors=False, skip_ssrf_check=True))
    assert r.ok is False and r.detail


def test_cert_expiration_expired_cert_reports_negative_days(tmp_path):
    c, k = _make_cert(tmp_path, days=-5)
    with _tls_server(c, k) as port:
        r = asyncio.run(checks.cert_expiration("127.0.0.1", port, 3, ignore_cert_errors=True, skip_ssrf_check=True))
    assert r.ok and r.value["remaining_days"] < 0


def test_cert_expiration_tls_error_on_plain_tcp():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        r = asyncio.run(
            checks.cert_expiration("127.0.0.1", srv.getsockname()[1], 1, ignore_cert_errors=True, skip_ssrf_check=True)
        )
    finally:
        srv.close()
    assert not r.ok


def test_availability_limit_keeps_outages_and_caps_size(tmp_path):
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = now - dt.timedelta(hours=10)
    path = tsdb._series_path(tmp_path, 1, "http_up")
    path.parent.mkdir(parents=True)
    # Alternating up/down every 10 minutes yields far more transitions than the limit.
    with path.open("w", encoding="utf-8") as stream:
        for i in range(60):
            stamp = start + dt.timedelta(minutes=10 * i)
            stream.write(json.dumps({"ts": stamp.isoformat(), "value": 0 if i % 4 == 3 else 1}) + "\n")
    kwargs = {"target_id": 1, "since": start, "until": now, "enabled_services": {"HTTP"}, "check_interval_seconds": 600}
    full = tsdb.query_availability_series(tmp_path, limit=1000, **kwargs)["HTTP"]
    limited = tsdb.query_availability_series(tmp_path, limit=20, **kwargs)["HTTP"]
    assert len(full) > 20
    assert len(limited) <= 20
    # Newest outages survive in full, each still followed by its recovery.
    downs = [p["ts"] for p in limited if p["up"] == 0]
    assert downs and downs[-1] == [p["ts"] for p in full if p["up"] == 0][-1]
    for i, p in enumerate(limited[:-1]):
        if p["up"] == 0:
            assert limited[i + 1]["up"] == 1
    assert limited[-1] == full[-1]


def test_limit_availability_points_keeps_all_outages_when_they_fit():
    points = []
    for i in range(30):
        points.append({"ts": f"t{2 * i}", "up": 1})
        points.append({"ts": f"t{2 * i + 1}", "up": 0})
    points.extend({"ts": f"u{i}", "up": 1} for i in range(1))
    out = tsdb._limit_availability_points(points, 1000)
    assert out == points
    capped = tsdb._limit_availability_points(points, 12)
    assert len(capped) <= 12
    assert capped[-1] == points[-1]
