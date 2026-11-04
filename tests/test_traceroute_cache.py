#!/usr/bin/env python3
#
# tests/test_traceroute_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, S106
"""The authenticated traceroute fragment reuses recent trace results."""

from __future__ import annotations

import re
import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import frontend as frontend_api
from app.db import sqlite as sqlite_db
from app.models.users import hash_token, new_token
from app.services import route_interpolator, traceroute_geo, traceroute_service
from app.utils import crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests.traceroute_helpers import patch_trace


def test_traceroute_fragment_reuses_authorized_recent_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_primary_key", None)

    from app.main import create_app

    app = create_app()
    conn = sqlite_db.connect(app.state.db_path)
    sqlite_db.init_schema(conn)
    user_id = sqlite_db.create_user(conn, username="traceroute-review", password_hash="stub", is_admin=True)
    token, expires_at, max_expires_at = new_token()
    sqlite_db.issue_token(
        conn, user_id=user_id, token_hash=hash_token(token), expires_at=expires_at, max_expires_at=max_expires_at
    )
    target_id = sqlite_db.create_target(
        conn,
        {"name": "Cache target", "host": "cache-target.example.com", "interval_seconds": 60, "retention_days": 30},
    )
    sqlite_db.close_connection(conn)

    with frontend_api._traceroute_fragment_cache_lock:
        frontend_api._traceroute_fragment_cache.clear()

    calls = []
    hops = [
        {
            "hop": hop,
            "ip": f"10.0.0.{hop}",
            "hostname": f"private-router-{hop}.example.net",
            "latency_ms": {"avg": float(hop)},
            "loss_pct": 0,
            "status": "ok",
        }
        for hop in range(1, 5)
    ] + [
        {
            "hop": 5,
            "ip": "198.51.100.1",
            "hostname": "router.example.net",
            "latency_ms": {"avg": 5.2},
            "loss_pct": 0,
            "status": "ok",
        },
        {
            "hop": 6,
            "ip": "203.0.113.2",
            "hostname": "cache-target.example.com",
            "latency_ms": {"avg": 6.4},
            "loss_pct": 0,
            "status": "ok",
        },
    ]
    geo_points = [
        {"ip": "198.51.100.1", "lat": 50.0, "lon": 8.0, "city": "Frankfurt", "country": "DE", "hop": 5},
        {
            "ip": "203.0.113.2",
            "lat": 52.0,
            "lon": 13.0,
            "city": "Berlin",
            "country": "DE",
            "hop": 6,
            "_destination": True,
        },
    ]
    patch_trace(monkeypatch, hops, resolved="203.0.113.2", calls=calls)
    monkeypatch.setattr(traceroute_service, "analyze_route", lambda *_args: {"destination_reached": True})
    monkeypatch.setattr(route_interpolator, "interpolate_route", lambda route, **_kwargs: route)
    monkeypatch.setattr(route_interpolator, "collapse_interpolated_hops", lambda route: route)
    monkeypatch.setattr(
        traceroute_geo,
        "geolocate_with_hostname_hint",
        lambda ip, _hint=None: next((point for point in geo_points if point["ip"] == ip), None),
    )
    monkeypatch.setattr(traceroute_geo, "route_hops_to_geo_points", lambda *_args, **_kwargs: geo_points)

    client = TestClient(app)
    client.get("/ui/login")
    client.cookies.set("token", token)
    url = f"/ui/fragments/traceroute/{target_id}"
    first = client.get(url, headers={"HX-Request": "true"})
    second = client.get(url, headers={"HX-Request": "true"})

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.headers["cache-control"] == "private, no-store"
    assert len(calls) == 1
    assert 'data-route-start-hop="1"' in first.text
    assert 'data-route-start-is-mapped="false"' in first.text
    assert 'data-route-destination-host="cache-target.example.com"' in first.text
    assert "Network route" in first.text
    assert "tr-step--first" in first.text
    assert 'data-location="unlocated"' in first.text
    assert "The map starts at hop 5 because the earlier hops have no location." in first.text
    assert "cache-target.example.com" in first.text

    conn = sqlite_db.connect(app.state.db_path)
    conn.execute("UPDATE targets SET host = ? WHERE id = ?", ("replacement.example.net", target_id))
    conn.commit()
    sqlite_db.close_connection(conn)
    changed_host = client.get(url, headers={"HX-Request": "true"})
    assert changed_host.status_code == 200
    assert len(calls) == 2

    client.cookies.delete("token")
    unauthorized = client.get(url, headers={"HX-Request": "true"})
    assert unauthorized.status_code == 200
    assert unauthorized.headers["hx-redirect"] == "/ui/login"
    assert len(calls) == 2

    key = (f"{frontend_api._TRACEROUTE_CACHE_VERSION}:{app.state.db_path}", target_id, "replacement.example.net")
    with frontend_api._traceroute_fragment_cache_lock:
        _expires_at, body, measured_at = frontend_api._traceroute_fragment_cache[key]
        frontend_api._traceroute_fragment_cache[key] = (time.monotonic() - 1, body, measured_at)
    client.cookies.set("token", token)
    expired = client.get(url, headers={"HX-Request": "true"})
    assert expired.status_code == 200
    assert len(calls) == 3

    client.close()


