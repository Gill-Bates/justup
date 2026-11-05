#!/usr/bin/env python3
#
# tests/test_rate_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""In-memory rate limiter: window maths, client buckets, 429 contract, per-route budgets, side-effect ordering."""

from __future__ import annotations

import gc
import logging
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import (
    csp_report,
    dashboard_layout,
    frontend,
    network_scans,
    pop,
    recipients,
    reports,
    settings,
    signal,
    targets,
    users,
)
from app.api.auth import get_current_user
from app.api.csp_report import CSP_REPORT_PATH
from app.db import sqlite as sqlite_db
from app.models.users import hash_password
from app.utils import crypto
from app.utils import rate_limit as rl
from app.utils.deps import get_conn
from app.utils.rate_limit import (
    RateLimitExceededError,
    SlidingWindow,
    parse_limit,
    rate_limit,
    rate_limit_auth,
    rate_limit_exceeded_handler,
    rate_limit_key,
    rate_limit_user,
    reset_all,
)

PROXY = "10.0.0.2"
PASSWORD = "Correct-horse-1"  # noqa: S105 - test fixture
GENERIC_429 = {"detail": "Too many requests. Please try again later."}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _request(peer: str | None, headers: list[tuple[str, str]] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers or []],
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(RateLimitExceededError, rate_limit_exceeded_handler)
    return app


# --- parser ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5/minute", (5, 60)),
        ("1/second", (1, 1)),
        ("10/hour", (10, 3600)),
        ("2/day", (2, 86400)),
        ("999999/day", (999999, 86400)),
    ],
)
def test_parse_limit_valid(text, expected):
    assert parse_limit(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "5",
        "5/week",
        "abc/minute",
        "0/minute",
        "-1/minute",
        "05/minute",
        "5 / minute",
        "5/minutes",
        "5 per minute",
        "1000000/day",
        None,
        5,
    ],
)
def test_parse_limit_invalid_raises_value_error(text):
    with pytest.raises(ValueError, match="rate limit"):
        parse_limit(text)


def test_invalid_limit_fails_at_registration_not_at_request_time():
    with pytest.raises(ValueError, match="rate limit"):
        rate_limit("lots")


@pytest.mark.parametrize("name", ["RATE_LIMIT_DEFAULT", "RATE_LIMIT_HEAVY", "RATE_LIMIT_PDF"])
def test_presets_are_valid(name):
    parse_limit(getattr(rl, name))


# --- sliding window -------------------------------------------------------------------------------


def test_window_allows_up_to_limit_then_reports_retry_after():
    clock = FakeClock()
    win = SlidingWindow(2, 60, clock)
    assert win.hit("a") is None
    clock.now += 10
    assert win.hit("a") is None
    clock.now += 5
    assert win.hit("a") == pytest.approx(45)  # the oldest hit leaves the window 60 s after it was made


def test_window_boundary_is_exact():
    clock = FakeClock()
    win = SlidingWindow(1, 60, clock)
    assert win.hit("a") is None
    clock.now += 59.999
    assert win.hit("a") == pytest.approx(0.001)
    clock.now += 0.001
    assert win.hit("a") is None  # a hit exactly one window old no longer counts


def test_rejected_requests_are_not_recorded():
    clock = FakeClock()
    win = SlidingWindow(1, 60, clock)
    assert win.hit("a") is None
    for _ in range(5):
        clock.now += 1
        assert win.hit("a") is not None
    clock.now = 1060.0
    assert win.hit("a") is None  # the rejections did not extend the block


def test_keys_are_independent():
    win = SlidingWindow(1, 60, FakeClock())
    assert win.hit("10.0.0.1") is None
    assert win.hit("10.0.0.2") is None
    assert win.hit("10.0.0.1") is not None


def test_concurrent_hits_never_exceed_the_limit():
    win = SlidingWindow(50, 60, FakeClock())
    allowed: list[int] = []

    def worker():
        allowed.append(sum(1 for _ in range(40) if win.hit("same") is None))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(allowed) == 50


