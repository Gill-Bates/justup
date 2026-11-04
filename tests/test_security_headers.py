#!/usr/bin/env python3
#
# tests/test_security_headers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Security headers on every response, the CSP report sink, and trusted-proxy behavior end to end."""

from __future__ import annotations

import logging
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import csp_report, users
from app.db import sqlite as sqlite_db
from app.middleware.security_headers import ENFORCED_CSP, REPORT_ONLY_CSP, SecurityHeadersMiddleware
from app.models.users import hash_password
from app.utils import crypto
from app.utils.client_ip import audit_client_ip
from app.utils.deps import get_conn
from app.utils.rate_limit import RateLimitExceededError, rate_limit_exceeded_handler, reset_all
from tests import _support

PASSWORD = "Correct-horse-1"  # noqa: S105 - test fixture
PROXY = "10.0.0.2"
VIOLATION = {"csp-report": {"violated-directive": "script-src", "blocked-uri": "https://evil.example/x.js?q=1"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    monkeypatch.delenv("UPMON_TRUSTED_PROXIES", raising=False)
    reset_all()


@pytest.fixture
def app(monkeypatch):
    with TemporaryDirectory(prefix="justup-sec-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        from app.main import create_app

        application = create_app()

        @application.get("/_boom")
        def boom():
            raise RuntimeError("boom")

        @application.get("/_own-headers")
        def own_headers():
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(
                "x", headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'"}
            )

        conn = sqlite_db.connect(application.state.db_path)
        try:
            sqlite_db.init_schema(conn)
        finally:
            sqlite_db.close_connection(conn)
        yield application


def _values(resp, name: str) -> list[str]:
    return resp.headers.get_list(name)


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/ui/login", 200),  # HTML
        ("/healthz", 200),  # JSON
        ("/static/favicon.svg", 200),  # static
        ("/static/does-not-exist.js", 404),  # static error
        ("/nope", 404),  # routing error
        ("/me", 401),  # auth error (JSON)
        ("/_boom", 500),  # unhandled exception, served by ServerErrorMiddleware
    ],
)
def test_headers_present_exactly_once(app, path, status):
    resp = TestClient(app, raise_server_exceptions=False).get(path)
    assert resp.status_code == status
    assert _values(resp, "X-Frame-Options") == ["SAMEORIGIN"]
    assert _values(resp, "Content-Security-Policy") == [ENFORCED_CSP]
    assert _values(resp, "Content-Security-Policy-Report-Only") == [REPORT_ONLY_CSP]


def test_enforced_policy_only_restricts_framing_and_report_only_never_enforces():
    assert ENFORCED_CSP == "frame-ancestors 'self'"
    assert "script-src" in REPORT_ONLY_CSP and "report-uri /csp-report" in REPORT_ONLY_CSP
    assert "img-src 'self' data: blob: https://cdn.jsdelivr.net" in REPORT_ONLY_CSP


def test_route_specific_headers_are_kept_not_duplicated(app):
    resp = TestClient(app).get("/_own-headers")
    assert _values(resp, "X-Frame-Options") == ["DENY"]
    assert _values(resp, "Content-Security-Policy") == ["default-src 'none'"]
    assert _values(resp, "Content-Security-Policy-Report-Only") == [REPORT_ONLY_CSP]


def test_non_http_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):  # noqa: ARG001
        seen.append(scope["type"])

    import asyncio

    asyncio.run(SecurityHeadersMiddleware(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


# --- CSP report endpoint -----------------------------------------------------------------------


def test_report_is_logged_without_query_and_answers_204(app, caplog):
    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger=csp_report.__name__):
        resp = client.post("/csp-report", json=VIOLATION, headers={"Content-Type": "application/csp-report"})
    assert resp.status_code == 204
    assert resp.content == b""
    message = next(r.getMessage() for r in caplog.records if "CSP_VIOLATION" in r.getMessage())
    assert "https://evil.example/x.js" in message
    assert "q=1" not in message
    assert _values(resp, "X-Frame-Options") == ["SAMEORIGIN"]


def test_report_sink_requires_no_auth_or_csrf_but_rejects_abuse(app, caplog):
    client = TestClient(app)
    assert client.post("/csp-report", content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
    big = b'{"csp-report": {"blocked-uri": "' + b"a" * 9000 + b'"}}'
    assert client.post("/csp-report", content=big, headers={"Content-Type": "application/json"}).status_code == 413
    assert (
        client.post("/csp-report", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 204
    )
    assert not any("CSP_VIOLATION" in r.getMessage() for r in caplog.records)


def test_report_sink_accepts_browser_reports_that_carry_a_session_cookie(app):
    # Browsers attach cookies to same-origin report POSTs but cannot add X-CSRF-Token.
    client = TestClient(app)
    client.cookies.set("token", "any-session-value")
    response = client.post("/csp-report", content=b"{}", headers={"Content-Type": "application/csp-report"})
    assert response.status_code == 204


def test_report_log_injection_is_neutralized(app, caplog):
    forged = {"csp-report": {"violated-directive": "x\nCRITICAL forged line\r", "blocked-uri": "y" * 5000}}
    with caplog.at_level(logging.WARNING, logger=csp_report.__name__):
        TestClient(app).post("/csp-report", json=forged, headers={"Content-Type": "application/json"})
    message = next(r.getMessage() for r in caplog.records if "CSP_VIOLATION" in r.getMessage())
    assert "\n" not in message and "\r" not in message
    assert len(message) < 600


def test_report_sink_is_rate_limited_per_untrusted_peer_even_with_forged_xff(app):
    client = TestClient(app, client=("203.0.113.9", 1))
    codes = [
        client.post(
            "/csp-report",
            json=VIOLATION,
            headers={"Content-Type": "application/json", "X-Forwarded-For": f"198.51.100.{i}"},
        ).status_code
        for i in range(32)
    ]
    assert codes[:30] == [204] * 30
    assert 429 in codes[30:]


def test_report_sink_rate_limit_uses_forwarded_client_behind_trusted_proxy(app, monkeypatch):
    monkeypatch.setenv("UPMON_TRUSTED_PROXIES", PROXY)
    client = TestClient(app, client=(PROXY, 1))
    codes = [
        client.post(
            "/csp-report",
            json=VIOLATION,
            headers={"Content-Type": "application/json", "X-Forwarded-For": f"198.51.100.{i}"},
        ).status_code
        for i in range(32)
    ]
    assert set(codes) == {204}  # every real client has its own bucket


# --- trusted proxies end to end ------------------------------------------------------------------


@pytest.fixture
def login_env(tmp_path):
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    sqlite_db.create_user(connection, username="alice", password_hash=hash_password(PASSWORD), is_admin=False)
    application = FastAPI()
    application.add_exception_handler(RateLimitExceededError, rate_limit_exceeded_handler)
    application.include_router(users.router)
    application.dependency_overrides[get_conn] = lambda: connection

    @application.get("/_audit-ip")
    def audit_ip(request: Request):
        return {"ip": audit_client_ip(request)}

    yield application, connection
    sqlite_db.close_connection(connection)


def _login(client, headers=None, password=PASSWORD):
    return client.post("/login", json={"username": "alice", "password": password}, headers=headers or {})


def _locked_ips(connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT ip_address FROM login_attempts")]


def test_without_configuration_only_the_direct_peer_counts(login_env):
    application, connection = login_env
    client = TestClient(application, client=(PROXY, 1))
    forged = {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8", "X-Forwarded-Proto": "https"}
    ok = _login(client, forged)
    assert ok.status_code == 200
    assert "secure" not in ok.headers["set-cookie"].lower()
    assert _login(client, forged, password="wrong").status_code == 401  # noqa: S106
    assert _locked_ips(connection) == [PROXY]
    assert client.get("/_audit-ip", headers=forged).json() == {"ip": PROXY}


def test_trusted_proxy_controls_cookie_flag_lockout_and_audit_ip(login_env, monkeypatch):
    application, connection = login_env
    monkeypatch.setenv("UPMON_TRUSTED_PROXIES", PROXY)
    client = TestClient(application, client=(PROXY, 1))
    headers = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"}
    assert "secure" in _login(client, headers).headers["set-cookie"].lower()
    assert _login(client, headers, password="wrong").status_code == 401  # noqa: S106
    assert _locked_ips(connection) == ["198.51.100.7"]
    assert client.get("/_audit-ip", headers=headers).json() == {"ip": "198.51.100.7"}


def test_untrusted_peer_cannot_spoof_when_a_different_proxy_is_trusted(login_env, monkeypatch):
    application, connection = login_env
    monkeypatch.setenv("UPMON_TRUSTED_PROXIES", PROXY)
    client = TestClient(application, client=("203.0.113.9", 1))
    headers = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"}
    assert "secure" not in _login(client, headers).headers["set-cookie"].lower()
    assert _login(client, headers, password="wrong").status_code == 401  # noqa: S106
    assert _locked_ips(connection) == ["203.0.113.9"]
    assert client.get("/_audit-ip", headers=headers).json() == {"ip": "203.0.113.9"}


def test_compose_passes_trusted_proxies_as_optional_empty_default():
    text = (_support.ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "UPMON_TRUSTED_PROXIES: ${UPMON_TRUSTED_PROXIES:-}\n" in text


def test_report_sink_understands_the_reporting_api_format(app, caplog):
    payload = [
        {"type": "deprecation", "body": {"blockedURL": "https://ignored.example/"}},
        {
            "type": "csp-violation",
            "body": {
                "effectiveDirective": "script-src-elem",
                "blockedURL": "https://evil.example/x.js?q=1",
                "documentURL": "https://app.example/ui/login",
                "originalPolicy": "must-not-be-logged",
            },
        },
    ]
    with caplog.at_level(logging.WARNING, logger=csp_report.__name__):
        resp = TestClient(app).post("/csp-report", json=payload, headers={"Content-Type": "application/reports+json"})
    assert resp.status_code == 204
    message = next(r.getMessage() for r in caplog.records if "CSP_VIOLATION" in r.getMessage())
    assert "effective-directive='script-src-elem'" in message and "https://evil.example/x.js" in message
    assert "q=1" not in message and "must-not-be-logged" not in message and "ignored.example" not in message


def test_report_sink_stops_reading_an_oversized_chunked_body_early(app):
    import asyncio

    chunks_read = 0
    responses: list[dict] = []

    async def receive():
        nonlocal chunks_read
        chunks_read += 1
        return {"type": "http.request", "body": b"a" * 1024, "more_body": True}

    async def send(message):
        responses.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/csp-report",
        "raw_path": b"/csp-report",
        "query_string": b"",
        "headers": [(b"content-type", b"application/csp-report")],  # chunked: no Content-Length
        "client": ("203.0.113.50", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }
    asyncio.run(app(scope, receive, send))
    assert next(m["status"] for m in responses if m["type"] == "http.response.start") == 413
    assert chunks_read <= csp_report.MAX_REPORT_BYTES // 1024 + 2  # never drains the endless stream