# ── Network diagnostics redesign: KPI derivation, HTML contract and explicit "Run again" ──


def _ok_hop(hop: int, ip: str, latency: float, hostname: str = "-", loss: float = 0.0) -> dict:
    return {
        "hop": hop,
        "ip": ip,
        "hostname": hostname,
        "latency_ms": {"avg": latency},
        "loss_pct": loss,
        "status": "ok",
    }


def _timeout_hop(hop: int) -> dict:
    return {"hop": hop, "ip": "*", "hostname": "-", "latency_ms": None, "loss_pct": 100.0, "status": "timeout"}


def _diagnostics_client(tmp_path: Path, monkeypatch, hops: list[dict], *, target_ip: str, geo_points: list[dict]):
    """Authenticated client whose trace/geo layer is stubbed; real interpolation and collapsing run."""
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_primary_key", None)
    from app.main import create_app
    from app.utils.rate_limit import reset_all

    reset_all()
    app = create_app()
    conn = sqlite_db.connect(app.state.db_path)
    sqlite_db.init_schema(conn)
    user_id = sqlite_db.create_user(conn, username="diag-user", password_hash="stub", is_admin=True)
    token, expires_at, max_expires_at = new_token()
    sqlite_db.issue_token(
        conn, user_id=user_id, token_hash=hash_token(token), expires_at=expires_at, max_expires_at=max_expires_at
    )
    target_id = sqlite_db.create_target(
        conn, {"name": "Diag", "host": "diag.example.com", "interval_seconds": 60, "retention_days": 30}
    )
    sqlite_db.close_connection(conn)
    with frontend_api._traceroute_fragment_cache_lock:
        frontend_api._traceroute_fragment_cache.clear()
    with frontend_api._traceroute_refresh_lock:
        frontend_api._traceroute_refresh_inflight.clear()
        frontend_api._traceroute_refresh_finished.clear()

    calls: list[tuple] = []
    patch_trace(monkeypatch, hops, resolved=target_ip, calls=calls)
    monkeypatch.setattr(
        traceroute_geo,
        "geolocate_with_hostname_hint",
        lambda ip, _hint=None: next((p for p in geo_points if p["ip"] == ip), None),
    )
    monkeypatch.setattr(traceroute_geo, "route_hops_to_geo_points", lambda *_args, **_kwargs: geo_points)

    client = TestClient(app)
    client.get("/ui/login")
    client.cookies.set("token", token)
    return client, f"/ui/fragments/traceroute/{target_id}", calls, app, target_id


_DEST_GEO = {"ip": "203.0.113.9", "lat": 50.1, "lon": 8.7, "city": "Frankfurt am Main", "country": "DE", "hop": 7}
_HX = {"HX-Request": "true"}


