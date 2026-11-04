#!/usr/bin/env python3
#
# tests/test_api_hardening.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Behavior of the app/api hardening: admin gating, logout, proxy trust, shared helpers."""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import signal as signal_api
from app.api.auth import require_admin
from app.db import sqlite as sqlite_db
from app.models.targets import TargetCreate, TargetPublic, target_row_to_dict
from app.models.users import hash_token, new_token
from app.services.notifications.signal_errors import SignalTimeout
from app.utils import auth_helpers, crypto
from app.utils.auth_helpers import get_user_by_token
from app.utils.client_ip import audit_client_ip, request_is_https, resolve_trusted_client_ip
from app.utils.constants import VALID_PDF_PAGE_SIZES
from app.utils.phone import mask_phone
from app.utils.uptime_metric import select_uptime_metric
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _request(peer: str | None, headers: dict[str, str] | None = None, scheme: str = "http") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw,
            "scheme": scheme,
            "query_string": b"",
            "server": ("testserver", 80),
            "client": (peer, 1234) if peer else None,
        }
    )


# --- client IP / proxy trust ------------------------------------------------------------------


class TestClientIp:
    def test_untrusted_peer_cannot_spoof_headers(self, monkeypatch):
        monkeypatch.delenv("UPMON_TRUSTED_PROXIES", raising=False)
        req = _request("203.0.113.9", {"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
        assert resolve_trusted_client_ip(req) == "203.0.113.9"
        assert audit_client_ip(req) == "203.0.113.9"

    def test_trusted_proxy_forwards_first_xff_entry(self, monkeypatch):
        monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "10.0.0.0/8")
        req = _request("10.1.2.3", {"X-Forwarded-For": "198.51.100.7, 10.1.2.3"})
        assert resolve_trusted_client_ip(req) == "198.51.100.7"

    def test_trusted_proxy_ipv6_client_and_fallbacks(self, monkeypatch):
        monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "127.0.0.1,::1")
        assert resolve_trusted_client_ip(_request("::1", {"X-Forwarded-For": "2001:db8::5"})) == "2001:db8::5"
        assert resolve_trusted_client_ip(_request("127.0.0.1", {"X-Real-IP": "192.0.2.4"})) == "192.0.2.4"
        assert resolve_trusted_client_ip(_request("127.0.0.1", {"Forwarded": 'for="192.0.2.60:4711";proto=https'})) == (
            "192.0.2.60"
        )

    def test_invalid_header_falls_back_to_peer(self, monkeypatch):
        monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "127.0.0.1")
        assert resolve_trusted_client_ip(_request("127.0.0.1", {"X-Forwarded-For": "not-an-ip"})) == "127.0.0.1"
        assert resolve_trusted_client_ip(_request("127.0.0.1")) == "127.0.0.1"

    def test_invalid_trusted_entries_fail_closed(self, monkeypatch):
        monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "*,garbage")
        req = _request("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})
        assert resolve_trusted_client_ip(req) == "203.0.113.9"

    def test_missing_or_unparsable_peer(self, monkeypatch):
        monkeypatch.delenv("UPMON_TRUSTED_PROXIES", raising=False)
        assert resolve_trusted_client_ip(_request(None)) == "unknown"
        assert audit_client_ip(_request("testclient")) == "unknown"

    def test_secure_flag_trusts_proto_header_only_from_trusted_peer(self, monkeypatch):
        monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "127.0.0.1")
        assert request_is_https(_request("127.0.0.1", {"X-Forwarded-Proto": "https"})) is True
        assert request_is_https(_request("203.0.113.9", {"X-Forwarded-Proto": "https"})) is False
        assert request_is_https(_request("203.0.113.9", scheme="https")) is True
        assert request_is_https(_request("127.0.0.1", {"X-Forwarded-Proto": "http"}, scheme="https")) is False

    def test_proto_uses_the_closest_proxy_value_like_the_forwarded_for_walk(self, monkeypatch):
        monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "127.0.0.1")
        # A client-forged left value must not beat the value appended by the trusted proxy.
        assert request_is_https(_request("127.0.0.1", {"X-Forwarded-Proto": "https, http"})) is False
        assert request_is_https(_request("127.0.0.1", {"X-Forwarded-Proto": "http, https"})) is True
        request = _request("127.0.0.1", {"X-Forwarded-Proto": "https"})
        request.scope["headers"].append((b"x-forwarded-proto", b"http"))
        assert request_is_https(request) is False

    def test_ipv4_mapped_ipv6_peer_and_chain_hops_match_ipv4_trusted_networks(self, monkeypatch):
        monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "10.0.0.0/8")
        mapped_peer = _request("::ffff:10.0.0.2", {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"})
        assert resolve_trusted_client_ip(mapped_peer) == "198.51.100.7"
        assert request_is_https(mapped_peer) is True
        chain = _request("10.0.0.2", {"X-Forwarded-For": "198.51.100.7, ::ffff:10.0.0.9"})
        assert resolve_trusted_client_ip(chain) == "198.51.100.7"
        # Mapping must not widen trust: a mapped address outside the network stays untrusted.
        outside = _request("::ffff:203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})
        assert resolve_trusted_client_ip(outside) == "::ffff:203.0.113.9"


# --- shared helpers ---------------------------------------------------------------------------


class TestSharedHelpers:
    def test_uptime_metric_priority_for_dict_and_row(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cases = [
            ((1, 1, 1), "http_up"),
            ((0, 1, 1), "ping_up"),
            ((0, 0, 1), "tcp_up"),
            ((0, 0, 0), None),
        ]
        for (http, ping, tcp), expected in cases:
            as_dict = {"enable_http_check": http, "enable_ping": ping, "enable_tcp_connect": tcp}
            row = conn.execute(
                "SELECT ? AS enable_http_check, ? AS enable_ping, ? AS enable_tcp_connect", (http, ping, tcp)
            ).fetchone()
            assert select_uptime_metric(as_dict) == expected
            assert select_uptime_metric(row) == expected
        assert select_uptime_metric({}) is None

    def test_mask_phone(self):
        assert mask_phone(None) == "***"
        assert mask_phone("") == "***"
        assert mask_phone("+4912") == "***"
        assert mask_phone("+4915112345678") == "+49****78"
        assert mask_phone("+" + "1" * 30) == "+11****11"

    def test_page_sizes_constant(self):
        assert frozenset({"a4", "letter"}) == VALID_PDF_PAGE_SIZES
        assert "A4" not in VALID_PDF_PAGE_SIZES

    def test_signal_uses_central_require_admin(self):
        assert signal_api.require_admin is require_admin


# --- API behavior -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)


def _make_user(conn, name: str, *, admin: bool) -> tuple[int, str]:
    user_id = sqlite_db.create_user(conn, username=name, password_hash="stub-hash", is_admin=admin)  # noqa: S106
    token, expires_at, max_expires_at = new_token()
    sqlite_db.issue_token(
        conn, user_id=user_id, token_hash=hash_token(token), expires_at=expires_at, max_expires_at=max_expires_at
    )
    return user_id, token


class _Session:
    def __init__(self, app, token: str, *, cookie: bool = False) -> None:
        self.client = TestClient(app)
        self.client.get("/ui/login")
        self.token = token
        self.cookie = cookie
        if cookie:
            self.client.cookies.set("token", token)

    def call(self, method: str, url: str, body=None, *, bearer: bool = True):
        headers = {"X-CSRF-Token": self.client.cookies.get("csrf_token")}
        if bearer:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.cookie:
            headers["Origin"] = "http://testserver"
        return self.client.request(method, url, json=body, headers=headers)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-api-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        from app.main import create_app

        app = create_app()
        conn = sqlite_db.connect(app.state.db_path)
        try:
            sqlite_db.init_schema(conn)
            admin_id, admin_token = _make_user(conn, "root", admin=True)
            user_id, user_token = _make_user(conn, "viewer", admin=False)
        finally:
            sqlite_db.close_connection(conn)
        yield {
            "app": app,
            "db_path": app.state.db_path,
            "admin_id": admin_id,
            "user_id": user_id,
            "admin": _Session(app, admin_token),
            "user": _Session(app, user_token),
            "admin_token": admin_token,
        }


def _db(env):
    return sqlite_db.connect(env["db_path"])


def test_https_login_uses_host_prefixed_http_only_cookie(env, monkeypatch):
    from app.api import users

    monkeypatch.setattr(users, "verify_password", lambda _password, _password_hash: True)
    client = TestClient(env["app"], base_url="https://testserver")
    try:
        client.get("/ui/login")
        response = client.post(
            "/login",
            json={"username": "root", "password": "test-password"},
            headers={"Origin": "https://testserver", "X-CSRF-Token": client.cookies.get("csrf_token")},
        )
        assert response.status_code == 200
        assert client.cookies.get("__Host-token") == response.json()["access_token"]
        assert client.cookies.get("token") is None
        cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith("__Host-token="))
        assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/" in cookie and "SameSite=lax" in cookie
    finally:
        client.close()


class TestLogout:
    def test_cookie_token_is_revoked_server_side(self, env):
        cookie_only = _Session(env["app"], env["admin_token"], cookie=True)
        resp = cookie_only.call("POST", "/logout", bearer=False)
        assert resp.status_code == 200
        assert resp.json()["detail"] == "Token revoked successfully"
        conn = _db(env)
        try:
            assert get_user_by_token(conn, env["admin_token"]) is None
        finally:
            sqlite_db.close_connection(conn)

    def test_bearer_token_is_revoked(self, env):
        resp = env["user"].call("POST", "/logout")
        assert resp.status_code == 200
        assert env["user"].call("GET", "/me").status_code == 401

    def test_logout_without_token_is_idempotent(self, env):
        anon = TestClient(env["app"])
        anon.get("/ui/login")
        resp = anon.post("/logout", headers={"X-CSRF-Token": anon.cookies.get("csrf_token")})
        assert resp.status_code == 200
        assert resp.json()["detail"] == "No token to revoke"


class TestUserAdmin:
    def test_admin_cannot_remove_own_admin_flag(self, env):
        resp = env["admin"].call(
            "PUT", f"/users/{env['admin_id']}", {"username": "root", "is_admin": False, "can_close_alerts": True}
        )
        assert resp.status_code == 403
        assert "own admin" in resp.json()["detail"]

    def test_admin_may_keep_own_flag_and_demote_others(self, env):
        keep = env["admin"].call(
            "PUT", f"/users/{env['admin_id']}", {"username": "root", "is_admin": True, "can_close_alerts": True}
        )
        assert keep.status_code == 200
        other = env["admin"].call(
            "PUT", f"/users/{env['user_id']}", {"username": "viewer", "is_admin": False, "can_close_alerts": False}
        )
        assert other.status_code == 200

    def test_non_admin_gets_403(self, env):
        resp = env["user"].call(
            "PUT", f"/users/{env['admin_id']}", {"username": "root", "is_admin": False, "can_close_alerts": False}
        )
        assert resp.status_code == 403


class TestRecipients:
    def _create(self, env):
        resp = env["admin"].call("POST", "/recipients", {"name": "A", "email": "a@example.com", "is_enabled": True})
        assert resp.status_code == 201, resp.text
        return resp.json()["recipient"]["id"]

    def test_invalid_phone_is_rejected_by_model_not_500(self, env):
        rid = self._create(env)
        assert env["admin"].call("PATCH", f"/recipients/{rid}", {"phone": "abc"}).status_code == 422
        assert env["admin"].call("POST", "/recipients", {"name": "B", "phone": "abc"}).status_code == 422

    def test_at_least_one_channel_is_required_and_a_switched_off_channel_is_cleared(self, env):
        no_channel = env["admin"].call("POST", "/recipients", {"name": "B"})
        assert no_channel.status_code == 422
        both = env["admin"].call(
            "POST", "/recipients", {"name": "C", "email": "c@example.com", "phone": "+4915112345678"}
        )
        assert both.status_code == 201, both.text
        rid = both.json()["recipient"]["id"]
        # The dialog sends null for a switched-off channel: the stored value is cleared, the other stays.
        only_email = env["admin"].call("PATCH", f"/recipients/{rid}", {"phone": None, "email": "c@example.com"})
        assert only_email.status_code == 200, only_email.text
        assert only_email.json()["recipient"]["phone"] is None
        assert only_email.json()["recipient"]["email"] == "c@example.com"
        none_left = env["admin"].call("PATCH", f"/recipients/{rid}", {"phone": None, "email": None})
        assert none_left.status_code == 400
        assert env["admin"].call("GET", f"/recipients/{rid}").json()["email"] == "c@example.com"

    def test_normalize_failure_maps_to_400(self):
        from fastapi import HTTPException

        from app.api.recipients import _normalize_phone_or_400

        with pytest.raises(HTTPException) as exc:
            _normalize_phone_or_400("12+34")
        assert exc.value.status_code == 400

    def test_verify_signal_rejects_oversized_input(self, env):
        too_many = env["admin"].call("POST", "/recipients/verify-signal", ["+4915112345678"] * 51)
        assert too_many.status_code == 422
        too_long = env["admin"].call("POST", "/recipients/verify-signal", ["+" + "1" * 40])
        assert too_long.status_code == 422


class TestSignal:
    def test_status_degrades_on_timeout(self, env, monkeypatch):
        class _Backend:
            def status(self):
                raise SignalTimeout("slow")

        monkeypatch.setattr(signal_api, "get_signal_backend", _Backend)
        resp = env["user"].call("GET", "/signal/status")
        assert resp.status_code == 200
        assert resp.json()["reachable"] is False
        # The internal timeout detail must not reach the client.
        assert resp.json()["error"] == "Signal service unavailable"
        assert "slow" not in resp.text

    def test_send_requires_admin(self, env):
        resp = env["user"].call("POST", "/signal/send", {"recipient": "+4915112345678", "message": "x"})
        assert resp.status_code == 403


class TestTargetRowMapping:
    def test_mapping_matches_public_model_and_hides_password(self, tmp_path: Path):
        conn = sqlite_db.connect(tmp_path / "t.db")
        try:
            sqlite_db.init_schema(conn)
            values = TargetCreate(name="t", host="example.com").model_dump()
            values["http_username"] = "u"
            values["http_password"] = "s3cret"  # noqa: S105 - throwaway test value
            row = sqlite_db.get_target(conn, sqlite_db.create_target(conn, values))
        finally:
            sqlite_db.close_connection(conn)
        mapped = target_row_to_dict(row)
        assert set(mapped) == set(TargetPublic.model_fields)
        assert mapped["has_http_password"] is True
        assert "http_password" not in mapped
        assert "s3cret" not in str(TargetPublic(**mapped).model_dump())
        assert mapped["group"] is None
        assert mapped["http_port"] is None
        assert mapped["tcp_ports"] == "80"


class TestWriteAccessIsAdminOnly:
    def test_non_admin_can_read_but_not_mutate_groups_and_recipients(self, env):
        admin, user = env["admin"], env["user"]
        group = admin.call("POST", "/groups", {"name": "core"})
        assert group.status_code == 201, group.text
        gid = group.json()["id"]
        recipient = admin.call("POST", "/recipients", {"name": "A", "email": "a@example.com"})
        rid = recipient.json()["recipient"]["id"]

        assert user.call("GET", "/groups").status_code == 200
        assert user.call("GET", "/recipients").status_code == 200
        assert user.call("POST", "/groups", {"name": "x"}).status_code == 403
        assert user.call("PATCH", f"/groups/{gid}", {"name": "y"}).status_code == 403
        assert user.call("DELETE", f"/groups/{gid}").status_code == 403
        assert user.call("POST", "/recipients", {"name": "B", "email": "b@example.com"}).status_code == 403
        assert user.call("PATCH", f"/recipients/{rid}", {"name": "C"}).status_code == 403
        assert user.call("DELETE", f"/recipients/{rid}").status_code == 403

        assert admin.call("PATCH", f"/groups/{gid}", {"name": "renamed"}).status_code == 200
        assert admin.call("DELETE", f"/recipients/{rid}").status_code == 204


def _preview_target(env, profile: str, address: str, **extra):
    response = env["admin"].call("POST", "/targets/preview", {"profile": profile, "address": address, **extra})
    assert response.status_code == 200, response.text
    return response.json()


class TestTargetWizardContracts:
    @pytest.mark.parametrize(
        ("profile", "address", "checks", "cert_enabled", "expected_url"),
        [
            ("website", "https://Example.COM/", (False, True, False), True, "https://example.com/"),
            (
                "website",
                "http://Example.COM:8080/path?q=One",
                (False, True, False),
                False,
                "http://example.com:8080/path?q=One",
            ),
            (
                "api",
                "https://example.com:8443/api/health?check=One",
                (False, True, False),
                True,
                "https://example.com:8443/api/health?check=One",
            ),
            ("server", "Example.COM.", (True, False, False), False, None),
        ],
    )
    def test_profiles_produce_active_checks_and_preserve_endpoints(
        self, env, profile, address, checks, cert_enabled, expected_url
    ):
        values = _preview_target(env, profile, address)["values"]
        assert values["is_enabled"] is True
        assert (values["enable_ping"], values["enable_http_check"], values["enable_tcp_connect"]) == checks
        assert values["enable_cert_expiration"] is cert_enabled
        assert values["interval_seconds"] == 60
        assert values["max_retries"] == 2
        assert values["retention_days"] == 365
        assert values["auto_close_alert"] is True
        if expected_url:
            assert values["http_prefer_head"] is False
            assert values["http_url"] == expected_url
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        assert created.json()["enable_cert_expiration"] is cert_enabled
        assert created.json()["http_url"] == expected_url
        if profile == "server":
            assert created.json()["host"] == "example.com"

    def test_tcp_profile_requires_explicit_ports(self, env):
        missing = env["admin"].call("POST", "/targets/preview", {"profile": "tcp", "address": "example.com"})
        assert missing.status_code == 422
        values = _preview_target(env, "tcp", "example.com", tcp_ports="443, 22")["values"]
        assert values["enable_tcp_connect"] is True
        assert values["enable_ping"] is False
        assert values["enable_http_check"] is False
        assert values["tcp_ports"] == "22,443"

    def test_tcp_only_target_saves_without_http_fields_and_runs_no_http_check(self, env):
        host = "tcp-only.example.com"
        payload = {"name": "SSH", "host": host, "tcp_host": host, "enable_tcp_connect": True, "tcp_ports": "22"}
        created = env["admin"].call("POST", "/targets", payload)
        assert created.status_code == 201, created.text
        body = created.json()
        assert (body["enable_http_check"], body["enable_cert_expiration"], body["enable_ping"]) == (False, False, False)
        assert body["http_url"] is None
        # The stored HTTP defaults are inert: only the enabled TCP check is scheduled and reported.
        with closing(_db(env)) as conn:
            row = sqlite_db.get_target(conn, body["id"])
            assert not row["enable_http_check"] and not row["enable_cert_expiration"]
        conflicts = _preview_target(env, "tcp", "tcp-only.example.com", tcp_ports="22")["conflicts"]
        assert [item["check_type"] for item in conflicts] == ["tcp"]

    def test_enabled_target_requires_at_least_one_check(self, env):
        values = TargetCreate(name="Empty", host="example.com").model_dump(mode="json")
        response = env["admin"].call("POST", "/targets", values)
        assert response.status_code == 422
        assert "check" in response.text.lower()

    def test_create_blocks_exact_duplicate_but_allows_distinct_services(self, env):
        first = _preview_target(env, "website", "https://Example.COM/")["values"]
        first["name"] = "Primary"
        created = env["admin"].call("POST", "/targets", first)
        assert created.status_code == 201, created.text
        target_id = created.json()["id"]

        duplicate = _preview_target(env, "website", "https://example.com:443/")["values"]
        duplicate["name"] = "Other name"
        conflict = env["admin"].call("POST", "/targets", duplicate)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["conflicts"][0]["id"] == target_id
        assert conflict.json()["detail"]["conflicts"][0]["classification"] == "exact"

        service = _preview_target(env, "tcp", "example.com", tcp_ports="22")["values"]
        assert env["admin"].call("POST", "/targets", service).status_code == 201
        path = _preview_target(env, "api", "https://example.com/api/health")["values"]
        assert env["admin"].call("POST", "/targets", path).status_code == 201

    @pytest.mark.parametrize(
        ("original", "equivalent"),
        [
            ("https://Example.COM./Path?q=One", "https://example.com:443/Path?q=One"),
            ("https://[2001:db8::1]/", "https://[2001:db8::1]:443/"),
            ("https://bücher.example/", "https://xn--bcher-kva.example:443/"),
        ],
    )
    def test_duplicate_identity_normalizes_host_and_standard_port(self, env, original, equivalent):
        first = _preview_target(env, "website", original)["values"]
        second = _preview_target(env, "website", equivalent)["values"]
        assert env["admin"].call("POST", "/targets", first).status_code == 201
        assert env["admin"].call("POST", "/targets", second).status_code == 409

    def test_http_path_query_and_scheme_are_distinct(self, env):
        addresses = [
            "https://example.com/Path?q=One",
            "https://example.com/path?q=One",
            "https://example.com/Path?q=Two",
            "http://example.com/Path?q=One",
        ]
        for address in addresses:
            values = _preview_target(env, "api", address)["values"]
            response = env["admin"].call("POST", "/targets", values)
            assert response.status_code == 201, (address, response.text)

    def test_preview_returns_overlap_without_secrets(self, env):
        combined = _preview_target(env, "custom", "https://example.com/", overrides={"enable_ping": True})["values"]
        combined["name"] = "Combined"
        assert env["admin"].call("POST", "/targets", combined).status_code == 201
        preview = _preview_target(env, "server", "example.com")
        assert any(item["classification"] == "overlap" for item in preview["conflicts"])
        assert all(set(item) == {"id", "name", "classification", "check_type"} for item in preview["conflicts"])
        assert "http_password" not in preview["values"]

    @pytest.mark.parametrize(
        ("address", "expected_http", "expected_ping"),
        [("https://example.com/health", True, False), ("example.com", False, True)],
    )
    def test_custom_profile_combines_checks_with_explicit_tcp_ports(self, env, address, expected_http, expected_ping):
        values = _preview_target(
            env,
            "custom",
            address,
            tcp_ports="22, 443",
            overrides={"enable_tcp_connect": True},
        )["values"]
        assert values["enable_http_check"] is expected_http
        assert values["enable_ping"] is expected_ping
        assert values["enable_tcp_connect"] is True
        assert values["tcp_ports"] == "22,443"
        assert values["tcp_host"] == "example.com"
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        assert created.json()["enable_http_check"] is expected_http
        assert created.json()["enable_ping"] is expected_ping
        assert created.json()["enable_tcp_connect"] is True
        assert created.json()["tcp_ports"] == "22,443"

    def test_custom_tcp_check_requires_explicit_ports(self, env):
        response = env["admin"].call(
            "POST",
            "/targets/preview",
            {"profile": "custom", "address": "example.com", "overrides": {"enable_tcp_connect": True}},
        )
        assert response.status_code == 422

    def test_target_and_recipients_are_atomic(self, env):
        recipient = env["admin"].call("POST", "/recipients", {"name": "Ops", "email": "ops@example.com"})
        assert recipient.status_code == 201, recipient.text
        recipient_id = recipient.json()["recipient"]["id"]
        values = _preview_target(env, "server", "first.example.com")["values"]
        values["alert_recipient_ids"] = [recipient_id]
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        with closing(_db(env)) as conn:
            assert sqlite_db.get_target_recipient_ids(conn, created.json()["id"]) == [recipient_id]
            before = len(sqlite_db.list_targets(conn))

        invalid = _preview_target(env, "server", "second.example.com")["values"]
        invalid["alert_recipient_ids"] = [999999]
        rejected = env["admin"].call("POST", "/targets", invalid)
        assert rejected.status_code == 422
        with closing(_db(env)) as conn:
            assert len(sqlite_db.list_targets(conn)) == before

    def test_patch_preserves_password_and_manual_cert_choice(self, env):
        values = _preview_target(env, "website", "https://example.com/")["values"]
        values.update(
            name="Authenticated",
            http_username="viewer",
            http_password="fixture-password",  # noqa: S106 - throwaway test value
            http_basic_auth_enabled=True,
            auto_close_alert=False,
        )
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        target_id = created.json()["id"]
        with closing(_db(env)) as conn:
            stored_password = sqlite_db.get_target(conn, target_id)["http_password"]
        changed = env["admin"].call("PATCH", f"/targets/{target_id}", {"enable_cert_expiration": False})
        assert changed.status_code == 200, changed.text
        unchanged = env["admin"].call("PATCH", f"/targets/{target_id}", {"interval_seconds": 120})
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["enable_cert_expiration"] is False
        assert unchanged.json()["auto_close_alert"] is False
        assert "fixture-password" not in unchanged.text
        enabled_close = env["admin"].call("PATCH", f"/targets/{target_id}", {"auto_close_alert": True})
        assert enabled_close.status_code == 200, enabled_close.text
        assert enabled_close.json()["auto_close_alert"] is True
        invalid_cert = env["admin"].call(
            "PATCH", f"/targets/{target_id}", {"http_url": "http://example.com/", "enable_cert_expiration": True}
        )
        assert invalid_cert.status_code == 422
        with closing(_db(env)) as conn:
            row = sqlite_db.get_target(conn, target_id)
            assert row["http_password"] == stored_password
            assert row["http_url"].startswith("https://")

    def test_patch_validates_basic_auth_against_the_merged_state(self, env):
        values = _preview_target(env, "website", "https://example.com/")["values"]
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        path = f"/targets/{created.json()['id']}"
        assert env["admin"].call("PATCH", path, {"http_basic_auth_enabled": True}).status_code == 422
        assert (
            env["admin"].call("PATCH", path, {"http_basic_auth_enabled": True, "http_username": "viewer"}).status_code
            == 422
        )
        complete = {"http_basic_auth_enabled": True, "http_username": "viewer", "http_password": "fixture-password"}
        assert env["admin"].call("PATCH", path, complete).status_code == 200
        # Stored credentials complete a later partial patch; clearing them disables auth consistently.
        assert env["admin"].call("PATCH", path, {"http_username": "other"}).status_code == 200
        cleared = env["admin"].call("PATCH", path, {"clear_http_password": True})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["http_basic_auth_enabled"] is False

    def test_explicit_cert_choice_on_create_is_preserved(self, env):
        values = _preview_target(env, "website", "https://example.com/")["values"]
        values["enable_cert_expiration"] = False
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        assert created.json()["enable_cert_expiration"] is False

    def test_non_admin_cannot_create_or_open_wizard(self, env):
        preview = env["user"].call("POST", "/targets/preview", {"profile": "server", "address": "example.com"})
        assert preview.status_code == 403
        values = _preview_target(env, "server", "example.com")["values"]
        assert env["user"].call("POST", "/targets", values).status_code == 403
        assert env["user"].call("GET", "/ui/targets/new").status_code == 403
        assert env["admin"].call("GET", "/ui/targets/new").status_code == 200

    def test_cookie_auth_requires_csrf_and_same_origin_for_api_writes(self, env):
        cookie_session = _Session(env["app"], env["admin_token"], cookie=True)
        try:
            values = _preview_target(env, "server", "cookie.example.com")["values"]
            csrf = cookie_session.client.cookies.get("csrf_token")
            no_origin = cookie_session.client.post("/targets", json=values, headers={"X-CSRF-Token": csrf})
            assert no_origin.status_code == 403
            hostile_origin = cookie_session.client.post(
                "/targets",
                json=values,
                headers={"Origin": "https://attacker.example", "X-CSRF-Token": csrf},
            )
            assert hostile_origin.status_code == 403
            allowed = cookie_session.call("POST", "/targets", values, bearer=False)
            assert allowed.status_code == 201, allowed.text
            assert cookie_session.client.get("/me").status_code == 200
        finally:
            cookie_session.client.close()

    def test_edit_opens_complete_editor_without_restarting_wizard(self, env):
        values = _preview_target(env, "website", "http://example.com:8080/health?x=1")["values"]
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        new_page = env["admin"].call("GET", "/ui/targets/new")
        edit_page = env["admin"].call("GET", f"/ui/targets/{created.json()['id']}/edit")
        assert 'id="target-wizard-form"' in new_page.text
        assert edit_page.status_code == 200, edit_page.text
        assert 'id="target-form"' in edit_page.text
        assert 'id="target-wizard-form"' not in edit_page.text
        assert "http://example.com:8080/health?x=1" in edit_page.text

    def test_concurrent_exact_create_has_one_winner(self, env):
        values = _preview_target(env, "server", "race.example.com")["values"]
        gate = Barrier(2)
        sessions = [_Session(env["app"], env["admin_token"]) for _ in range(2)]

        def create(session):
            gate.wait(timeout=5)
            return session.call("POST", "/targets", values).status_code

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(create, session) for session in sessions]
                statuses = sorted(future.result(timeout=10) for future in futures)
        finally:
            for session in sessions:
                session.client.close()
        assert statuses == [201, 409]


class TestFailClosedBearerAuth:
    """A PRESENT Authorization header is fail-closed: it must never fall back to
    cookie auth, even when a valid session cookie rides along (DECISIONS B9, P0).
    """

    def test_valid_bearer_header_authenticates(self, env):
        resp = env["admin"].call("GET", "/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == "root"

    def test_malformed_header_does_not_fall_back_to_valid_cookie(self, env):
        # Cookie is a VALID admin token; the Authorization header is malformed
        # (not "Bearer <token>"). Fail-closed => 401, no cookie fallback.
        client = TestClient(env["app"])
        client.get("/ui/login")
        client.cookies.set("token", env["admin_token"])
        resp = client.get("/me", headers={"Authorization": env["admin_token"]})
        try:
            assert resp.status_code == 401
        finally:
            client.close()

    def test_invalid_bearer_token_does_not_fall_back_to_valid_cookie(self, env):
        # Well-formed "Bearer <token>" but the token is bogus; a valid cookie is
        # also present. Fail-closed => 401, no cookie fallback.
        client = TestClient(env["app"])
        client.get("/ui/login")
        client.cookies.set("token", env["admin_token"])
        resp = client.get("/me", headers={"Authorization": "Bearer not-a-real-token"})
        try:
            assert resp.status_code == 401
        finally:
            client.close()

    def test_revoked_bearer_token_yields_401(self, env):
        # Revoke the admin's token server-side, then present it in the header.
        conn = _db(env)
        try:
            sqlite_db.revoke_token(conn, token_hash=hash_token(env["admin_token"]))
        finally:
            sqlite_db.close_connection(conn)
        resp = env["admin"].call("GET", "/me")
        assert resp.status_code == 401

    def test_no_header_valid_cookie_on_ui_route_works(self, env):
        # No Authorization header + valid cookie on a UI route => cookie auth
        # preserved (admin wizard renders).
        client = TestClient(env["app"])
        client.get("/ui/login")
        client.cookies.set("token", env["admin_token"])
        resp = client.get("/ui/targets/new")
        try:
            assert resp.status_code == 200
            assert 'id="target-wizard-form"' in resp.text
        finally:
            client.close()

    def test_no_header_no_cookie_api_is_401_and_ui_redirects(self, env):
        # No Authorization header and no cookie: API => 401, UI => redirect to login.
        anon = TestClient(env["app"], follow_redirects=False)
        anon.get("/ui/login")
        try:
            assert anon.get("/me").status_code == 401
            ui = anon.get("/ui/targets/new")
            assert ui.status_code == 302
            assert ui.headers["location"].startswith("/ui/login")
        finally:
            anon.close()


def test_server_launch_does_not_let_uvicorn_trust_arbitrary_proxy_headers(monkeypatch):
    import sys

    import app.__main__ as entry
    import app.main

    captured = {}

    class _FakeServer:
        def __init__(self, config):
            pass

        def run(self):
            pass

    fake = SimpleNamespace(
        Config=lambda *_a, **kw: captured.update(kw),
        Server=_FakeServer,
    )
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setattr(app.main, "create_app", object)
    entry.main()
    # uvicorn would rewrite client/scheme from any peer; utils.client_ip owns that decision.
    assert captured.get("proxy_headers") is False
    assert captured.get("forwarded_allow_ips") in (None, "")


def test_forged_forwarded_chain_is_ignored_unless_peer_is_trusted(monkeypatch):
    monkeypatch.setenv("UPMON_TRUSTED_PROXIES", "10.0.0.0/8")
    forged = {"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 10.0.0.2, 10.0.0.3"}
    assert resolve_trusted_client_ip(_request("10.0.0.4", forged)) == "198.51.100.7"
    assert resolve_trusted_client_ip(_request("203.0.113.9", forged)) == "203.0.113.9"


@pytest.mark.parametrize(
    ("level", "expected"), [(logging.INFO, False), (logging.WARNING, False), (logging.DEBUG, True)]
)
def test_uvicorn_access_lines_only_pass_at_debug_and_are_labelled_debug(level, expected):
    import io

    import app.__main__ as entry

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(entry.UVICORN_LOG_CONFIG["filters"]["access_debug"]["()"]())
    access = logging.getLogger("test.access")
    access.addHandler(handler)
    access.propagate = False
    access.setLevel(logging.INFO)  # as uvicorn.run(log_level="info") leaves the real access logger
    root = logging.getLogger()
    previous = root.level
    root.setLevel(level)
    try:
        access.info("GET /x 200")
    finally:
        root.setLevel(previous)
        access.removeHandler(handler)
    assert stream.getvalue() == ("DEBUG GET /x 200\n" if expected else "")


def test_patch_rename_of_ping_tcp_target_next_to_ping_only_target_succeeds(env):
    ping_only = _preview_target(env, "server", "shared.example")["values"]
    ping_only["name"] = "Ping only"
    assert env["admin"].call("POST", "/targets", ping_only).status_code == 201
    combined = _preview_target(env, "custom", "shared.example", tcp_ports="22", overrides={"enable_tcp_connect": True})[
        "values"
    ]
    combined["name"] = "Ping and TCP"
    created = env["admin"].call("POST", "/targets", combined)
    assert created.status_code == 201, created.text
    renamed = env["admin"].call("PATCH", f"/targets/{created.json()['id']}", {"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text


def test_patch_making_two_tcp_targets_identical_is_rejected(env):
    ids = []
    for name, port in (("SSH", "22"), ("Alt SSH", "2222")):
        values = _preview_target(env, "tcp", "tcp.example", tcp_ports=port)["values"]
        values["name"] = name
        created = env["admin"].call("POST", "/targets", values)
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    conflict = env["admin"].call("PATCH", f"/targets/{ids[1]}", {"tcp_ports": "22"})
    assert conflict.status_code == 409, conflict.text


_safe_redirect_target = auth_helpers._safe_redirect_target  # noqa: SLF001 - the unit under test


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/ui/targets", "/ui/targets"),
        ("/ui/targets?name=a%20b&page=2", "/ui/targets?name=a%20b&page=2"),
        ("/", "/"),
        ("", "/ui/login"),
        ("https://evil.example/", "/ui/login"),
        ("http://evil.example", "/ui/login"),
        ("javascript:alert(1)", "/ui/login"),
        ("evil.example/path", "/ui/login"),
        ("//evil.example", "/ui/login"),
        ("/\\evil.example", "/ui/login"),
        ("/ui/\\evil", "/ui/login"),
        ("/%5Cevil.example", "/ui/login"),
        ("/%5cevil.example", "/ui/login"),
        ("/%255Cevil.example", "/ui/login"),
        ("/%2F%2Fevil.example", "/ui/login"),
        ("/%2f/evil.example", "/ui/login"),
        ("/\tevil.example", "/ui/login"),
        ("/\nevil.example", "/ui/login"),
        ("/\revil.example", "/ui/login"),
        ("/%09/evil.example", "/ui/login"),
        ("/%0a/evil.example", "/ui/login"),
        ("/%0d/evil.example", "/ui/login"),
        ("/ui/%00x", "/ui/login"),
        ("/\x7fx", "/ui/login"),
    ],
)
def test_safe_redirect_target_rejects_open_redirect_forms(target, expected):
    assert _safe_redirect_target(target) == expected


def test_safe_redirect_target_uses_the_given_default():
    assert _safe_redirect_target("/\\evil.example", "/ui/change-password") == "/ui/change-password"


def test_redirect_helpers_fall_back_for_an_unsafe_target(monkeypatch):
    monkeypatch.setattr(auth_helpers, "resolve_user_optional", lambda *_a, **_k: None)
    request = SimpleNamespace(url=SimpleNamespace(path="/ui/x"))
    assert (
        auth_helpers.resolve_user_or_htmx_redirect(request, None, "/\\evil.example").headers["HX-Redirect"]
        == "/ui/login"
    )
    assert auth_helpers.resolve_user_or_redirect(request, None, "/\\evil.example").headers["location"] == "/ui/login"