def test_hit_accepts_a_lowered_limit_without_forgetting_history():
    clock = FakeClock()
    win = SlidingWindow(10, 60, clock)
    for _ in range(5):
        assert win.hit("a") is None
        clock.now += 1
    assert win.hit("a", limit=3) is not None  # 5 recorded hits already exceed the new limit of 3
    clock.now += 100
    assert win.hit("a", limit=3) is None


# --- memory bound ---------------------------------------------------------------------------------


def test_full_table_rejects_new_keys_but_never_evicts_active_ones():
    clock = FakeClock()
    win = SlidingWindow(5, 60, clock, max_keys=3)
    for key in ("a", "b", "c"):
        assert win.hit(key) is None
    with pytest.raises(RuntimeError, match="capacity"):
        win.hit("d")
    assert len(win) == 3
    assert win.hit("a") is None  # known keys keep being served
    clock.now += 61
    assert win.hit("d") is None  # expired buckets were removed to make room
    assert len(win) == 1


def test_full_table_sweep_is_throttled():
    clock = FakeClock()
    win = SlidingWindow(1, 1, clock, max_keys=2)
    win.hit("a")
    win.hit("b")
    clock.now += 0.5
    with pytest.raises(RuntimeError, match="capacity"):
        win.hit("c")  # sweeps, finds both active
    clock.now += 0.4
    with pytest.raises(RuntimeError, match="capacity"):
        win.hit("c")  # no second full scan within the sweep interval
    clock.now += 0.7
    assert win.hit("c") is None


def test_reset_all_clears_every_counter():
    win = SlidingWindow(1, 60, FakeClock())
    assert win.hit("a") is None
    assert win.hit("a") is not None
    reset_all()
    assert win.hit("a") is None


# --- client bucket --------------------------------------------------------------------------------


def test_ipv6_clients_share_one_counter_per_slash64():
    assert rate_limit_key(_request("2001:db8:1:2::1")) == rate_limit_key(_request("2001:db8:1:2:ffff::9"))
    assert rate_limit_key(_request("2001:db8:1:2::1")) != rate_limit_key(_request("2001:db8:1:3::1"))
    assert rate_limit_key(_request("2001:db8:1:2::1")) == "2001:db8:1:2::/64"


def test_ipv4_mapped_ipv6_uses_the_ipv4_bucket():
    assert rate_limit_key(_request("::ffff:192.0.2.9")) == rate_limit_key(_request("192.0.2.9")) == "192.0.2.9"


def test_missing_client_address_shares_one_conservative_bucket():
    assert rate_limit_key(_request(None)) == "unknown"
    app = _app()

    @app.get("/a", dependencies=[Depends(rate_limit("2/minute"))])
    def route():
        return {}

    client = TestClient(app)
    assert [client.get("/a").status_code for _ in range(3)] == [200, 200, 429]