def test_logical_hop_count_expands_collapsed_rows() -> None:
    rows = [{"hop": 1}, {"_collapsed": True, "_raw_hops": [{"hop": 2}, {"hop": 3}, {"hop": 4}]}, {"hop": 5}]
    assert frontend_api._logical_hop_count(rows) == 5
    assert frontend_api._logical_hop_count([]) == 0


def test_destination_kpis_use_measured_values_and_never_default_to_zero() -> None:
    reached = [
        {"hop": 1, "ip": "10.0.0.1"},
        {
            "hop": 2,
            "ip": "203.0.113.9",
            "rtt_ms": 15.74,
            "city": "Frankfurt",
            "country": "DE",
        },
    ]
    kpis = frontend_api._destination_kpis(reached, "203.0.113.9", None)
    assert (kpis["hop_count"], kpis["latency_display"]) == (2, "15.7 ms")
    assert "loss_display" not in kpis and "loss_pct" not in kpis
    assert (kpis["destination_reached"], kpis["city"], kpis["country"]) == (True, "Frankfurt", "DE")

    lossy = [{"hop": 1, "ip": "203.0.113.9", "rtt_ms": 3.0}]
    assert frontend_api._destination_kpis(lossy, "203.0.113.9", None)["latency_display"] == "3.0 ms"

    # A synthetic destination was never measured: latency and loss stay unknown, not 0.
    synthetic = [
        {"hop": 1, "ip": "10.0.0.1", "rtt_ms": 1.0},
        {"hop": 2, "ip": "203.0.113.9", "rtt_ms": None, "_synthetic_destination": True},
    ]
    kpis = frontend_api._destination_kpis(synthetic, "203.0.113.9", {"city": "Berlin", "country": "DE"})
    assert kpis["latency_display"] is None
    assert kpis["destination_reached"] is False and kpis["city"] == "Berlin"

    # Without a destination hop or a loss value nothing is invented.
    unknown = frontend_api._destination_kpis([{"hop": 1, "ip": "10.0.0.1"}], None, None)
    assert unknown["latency_display"] is None and unknown["city"] is None
    single = frontend_api._destination_kpis([{"hop": 1, "ip": "203.0.113.9", "rtt_ms": 2.0}], "203.0.113.9", None)
    assert single["latency_display"] == "2.0 ms" and "loss_display" not in single


def test_fragment_counts_collapsed_hops_and_renders_panels(tmp_path: Path, monkeypatch) -> None:
    hops = [
        _ok_hop(1, "10.0.0.1", 1.0),
        _timeout_hop(2),
        _timeout_hop(3),
        _timeout_hop(4),
        _timeout_hop(5),
        _ok_hop(6, "198.51.100.1", 9.0, "edge.example.net"),
        _ok_hop(7, "203.0.113.9", 15.7, "<script>alert(1)</script>.example.net"),
    ]
    geo = [
        {"ip": "198.51.100.1", "lat": 51.5, "lon": 7.4, "city": "Dortmund", "country": "DE", "hop": 6},
        {**_DEST_GEO, "_destination": True},
    ]
    client, url, calls, _app, _tid = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=geo
    )
    response = client.get(url, headers=_HX)
    text = response.text

    assert response.status_code == 200 and len(calls) == 1
    # 7 logical hops even though the collapsed ICMP-blocked rows shrink the table.
    assert 'class="tr-kpi-value">7</dd>' in text
    assert "Traceroute hops <span" in text and "(7)" in text
    assert "No reply" in text and "ICMP blocked" not in text and "tr-hop-row--blocked" in text
    # Hops 2-5 are ICMP-blocked and consecutive without map location: one expandable group of 4.
    assert 'data-measurement="timeout"' in text and 'data-measurement="interpolated"' not in text
    assert "Estimated" not in text and "estimated" not in text
    assert "4 hops without reply" in text  # row subtitle and timeline group title, counted from hop numbers
    assert text.count("data-tr-group") == 1 and 'aria-expanded="false"' in text
    assert 'aria-controls="tr-group-2-5"' in text and 'id="tr-group-2-5"' in text
    assert text.count("data-tr-hop") == 7  # every hop stays reachable, grouped ones inside the group
    # Visible text names each step; an aria-label would trip axe's label-content-name-mismatch.
    assert not re.search(r"<button[^>]*tr-step-btn[^>]*aria-label", text)
    assert text.count(">No reply<") >= 4 and text.count("ju-status-badge--warning") >= 1
    assert (
        "15.7 ms" in text
        and "%" not in text.split("hop-table", 1)[1].split("</table>")[0]
        and "Frankfurt am Main, DE" in text
    )
    assert "Geolocated route for 2 of 7 hops" in text
    for header in ("IP address", "Hostname", "Location", "RTT", "Status"):
        assert f">{header}</th>" in text
    # Hostnames are escaped; never emitted as markup.
    assert "<script>alert(1)" not in text and "&lt;script&gt;alert(1)" in text
    client.close()


