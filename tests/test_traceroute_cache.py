#!/usr/bin/env python3
#
# tests/test_traceroute_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, S106
"""The authenticated traceroute fragment reuses recent MTR results."""

from __future__ import annotations

import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import frontend as frontend_api
from app.db import sqlite as sqlite_db
from app.models.users import hash_token, new_token
from app.services import mtr_run, route_interpolator, traceroute_geo
from app.utils import crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect


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
    monkeypatch.setattr(mtr_run, "resolve_target_ip", lambda _host: "203.0.113.2")
    monkeypatch.setattr(mtr_run, "run_mtr", lambda *args, **_kwargs: calls.append(args) or hops)
    monkeypatch.setattr(mtr_run, "analyze_route", lambda *_args: {"destination_reached": True})
    monkeypatch.setattr(route_interpolator, "interpolate_route", lambda route, **_kwargs: route)
    monkeypatch.setattr(route_interpolator, "collapse_interpolated_hops", lambda route: route)
    monkeypatch.setattr(
        traceroute_geo,
        "geolocate_with_hostname_hint",
        lambda ip, _hint=None: next((point for point in geo_points if point["ip"] == ip), None),
    )
    monkeypatch.setattr(traceroute_geo, "mtr_hops_to_geo_points", lambda *_args, **_kwargs: geo_points)

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
    assert "Route endpoints" in first.text
    assert 'class="traceroute-hop-sequence-step traceroute-hop-sequence-step--start"' in first.text
    assert "traceroute-hop-sequence-step--unmapped" in first.text
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

    key = (str(app.state.db_path), target_id, "replacement.example.net")
    with frontend_api._traceroute_fragment_cache_lock:
        _expires_at, body = frontend_api._traceroute_fragment_cache[key]
        frontend_api._traceroute_fragment_cache[key] = (time.monotonic() - 1, body)
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
    """Authenticated client whose MTR/geo layer is stubbed; real interpolation and collapsing run."""
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(crypto, "_primary_key", None)
    from app.main import create_app
    from app.utils.rate_limit import limiter

    limiter.reset()
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
    monkeypatch.setattr(mtr_run, "resolve_target_ip", lambda _host: target_ip)
    monkeypatch.setattr(mtr_run, "run_mtr", lambda *args, **_kwargs: calls.append(args) or [dict(h) for h in hops])
    monkeypatch.setattr(
        traceroute_geo,
        "geolocate_with_hostname_hint",
        lambda ip, _hint=None: next((p for p in geo_points if p["ip"] == ip), None),
    )
    monkeypatch.setattr(traceroute_geo, "mtr_hops_to_geo_points", lambda *_args, **_kwargs: geo_points)

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
            "latency_ms": {"avg": 15.74},
            "loss_pct": 0.0,
            "city": "Frankfurt",
            "country": "DE",
        },
    ]
    kpis = frontend_api._destination_kpis(reached, "203.0.113.9", None)
    assert (kpis["hop_count"], kpis["latency_display"], kpis["loss_display"]) == (2, "15.7 ms", "0%")
    assert (kpis["destination_reached"], kpis["city"], kpis["country"]) == (True, "Frankfurt", "DE")

    lossy = [{"hop": 1, "ip": "203.0.113.9", "latency_ms": {"avg": 3.0}, "loss_pct": 33.33}]
    assert frontend_api._destination_kpis(lossy, "203.0.113.9", None)["loss_display"] == "33.3%"

    # A synthetic destination was never measured: latency and loss stay unknown, not 0.
    synthetic = [
        {"hop": 1, "ip": "10.0.0.1", "latency_ms": {"avg": 1.0}, "loss_pct": 0.0},
        {"hop": 2, "ip": "203.0.113.9", "latency_ms": None, "loss_pct": None, "_synthetic_destination": True},
    ]
    kpis = frontend_api._destination_kpis(synthetic, "203.0.113.9", {"city": "Berlin", "country": "DE"})
    assert kpis["latency_display"] is None and kpis["loss_display"] is None
    assert kpis["destination_reached"] is False and kpis["city"] == "Berlin"

    # Without a destination hop or a loss value nothing is invented.
    unknown = frontend_api._destination_kpis([{"hop": 1, "ip": "10.0.0.1"}], None, None)
    assert unknown["latency_display"] is None and unknown["loss_display"] is None and unknown["city"] is None
    no_loss = frontend_api._destination_kpis(
        [{"hop": 1, "ip": "203.0.113.9", "latency_ms": {"avg": 2.0}}], "203.0.113.9", None
    )
    assert no_loss["loss_display"] is None


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
    assert "ICMP blocked" in text and "tr-hop-row--blocked" in text
    assert 'data-state="interpolated"' in text
    assert text.count('class="traceroute-hop-sequence-step ') == 7
    for hop in range(2, 6):
        assert f'aria-label="Hop {hop}, interpolated"' in text
    assert "15.7 ms" in text and "0%" in text and "Frankfurt am Main, DE" in text
    assert "Geolocated route for 2 of 7 hops" in text
    for header in ("IP address", "Hostname", "Location", "Latency", "Loss", "Status"):
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
    assert text.count('data-known="false"') == 2  # latency and loss stay unknown, never 0
    assert text.count('class="tr-kpi-value">—</dd>') == 2
    assert "No reply" in text
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

    # Without the HTMX header the refresh flag is ignored (no cross-site trigger of an MTR run).
    assert client.get(f"{url}?refresh=1").status_code == 200
    assert len(calls) == 1

    forced = client.get(f"{url}?refresh=1", headers=_HX)
    assert forced.status_code == 200 and len(calls) == 2
    assert client.get(url, headers=_HX).content == forced.content  # cache now holds the new result
    assert len(calls) == 2

    # Immediate repeat is rejected (cooldown) and does not start another MTR run.
    again = client.get(f"{url}?refresh=1", headers=_HX)
    assert again.status_code == 429 and "retry-after" in again.headers and len(calls) == 2

    # After the cooldown another forced run is allowed.
    key = (str(app.state.db_path), target_id, "diag.example.com")
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