def test_forged_forwarding_headers_from_untrusted_peers_do_not_create_buckets():
    forged = [("X-Forwarded-For", "198.51.100.1"), ("X-Real-IP", "198.51.100.2"), ("Forwarded", "for=198.51.100.3")]
    assert rate_limit_key(_request("203.0.113.9", forged)) == "203.0.113.9"
    app = _app()

    @app.get("/a", dependencies=[Depends(rate_limit("2/minute"))])
    def route():
        return {}

    client = TestClient(app, client=("203.0.113.9", 1))
    codes = [client.get("/a", headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_trusted_proxy_forwards_the_real_client(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", PROXY)
    chain = [("X-Forwarded-For", "6.6.6.6, 198.51.100.7")]
    assert rate_limit_key(_request(PROXY, chain)) == "198.51.100.7"  # the left side is client-controlled
    assert rate_limit_key(_request("203.0.113.9", chain)) == "203.0.113.9"
    assert rate_limit_key(_request(PROXY, [("X-Forwarded-For", "2001:db8::5")])) == "2001:db8::/64"


def test_trusted_proxy_clients_get_separate_counters(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", PROXY)
    app = _app()

    @app.get("/a", dependencies=[Depends(rate_limit("1/minute"))])
    def route():
        return {}

    client = TestClient(app, client=(PROXY, 1))
    assert client.get("/a", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200
    assert client.get("/a", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 200
    assert client.get("/a", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 429


# --- HTTP contract --------------------------------------------------------------------------------


def _limited_app(clock: FakeClock) -> FastAPI:
    app = _app()

    def deny():
        raise HTTPException(status_code=401, detail="nope")

    @app.get("/a", dependencies=[Depends(rate_limit("2/minute", clock=clock))])
    def route_a():
        return {"ok": "a"}

    @app.get("/b", dependencies=[Depends(rate_limit("2/minute", clock=clock))])
    def route_b():
        return {"ok": "b"}

    @app.get("/secure", dependencies=[Depends(rate_limit("2/minute", clock=clock))])
    def secure(_=Depends(deny)):
        return {"ok": "secure"}

    return app


def test_first_n_allowed_then_429_with_contract_headers():
    clock = FakeClock()
    client = TestClient(_limited_app(clock))
    assert client.get("/a").status_code == 200
    clock.now += 20
    assert client.get("/a").status_code == 200
    clock.now += 10
    response = client.get("/a")
    assert response.status_code == 429
    assert response.json() == GENERIC_429
    assert response.headers["Retry-After"] == "30"
    assert response.headers["X-RateLimit-Limit"] == "2"
    clock.now += 30
    assert client.get("/a").status_code == 200


def test_retry_after_is_at_least_one_second():
    clock = FakeClock()
    client = TestClient(_limited_app(clock))
    assert [client.get("/a").status_code for _ in range(2)] == [200, 200]
    clock.now += 59.9999
    assert client.get("/a").headers["Retry-After"] == "1"


def test_counters_are_separate_per_route_even_with_identical_limit_string():
    client = TestClient(_limited_app(FakeClock()))
    assert [client.get("/a").status_code for _ in range(3)] == [200, 200, 429]
    assert client.get("/b").status_code == 200


def test_limit_is_counted_before_the_auth_dependency():
    client = TestClient(_limited_app(FakeClock()))
    assert [client.get("/secure").status_code for _ in range(3)] == [401, 401, 429]


def test_internal_error_fails_closed_with_logged_cause(monkeypatch, caplog):
    def boom(self, key, **kwargs):  # noqa: ARG001
        raise ValueError("broken")

    monkeypatch.setattr(SlidingWindow, "hit", boom)
    monkeypatch.setattr(rl, "_last_failure_log", -1000.0)
    client = TestClient(_limited_app(FakeClock()))
    with caplog.at_level(logging.ERROR, logger=rl.__name__):
        responses = [client.get("/a") for _ in range(3)]
    assert [r.status_code for r in responses] == [429, 429, 429]
    assert responses[0].json() == GENERIC_429  # no internal detail in the body
    assert "broken" not in responses[0].text
    assert 1 <= int(responses[0].headers["Retry-After"]) <= 60
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1  # log flood is throttled


def test_capacity_overflow_answers_429_with_retry_after(monkeypatch):
    def full(self, key, **kwargs):  # noqa: ARG001
        raise rl.CapacityExhaustedError("full")

    monkeypatch.setattr(SlidingWindow, "hit", full)
    response = TestClient(_limited_app(FakeClock())).get("/a")
    assert response.status_code == 429
    assert response.json() == GENERIC_429
    assert 1 <= int(response.headers["Retry-After"]) <= 60


def test_limit_applies_to_each_client_separately():
    app = _app()

    @app.get("/a", dependencies=[Depends(rate_limit("1/minute"))])
    def route():
        return {}

    first = TestClient(app, client=("203.0.113.1", 1))
    second = TestClient(app, client=("203.0.113.2", 1))
    assert first.get("/a").status_code == 200
    assert first.get("/a").status_code == 429
    assert second.get("/a").status_code == 200


# --- dynamic login limit --------------------------------------------------------------------------


@pytest.fixture
def auth_setting(monkeypatch):
    holder = {"value": "10/minute"}
    monkeypatch.setattr(rl, "get_runtime_setting", lambda _key, _default=None: holder["value"])
    return holder


def _auth_client(clock: FakeClock) -> TestClient:
    app = _app()

    @app.post("/login", dependencies=[Depends(rate_limit_auth(clock=clock))])
    def login():
        return {}

    return TestClient(app)


@pytest.mark.usefixtures("auth_setting")
def test_auth_limit_defaults_to_ten_per_minute():
    client = _auth_client(FakeClock())
    assert [client.post("/login").status_code for _ in range(11)] == [200] * 10 + [429]


def test_auth_limit_change_applies_without_restart_and_keeps_history(auth_setting):
    clock = FakeClock()
    client = _auth_client(clock)
    assert [client.post("/login").status_code for _ in range(4)] == [200] * 4
    auth_setting["value"] = "3/minute"
    # The four hits already made count: lowering the limit must not hand out a fresh budget.
    assert client.post("/login").status_code == 429
    clock.now += 61
    assert [client.post("/login").status_code for _ in range(4)] == [200, 200, 200, 429]


def test_auth_limit_raise_takes_effect_immediately(auth_setting):
    auth_setting["value"] = "2/minute"
    client = _auth_client(FakeClock())
    assert [client.post("/login").status_code for _ in range(3)] == [200, 200, 429]
    auth_setting["value"] = "5/minute"
    assert [client.post("/login").status_code for _ in range(4)] == [200, 200, 200, 429]


@pytest.mark.parametrize("bad", ["fast", "0/minute", "5/week", "", "10 / minute", "9999999/minute"])
def test_invalid_stored_auth_limit_falls_back_to_the_default(auth_setting, bad):
    auth_setting["value"] = bad
    assert rl.RATE_LIMIT_AUTH() == "10/minute"
    client = _auth_client(FakeClock())
    assert [client.post("/login").status_code for _ in range(11)] == [200] * 10 + [429]


def test_changing_the_auth_limit_creates_no_new_counters(auth_setting):
    client = _auth_client(FakeClock())
    # _windows is weak: collect dead windows of earlier tests so GC timing cannot skew the count.
    gc.collect()
    windows_before = len(rl._windows)  # noqa: SLF001
    for value in ("3/minute", "4/minute", "3/minute", "5/hour", "x", "3/minute"):
        auth_setting["value"] = value
        client.post("/login")
    assert len(rl._windows) == windows_before  # noqa: SLF001
    assert rl._parse_str.cache_info().currsize <= 64  # noqa: SLF001


def test_each_auth_endpoint_has_its_own_counter(auth_setting):
    auth_setting["value"] = "1/minute"
    app = _app()

    @app.post("/one", dependencies=[Depends(rate_limit_auth())])
    def one():
        return {}

    @app.post("/two", dependencies=[Depends(rate_limit_auth())])
    def two():
        return {}

    client = TestClient(app)
    assert [client.post(p).status_code for p in ("/one", "/one", "/two")] == [200, 429, 200]


# --- login: rate limit on top of the existing lockout ---------------------------------------------


@pytest.fixture
def login_client(tmp_path, auth_setting):
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    sqlite_db.create_user(connection, username="alice", password_hash=hash_password(PASSWORD), is_admin=False)
    app = _app()
    app.include_router(users.router)
    app.dependency_overrides[get_conn] = lambda: connection
    yield TestClient(app, client=("203.0.113.50", 1)), auth_setting
    sqlite_db.close_connection(connection)


def test_login_rate_limit_answers_429_with_retry_after(login_client):
    client, setting = login_client
    setting["value"] = "3/minute"
    good = {"username": "alice", "password": PASSWORD}
    assert [client.post("/login", json=good).status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.post("/login", json=good)
    assert blocked.status_code == 429
    assert blocked.json() == GENERIC_429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_login_lockout_keeps_its_own_response_and_retry_after(login_client):
    client, _setting = login_client
    bad = {"username": "alice", "password": "wrong-password-1"}
    assert client.post("/login", json=bad).status_code == 401
    locked = client.post("/login", json={"username": "alice", "password": PASSWORD})
    assert locked.status_code == 429
    assert locked.json()["detail"] == "Too many failed attempts. Please try again later."
    assert int(locked.headers["Retry-After"]) >= 1
    assert "X-RateLimit-Limit" not in locked.headers  # not produced by the limiter


# --- signal: per-user budget ----------------------------------------------------------------------


class _FakeSignalBackend:
    def __init__(self) -> None:
        self.cancels = 0

    def cancel_link(self) -> None:
        self.cancels += 1


@pytest.fixture
def signal_env(monkeypatch):
    backend = _FakeSignalBackend()
    monkeypatch.setattr(signal, "get_signal_backend", lambda: backend)
    tokens = {"Bearer a1": 1, "Bearer a2": 1, "Bearer b1": 2}

    def fake_auth(request: Request):
        user_id = tokens.get(request.headers.get("authorization", ""))
        if user_id is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {"id": user_id, "is_admin": True}

    app = _app()
    app.include_router(signal.router)
    app.dependency_overrides[get_current_user] = fake_auth
    return app, backend


def test_signal_budget_is_per_user_not_per_token(signal_env):
    app, backend = signal_env
    client = TestClient(app, client=("203.0.113.5", 1))
    # link/cancel allows 10 per hour: alternating two valid tokens of ONE user must not double it.
    codes = [
        client.post("/signal/link/cancel", headers={"Authorization": f"Bearer a{1 + i % 2}"}).status_code
        for i in range(11)
    ]
    assert codes == [204] * 10 + [429]
    assert backend.cancels == 10  # the 429 came before the handler body ran
    other = client.post("/signal/link/cancel", headers={"Authorization": "Bearer b1"})
    assert other.status_code == 204  # another user stays independent


def test_signal_user_budget_survives_a_changing_client_address(signal_env):
    app, _backend = signal_env
    codes = []
    for i in range(11):
        client = TestClient(app, client=(f"203.0.113.{10 + i}", 1))
        codes.append(client.post("/signal/link/cancel", headers={"Authorization": "Bearer a1"}).status_code)
    assert codes == [204] * 10 + [429]


def test_signal_unauthenticated_flood_is_ip_limited_and_spares_user_budgets(signal_env):
    app, backend = signal_env
    attacker = TestClient(app, client=("198.51.100.66", 1))
    codes = [attacker.post("/signal/link/cancel").status_code for _ in range(62)]
    assert codes[:60] == [401] * 60
    assert codes[60:] == [429, 429]
    assert backend.cancels == 0
    victim = TestClient(app, client=("203.0.113.5", 1))
    ok = [victim.post("/signal/link/cancel", headers={"Authorization": "Bearer a1"}).status_code for _ in range(10)]
    assert ok == [204] * 10  # the flood did not touch the user's own budget


def test_signal_budgets_match_the_previous_limits():
    expected = {
        ("POST", "/signal/send"): "5/minute",
        ("GET", "/signal/qr"): "3/hour",
        ("POST", "/signal/link/confirm"): "5/hour",
        ("POST", "/signal/link/cancel"): "10/hour",
        ("POST", "/signal/register"): "3/hour",
        ("POST", "/signal/verify"): "10/hour",
    }
    assert _route_specs(signal.router) == {k: sorted([rl.RATE_LIMIT_DEFAULT, v]) for k, v in expected.items()}


def test_user_limit_pairs_with_the_given_authentication_dependency():
    app = _app()

    def auth(request: Request):
        return {"id": int(request.headers["x-user"])}

    @app.get("/x", dependencies=[Depends(rate_limit_user("1/minute", auth))])
    def route():
        return {}

    client = TestClient(app)
    assert client.get("/x", headers={"X-User": "1"}).status_code == 200
    assert client.get("/x", headers={"X-User": "1"}).status_code == 429
    assert client.get("/x", headers={"X-User": "2"}).status_code == 200


# --- regression table: every route keeps its limit, none is counted twice -------------------------

RATE_LIMITED_ROUTES = {
    ("POST", "/login"): ["auth"],
    ("POST", "/me/password"): ["auth"],
    ("POST", "/me/complete-required-change"): ["auth"],
    ("GET", "/ui/fragments/target-info-cards/{target_id}"): ["60/minute"],
    ("GET", "/ui/fragments/status-summary"): ["30/minute"],
    ("GET", "/ui/fragments/targets-table"): ["30/minute"],
    ("GET", "/ui/fragments/target-status/{target_id}"): ["60/minute"],
    ("GET", "/ui/fragments/uptime/{target_id}"): ["300/minute"],
    ("GET", "/ui/fragments/traceroute/{target_id}"): ["10/minute"],
    ("GET", "/reports/targets/{target_id}/pdf"): ["5/minute"],
    ("POST", "/reports/targets/{target_id}/pdf/request"): ["5/minute"],
    ("POST", "/settings/tsdb/purge"): ["10/minute"],
    ("POST", "/settings/alerts/purge"): ["10/minute"],
    ("POST", "/settings/auth/toggle"): ["5/minute"],
    ("POST", "/settings/signal/test"): ["5/minute"],
    ("GET", "/settings/signal/status"): ["30/minute"],
    ("GET", "/settings/network/public-egress"): ["12/minute"],
    ("POST", "/settings/signal/unregister"): ["3/minute"],
    ("POST", "/settings/signal/unlink"): ["5/minute"],
    ("POST", "/settings/backup"): ["10/minute"],
    ("POST", "/settings/restore"): ["10/minute"],
    ("POST", "/settings/smtp/test-mail"): ["5/minute"],
    ("POST", "/signal/send"): ["5/minute", "60/minute"],
    ("GET", "/signal/qr"): ["3/hour", "60/minute"],
    ("POST", "/signal/link/confirm"): ["5/hour", "60/minute"],
    ("POST", "/signal/link/cancel"): ["10/hour", "60/minute"],
    ("POST", "/signal/register"): ["3/hour", "60/minute"],
    ("POST", "/signal/verify"): ["10/hour", "60/minute"],
    ("POST", "/targets/{target_id}/test-alert"): ["5/minute"],
    ("POST", "/recipients/verify-signal"): ["10/minute"],
    ("POST", "/recipients/{recipient_id}/recheck-signal"): ["5/minute"],
    ("POST", "/recipients/{recipient_id}/test"): ["10/minute"],
    ("GET", "/recipients/{recipient_id}/test/{queue_id}"): ["60/minute"],
    ("GET", "/me/dashboard-layout"): ["60/minute"],
    ("GET", "/me/dashboard-layout/monitors"): ["60/minute"],
    ("GET", "/me/dashboard-layout/uptime"): ["60/minute"],
    ("PUT", "/me/dashboard-layout"): ["60/minute"],
    ("DELETE", "/me/dashboard-layout"): ["60/minute"],
    ("POST", "/api/admin/pop/refresh"): ["10/minute"],
    ("POST", CSP_REPORT_PATH): ["30/minute"],
    ("GET", "/network-scans/status"): ["60/minute"],
    ("POST", "/network-scans/services/{target_id}/preview"): ["2/minute", "5/minute"],
    ("POST", "/network-scans/services/{target_id}/confirm"): ["10/minute", "5/minute"],
    ("POST", "/network-scans"): ["10/minute", "3/minute"],
    ("GET", "/network-scans/{scan_id}"): ["60/minute"],
    ("DELETE", "/network-scans/{scan_id}"): ["10/minute"],
}


def _route_specs(router_or_app) -> dict[tuple[str, str], list[str]]:
    found: dict[tuple[str, str], list[str]] = {}
    for route in router_or_app.routes:
        if not isinstance(route, APIRoute):
            continue
        specs = sorted(
            spec for dep in route.dependant.dependencies if (spec := getattr(dep.call, "rate_limit_spec", None))
        )
        if specs:
            for method in route.methods:
                found[(method, route.path)] = specs
    return found


def test_all_46_rate_limited_routes_keep_their_limit_exactly_once():
    found: dict[tuple[str, str], list[str]] = {}
    for router in (
        users.router,
        dashboard_layout.router,
        csp_report.router,
        targets.router,
        settings.router,
        signal.router,
        recipients.router,
        reports.router,
        reports.ui_router,
        pop.router,
        frontend.router,
        network_scans.router,
    ):
        found |= _route_specs(router)
    assert len(RATE_LIMITED_ROUTES) == 46
    assert found == {k: sorted(v) for k, v in RATE_LIMITED_ROUTES.items()}


# --- 429 happens before expensive side effects ----------------------------------------------------


def _admin_app(tmp_path: Path, *routers) -> tuple[FastAPI, object]:
    conn = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(conn)
    app = _app()
    for router in routers:
        app.include_router(router)
    app.state.data_dir = tmp_path / "missing-data-dir"
    app.state.tsdb_dir = tmp_path
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_admin": True}
    return app, conn


def test_backup_is_rejected_before_any_work_once_the_limit_is_used(tmp_path):
    app, conn = _admin_app(tmp_path, settings.router)
    try:
        client = TestClient(app)
        # The missing data dir makes the body fail with 404 without side effects.
        assert [client.post("/settings/backup").status_code for _ in range(10)] == [404] * 10
        assert client.post("/settings/backup").status_code == 429
    finally:
        sqlite_db.close_connection(conn)


def test_restore_is_rejected_before_the_password_check_or_upload_is_read(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(settings, "_verify_admin_password", lambda *_a: calls.append("verify"))
    app, conn = _admin_app(tmp_path, settings.router)
    try:
        client = TestClient(app)
        files = {"file": ("not-a-backup.tar.bz2", b"x")}
        codes = [client.post("/settings/restore", data={"password": "x"}, files=files).status_code for _ in range(11)]
        assert codes[:10] == [400] * 10  # invalid file name, rejected by the handler body
        assert codes[10] == 429
        assert len(calls) == 10
    finally:
        sqlite_db.close_connection(conn)


def test_test_mail_is_rejected_before_templates_or_spooler_are_touched(tmp_path):
    app, conn = _admin_app(tmp_path, settings.router)
    try:
        client = TestClient(app)
        codes = [client.post("/settings/smtp/test-mail", json={"to": "a@example.com"}).status_code for _ in range(6)]
        assert codes[-1] == 429
        assert 429 not in codes[:5]
    finally:
        sqlite_db.close_connection(conn)


def test_pdf_report_is_rejected_before_rendering(tmp_path, monkeypatch):
    rendered: list[int] = []
    monkeypatch.setattr(reports, "generate_pdf_report", lambda *_a: rendered.append(1) or b"%PDF")
    app, conn = _admin_app(tmp_path, reports.router)
    try:
        client = TestClient(app)
        codes = [client.get("/reports/targets/999/pdf").status_code for _ in range(6)]
        assert codes == [404] * 5 + [429]
        assert rendered == []
    finally:
        sqlite_db.close_connection(conn)


def test_csp_report_sink_allows_30_per_minute_per_peer():
    app = _app()
    app.include_router(csp_report.router)
    client = TestClient(app, client=("203.0.113.9", 1))
    body = {"csp-report": {"violated-directive": "script-src"}}
    headers = {"Content-Type": "application/json"}
    codes = [client.post(CSP_REPORT_PATH, json=body, headers=headers).status_code for _ in range(31)]
    assert codes == [204] * 30 + [429]