def test_fragment_shows_unknown_for_unreached_destination(tmp_path: Path, monkeypatch) -> None:
    hops = [_ok_hop(1, "10.0.0.1", 1.0), _ok_hop(2, "198.51.100.1", 9.0), _timeout_hop(3)]
    geo = [{"ip": "198.51.100.1", "lat": 51.5, "lon": 7.4, "city": "Dortmund", "country": "DE", "hop": 2}]
    client, url, _calls, _app, _tid = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=geo
    )
    text = client.get(url, headers=_HX).text

    assert "not measured" in text
    assert text.count('data-known="false"') == 1  # the RTT stays unknown, never 0
    assert text.count('class="tr-kpi-value">—</dd>') == 1
    assert "No reply" in text
    client.close()


def test_rendered_fragment_names_no_removed_tool(tmp_path: Path, monkeypatch) -> None:
    hops = [_ok_hop(1, "10.0.0.1", 1.0), _timeout_hop(2), _ok_hop(3, "203.0.113.9", 5.0)]
    geo = [{**_DEST_GEO, "hop": 3, "_destination": True}]
    client, url, _calls, _app, _tid = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=geo
    )
    text = client.get(url, headers=_HX).text
    assert "No reply" in text
    assert not re.search(r"\bmtr\b", text, re.IGNORECASE)
    client.close()


def test_run_again_forces_new_measurement_with_auth_and_parallel_guard(tmp_path: Path, monkeypatch) -> None:
    hops = [_ok_hop(1, "10.0.0.1", 1.0), _ok_hop(2, "203.0.113.9", 5.0)]
    geo = [{**_DEST_GEO, "hop": 2, "_destination": True}]
    client, url, calls, app, target_id = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=geo
    )

    client.get(url, headers=_HX)
    client.get(url, headers=_HX)
    assert len(calls) == 1  # normal tab loads still use the five-minute cache

    # Without the HTMX header the refresh flag is ignored (no cross-site trigger of a trace).
    assert client.get(f"{url}?refresh=1").status_code == 200
    assert len(calls) == 1

    forced = client.get(f"{url}?refresh=1", headers=_HX)
    assert forced.status_code == 200 and len(calls) == 2
    assert client.get(url, headers=_HX).content == forced.content  # cache now holds the new result
    assert len(calls) == 2

    # Immediate repeat is rejected (cooldown) and does not start another trace.
    again = client.get(f"{url}?refresh=1", headers=_HX)
    assert again.status_code == 429 and "retry-after" in again.headers and len(calls) == 2

    # After the cooldown another forced run is allowed.
    key = (f"{frontend_api._TRACEROUTE_CACHE_VERSION}:{app.state.db_path}", target_id, "diag.example.com")
    with frontend_api._traceroute_refresh_lock:
        frontend_api._traceroute_refresh_finished[key] = time.monotonic() - 60
    assert client.get(f"{url}?refresh=1", headers=_HX).status_code == 200
    assert len(calls) == 3

    # A run that is still in flight blocks parallel forced runs.
    with frontend_api._traceroute_refresh_lock:
        frontend_api._traceroute_refresh_finished.clear()
        frontend_api._traceroute_refresh_inflight.add(key)
    assert client.get(f"{url}?refresh=1", headers=_HX).status_code == 429
    assert len(calls) == 3
    with frontend_api._traceroute_refresh_lock:
        frontend_api._traceroute_refresh_inflight.clear()

    # Unauthenticated forced refresh is redirected to the login and measures nothing.
    client.cookies.delete("token")
    denied = client.get(f"{url}?refresh=1", headers=_HX)
    assert denied.headers["hx-redirect"] == "/ui/login"
    assert len(calls) == 3
    client.close()


def test_refresh_is_rejected_while_a_cache_miss_measurement_is_in_flight(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    hops = [_ok_hop(1, "10.0.0.1", 1.0), _ok_hop(2, "203.0.113.9", 5.0)]
    geo = [{**_DEST_GEO, "hop": 2, "_destination": True}]
    client, url, calls, app, target_id = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=geo
    )
    key = (f"{frontend_api._TRACEROUTE_CACHE_VERSION}:{app.state.db_path}", target_id, "diag.example.com")
    frontend_api._traceroute_inflight[key] = SimpleNamespace(done=lambda: False)
    try:
        blocked = client.get(f"{url}?refresh=1", headers=_HX)
    finally:
        frontend_api._traceroute_inflight.pop(key, None)
    assert blocked.status_code == 429 and "retry-after" in blocked.headers
    assert calls == []  # no second measurement raced the running one
    client.close()


# ── 24 h browser cache contract: original measurement time, bypass and scope keys ──
def _measured_at(response) -> int:
    return int(response.headers["x-measured-at"])


def _allow_refresh(app, target_id: int, host: str = "diag.example.com") -> None:
    with frontend_api._traceroute_refresh_lock:
        frontend_api._traceroute_refresh_finished[
            (f"{frontend_api._TRACEROUTE_CACHE_VERSION}:{app.state.db_path}", target_id, host)
        ] = time.monotonic() - 60


def test_response_carries_the_original_measurement_time(tmp_path: Path, monkeypatch) -> None:
    hops = [_ok_hop(1, "10.0.0.1", 1.0), _ok_hop(2, "203.0.113.9", 5.0)]
    geo = [{**_DEST_GEO, "hop": 2, "_destination": True}]
    client, url, calls, _app, _tid = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=geo
    )
    before = time.time_ns() // 1_000_000
    first = client.get(url, headers=_HX)
    after = time.time_ns() // 1_000_000
    assert before <= _measured_at(first) <= after
    time.sleep(0.02)
    second = client.get(url, headers=_HX)
    assert len(calls) == 1  # served from the five-minute cache
    assert _measured_at(second) == _measured_at(first)  # a hit reports when it was measured, not now
    assert second.headers["cache-control"] == first.headers["cache-control"] == "private, no-store"
    client.close()


def test_refresh_bypasses_the_server_cache_and_reports_the_new_time(tmp_path: Path, monkeypatch) -> None:
    hops = [_ok_hop(1, "10.0.0.1", 1.0), _ok_hop(2, "203.0.113.9", 5.0)]
    geo = [{**_DEST_GEO, "hop": 2, "_destination": True}]
    client, url, calls, app, target_id = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=geo
    )
    first = client.get(url, headers=_HX)
    time.sleep(0.02)
    # An expired browser entry asks with refresh=1: the fresh five-minute entry must not answer it.
    forced = client.get(f"{url}?refresh=1", headers=_HX)
    assert len(calls) == 2
    assert _measured_at(forced) > _measured_at(first)
    assert _measured_at(client.get(url, headers=_HX)) == _measured_at(forced)
    # Cooldown and in-flight protection are unchanged and carry no measurement time.
    limited = client.get(f"{url}?refresh=1", headers=_HX)
    assert limited.status_code == 429 and "x-measured-at" not in limited.headers and len(calls) == 2
    _allow_refresh(app, target_id)
    with frontend_api._traceroute_refresh_lock:
        frontend_api._traceroute_refresh_inflight.add(
            (f"{frontend_api._TRACEROUTE_CACHE_VERSION}:{app.state.db_path}", target_id, "diag.example.com")
        )
    assert client.get(f"{url}?refresh=1", headers=_HX).status_code == 429
    assert len(calls) == 2
    client.close()


def test_unsuccessful_measurement_has_no_time_and_is_not_cached(tmp_path: Path, monkeypatch) -> None:
    client, url, calls, _app, _tid = _diagnostics_client(
        tmp_path, monkeypatch, [], target_ip="203.0.113.9", geo_points=[]
    )
    response = client.get(url, headers=_HX)
    assert response.status_code == 200 and "No route could be traced" in response.text
    assert "x-measured-at" not in response.headers  # the browser must not keep this as a measurement
    client.get(url, headers=_HX)
    assert len(calls) == 2  # and the server does not cache it either
    client.close()


def test_unauthenticated_fragment_has_no_measurement_time(tmp_path: Path, monkeypatch) -> None:
    hops = [_ok_hop(1, "203.0.113.9", 5.0)]
    client, url, calls, _app, _tid = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="203.0.113.9", geo_points=[]
    )
    client.get(url, headers=_HX)
    client.cookies.delete("token")
    denied = client.get(url, headers=_HX)
    assert denied.headers["hx-redirect"] == "/ui/login" and "x-measured-at" not in denied.headers
    assert len(calls) == 1
    client.close()


def test_cache_scope_separates_user_target_host_and_recreated_target() -> None:
    user = {"id": 1, "created_at": "2026-01-01 10:00:00"}
    row = {"id": 7, "created_at": "2026-02-01 10:00:00", "host": "a.example.com"}
    base = frontend_api._traceroute_cache_scope(user, row)
    assert base == frontend_api._traceroute_cache_scope(user, dict(row))
    assert all(len(v) == 16 and v.isalnum() for v in base.values())
    other_user = frontend_api._traceroute_cache_scope({**user, "id": 2}, row)
    assert other_user["user"] != base["user"] and other_user["target"] == base["target"]
    assert frontend_api._traceroute_cache_scope({**user, "created_at": "2026-03-01"}, row)["user"] != base["user"]
    assert frontend_api._traceroute_cache_scope(user, {**row, "host": "b.example.com"})["target"] != base["target"]
    assert frontend_api._traceroute_cache_scope(user, {**row, "created_at": "2026-03-01"})["target"] != base["target"]
    assert frontend_api._traceroute_cache_scope(user, {**row, "id": 8})["target"] != base["target"]


def test_target_page_delivers_scope_keys_and_no_secrets(tmp_path: Path, monkeypatch) -> None:
    client, _url, _calls, app, target_id = _diagnostics_client(
        tmp_path, monkeypatch, [], target_ip="203.0.113.9", geo_points=[]
    )
    page = client.get(f"/ui/targets/{target_id}")
    assert page.status_code == 200
    match = re.search(
        r'data-cache-user="([0-9a-f]{16})"\s+data-cache-target="([0-9a-f]{16})"\s+data-cache-build="([0-9a-f]{16})"',
        page.text,
    )
    assert match, "scope keys are rendered on the diagnostics container"
    assert 'title="Run new scan" aria-label="Run new scan"' in page.text
    assert client.cookies.get("token") not in page.text
    conn = sqlite_db.connect(app.state.db_path)
    conn.execute("UPDATE targets SET host = ? WHERE id = ?", ("changed.example.net", target_id))
    conn.commit()
    sqlite_db.close_connection(conn)
    changed = re.search(r'data-cache-target="([0-9a-f]{16})"', client.get(f"/ui/targets/{target_id}").text)
    assert changed and changed.group(1) != match.group(2)
    client.close()


def test_cache_scope_build_changes_with_fragment_revision_version_and_build(monkeypatch) -> None:
    """A browser entry rendered by other fragment logic or another release is never replayed."""
    user = {"id": 1, "created_at": "2026-01-01 10:00:00"}
    row = {"id": 7, "created_at": "2026-02-01 10:00:00", "host": "a.example.com"}
    base = frontend_api._traceroute_cache_scope(user, row)["build"]
    assert base == frontend_api._traceroute_cache_scope(user, row)["build"]
    for name, value in (
        ("TRACEROUTE_FRAGMENT_REVISION", frontend_api.TRACEROUTE_FRAGMENT_REVISION + 1),
        ("VERSION", "9.9.9"),
        ("BUILD_INFO", "0123abc"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(frontend_api, name, value)
            assert frontend_api._traceroute_cache_scope(user, row)["build"] != base, name


# ── Route timeline: state mapping, grouping and rendered markup ──


def _timeline(hops: list[dict], mapped: set[int], target_ip: str | None = "203.0.113.9") -> dict:
    return frontend_api._build_route_timeline(hops, mapped, target_ip, None)


def _seq_hop(hop: int, **extra) -> dict:
    return {"hop": hop, "ip": f"10.0.0.{hop}", "hostname": "-", "status": "ok", **extra}


def test_timeline_state_mapping_keeps_measurement_and_location_separate() -> None:
    hops = [
        _seq_hop(1),
        {**_seq_hop(2), "ip": "*", "status": "timeout"},
        {**_seq_hop(3), "ip": "*", "status": "interpolated", "_interpolated": True},
        _seq_hop(4, status="partial"),
        {**_seq_hop(5), "ip": "203.0.113.9", "status": "partial", "_synthetic_destination": True},
    ]
    nodes = _timeline(hops, {1, 4})["nodes"]
    by_hop = {n["hop"]: n for n in nodes}
    assert by_hop[1]["kind"] == "first" and by_hop[1]["title"] == "First hop"
    assert (by_hop[1]["measurement"], by_hop[1]["location"]) == ("measured", "mapped")
    assert (by_hop[2]["measurement"], by_hop[2]["location"]) == ("timeout", "unlocated")
    # An interpolated latency inside the interpreter never makes a silent hop "estimated" for the user.
    assert (by_hop[3]["measurement"], by_hop[3]["measurement_label"]) == ("timeout", "No reply")
    assert by_hop[3]["state_label"] == "No reply"
    assert by_hop[4]["measurement_label"] == "Measured, some probes lost"
    # A synthetic destination is never a successful measurement.
    assert by_hop[5]["kind"] == "destination" and by_hop[5]["measurement"] == "unreached"
    assert "not reached" in by_hop[5]["aria_label"].lower()
    # A measured hop without map location is not failed.
    assert (_timeline([_seq_hop(1), _seq_hop(2)], set())["nodes"][1]["measurement"]) == "measured"


def test_timeline_has_exactly_one_destination_and_first_hop() -> None:
    hops = [_seq_hop(1), _seq_hop(2, ip="203.0.113.9"), _seq_hop(3, ip="203.0.113.9")]
    kinds = [n["kind"] for n in _timeline(hops, {1, 2, 3})["nodes"]]
    assert kinds == ["first", "hop", "destination"]


def test_timeline_groups_only_contiguous_hop_numbers() -> None:
    hops = [_seq_hop(1), _seq_hop(2), _seq_hop(3), _seq_hop(4), _seq_hop(5, ip="203.0.113.9")]
    grouped = _timeline(hops, {1, 5})["nodes"]
    assert [(n["kind"], n["label"]) for n in grouped] == [("first", "1"), ("group", "2–4"), ("destination", "5")]
    assert [m["hop"] for m in grouped[1]["members"]] == [2, 3, 4]
    # Gap in the numbering (3 and 4 missing) ends the run: no group claims unseen hops.
    gap = [_seq_hop(1), _seq_hop(2), _seq_hop(3), _seq_hop(6), _seq_hop(7), _seq_hop(8), _seq_hop(9, ip="203.0.113.9")]
    nodes = _timeline(gap, {1, 9})["nodes"]
    assert [n["label"] for n in nodes] == ["1", "2", "3", "6–8", "9"]
    assert [n["kind"] for n in nodes] == ["first", "hop", "hop", "group", "destination"]
    short = [_seq_hop(1), _seq_hop(2), _seq_hop(3), _seq_hop(5, ip="203.0.113.9")]
    assert [n["kind"] for n in _timeline(short, {1, 5})["nodes"]] == ["first", "hop", "hop", "destination"]


def test_timeline_legend_lists_only_present_states() -> None:
    legend = _timeline([_seq_hop(1), _seq_hop(2, ip="203.0.113.9")], {1, 2})
    assert [s["key"] for s in legend["legend"]] == ["measured"] and not legend["has_unlocated"]


def test_fragment_renders_single_timeline(tmp_path: Path, monkeypatch) -> None:
    hops = [
        _ok_hop(1, "2001:db8::1", 1.0),
        _timeout_hop(2),
        _timeout_hop(3),
        _ok_hop(4, "2001:db8::4", 3.0),
        _ok_hop(5, "2001:db8::5", 4.0),
        _ok_hop(6, "2001:db8::6", 5.0),
        _ok_hop(7, "2001:db8::99", 6.0),
    ]
    geo = [{"ip": "2001:db8::5", "lat": 51.5, "lon": 7.4, "city": "Dortmund", "country": "DE", "hop": 5}]
    client, url, _calls, _app, _tid = _diagnostics_client(
        tmp_path, monkeypatch, hops, target_ip="2001:db8::99", geo_points=geo
    )
    text = client.get(url, headers=_HX).text
    client.close()
    assert "route_sequence" not in text and "traceroute-hop-sequence" not in text
    assert text.count('<ol class="tr-timeline"') == 1
    assert text.count("tr-step--first") == 1 and text.count("tr-step--destination") == 1
    assert "Start / Destination" not in text and ">Start<" not in text
    assert not re.search(r"<button[^>]*tr-step-btn[^>]*aria-label", text)
    assert 'data-hop-first="1"' in text and 'data-country="DE"' in text
    assert "2001:db8::1" in text and "2001:db8::5" in text
    assert 'aria-controls="tr-group-2-3"' not in text  # 2 hops are below the group threshold
    assert 'class="tr-route-legend"' in text and "No map location" in text
    assert "Hop 1–7" in text and "7 hops" in text
    assert 'title="2001:db8::99"' in text  # full IPv6 stays available


def test_timeline_groups_no_reply_runs_and_keeps_single_timeouts_separate() -> None:
    silent = {"ip": "*", "status": "timeout"}
    hops = [
        _seq_hop(1),
        *[{**_seq_hop(n), **silent} for n in (2, 3, 4)],
        _seq_hop(5),
        {**_seq_hop(6), **silent},
        _seq_hop(7, ip="203.0.113.9"),
    ]
    timeline = _timeline(hops, {1, 5, 7})
    nodes = timeline["nodes"]
    assert [(n["kind"], n["label"]) for n in nodes] == [
        ("first", "1"),
        ("group", "2–4"),
        ("hop", "5"),
        ("hop", "6"),
        ("destination", "7"),
    ]
    group = nodes[1]
    assert (group["title"], group["subtitle"], group["measurement"]) == ("3 hops without reply", "No reply", "timeout")
    assert nodes[3]["state_label"] == "No reply" and "estimated" not in nodes[3]["detail"].lower()
    assert [item["label"] for item in timeline["legend"]] == ["Measured", "No reply"]
