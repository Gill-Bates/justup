#!/usr/bin/env python3
#
# tests/test_public_egress.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Public egress lookup (Settings > Network): all network access is mocked, nothing leaves the machine."""

from __future__ import annotations

import datetime as dt
import errno
import io
import socket
import ssl
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.services import public_egress as pe
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests.test_api_hardening import _encryption_key, env  # noqa: F401 - fixtures reused for the API tests

V4 = "93.184.216.34"
V6 = "2606:2800:220:1:248:1893:25c8:1946"
V4_OTHER = "198.51.100.7"  # documentation range is non-global; used only where a *different* value is needed
V4_ALT = "8.8.4.4"
HOSTS = {p.host for providers in pe.PROVIDERS.values() for p in providers}


def reply(body: bytes | str, status: int = 200, extra: str = "") -> bytes:
    data = body.encode() if isinstance(body, str) else body
    head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(data)}\r\n{extra}\r\n".encode()
    return head + data


class FakeSock:
    def __init__(self, response: bytes) -> None:
        self._response = response
        self.sent = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, *_args, **_kwargs):
        return io.BytesIO(self._response)

    def settimeout(self, _value) -> None:  # pragma: no cover - interface only
        pass

    def close(self) -> None:
        pass


class Net:
    """Scripted network: ``answers[(family, host)]`` is response bytes or an exception to raise on connect."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers: dict[tuple[str, str], bytes | BaseException] = {}
        self.dns: dict[tuple[str, str], list[str] | BaseException] = {}
        self.connects: list[tuple[str, str, str]] = []
        monkeypatch.setattr(pe.socket, "getaddrinfo", self.resolve_scripted)
        monkeypatch.setattr(pe, "_open_tls", self.open_tls)

    def resolve_scripted(self, host, _port, family=0, *_args, **_kwargs):
        key = ("ipv6" if family == socket.AF_INET6 else "ipv4", host)
        answer = self.dns.get(key)
        if answer is None:
            answer = [V6 if key[0] == "ipv6" else V4]
        if isinstance(answer, BaseException):
            raise answer
        return [(family, socket.SOCK_STREAM, 6, "", (a, 443)) for a in answer]

    def open_tls(self, family, address, host, _timeout):
        self.connects.append((family, address, host))
        answer = self.answers.get((family, host))
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise OSError(errno.ECONNREFUSED, "no scripted answer")
        return FakeSock(answer)

    def set_family(self, family: str, body, **kw) -> None:
        for provider in pe.PROVIDERS[family]:
            self.answers[(family, provider.host)] = reply(body, **kw) if not isinstance(body, BaseException) else body

    def down(self, family: str, exc: BaseException) -> None:
        self.set_family(family, exc)


@pytest.fixture(autouse=True)
def _fresh_state():
    pe.reset_state()
    yield
    pe.reset_state()


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    return Net(monkeypatch)


def no_route() -> OSError:
    return OSError(errno.ENETUNREACH, "Network is unreachable")


# ─── 1-5: success per family and combinations ────────────────────────────────


def test_ipv4_success(net):
    net.set_family("ipv4", V4 + "\n")
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "available"
    assert result["ipv4"]["address"] == V4
    assert result["ipv4"]["sources"] == [p.host for p in pe.PROVIDERS["ipv4"]]
    assert result["cached"] is False


def test_ipv6_success(net):
    net.down("ipv4", no_route())
    net.set_family("ipv6", V6)
    result = pe.get_public_egress()
    assert result["ipv6"]["status"] == "available"
    assert result["ipv6"]["address"] == V6


def test_ipv4_only_marks_ipv6_unavailable_without_blocking_ipv4(net):
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert (result["ipv4"]["status"], result["ipv4"]["address"]) == ("available", V4)
    assert (result["ipv6"]["status"], result["ipv6"]["address"]) == ("unavailable", None)
    assert result["ipv6"]["reason"] == "no_route"


def test_ipv6_only_marks_ipv4_unavailable(net):
    net.dns[("ipv4", pe.PROVIDERS["ipv4"][0].host)] = socket.gaierror(socket.EAI_NONAME, "no A record")
    net.dns[("ipv4", pe.PROVIDERS["ipv4"][1].host)] = socket.gaierror(socket.EAI_NONAME, "no A record")
    net.set_family("ipv6", V6)
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "unavailable"
    assert result["ipv6"]["address"] == V6


def test_dual_stack(net):
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    result = pe.get_public_egress()
    assert result["ipv4"]["address"] == V4
    assert result["ipv6"]["address"] == V6


def test_ipv6_lookup_never_uses_ipv4_and_vice_versa(net):
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    pe.get_public_egress()
    for family, address, host in net.connects:
        provider_family = "ipv6" if host in {p.host for p in pe.PROVIDERS["ipv6"]} else "ipv4"
        assert family == provider_family
        assert (":" in address) == (family == "ipv6")


def test_one_provider_down_still_available_from_the_other(net):
    first, second = pe.PROVIDERS["ipv4"]
    net.answers[("ipv4", first.host)] = OSError(errno.ECONNREFUSED, "refused")
    net.answers[("ipv4", second.host)] = reply(V4)
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "available"
    assert result["ipv4"]["sources"] == [second.host]


# ─── 6-9: failures ───────────────────────────────────────────────────────────


def test_no_internet_is_unavailable_for_both(net):
    net.down("ipv4", no_route())
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == result["ipv6"]["status"] == "unavailable"
    assert result["ipv4"]["address"] is None


def test_dns_failure_is_a_failed_lookup_not_an_address(net):
    for family in ("ipv4", "ipv6"):
        for provider in pe.PROVIDERS[family]:
            net.dns[(family, provider.host)] = socket.gaierror(socket.EAI_AGAIN, "temporary failure")
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "failed"
    assert result["ipv4"]["reason"] == "dns_failed"
    assert result["ipv4"]["address"] is None


def test_ipv6_without_route_does_not_affect_ipv4(net):
    net.set_family("ipv4", V4)
    net.down("ipv6", OSError(errno.EHOSTUNREACH, "No route to host"))
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "available"
    assert result["ipv6"]["status"] == "unavailable"


def test_timeout_is_failed(net):
    net.down("ipv4", TimeoutError("timed out"))
    net.set_family("ipv6", V6)
    result = pe.get_public_egress()
    assert (result["ipv4"]["status"], result["ipv4"]["reason"]) == ("failed", "timeout")
    assert result["ipv6"]["status"] == "available"


def test_overall_budget_bounds_a_hanging_provider(net, monkeypatch):
    release = threading.Event()
    real = net.open_tls

    def slow(family, address, host, timeout):
        if family == "ipv6":
            release.wait(5)
        return real(family, address, host, timeout)

    monkeypatch.setattr(pe, "_open_tls", slow)
    monkeypatch.setattr(pe, "OVERALL_BUDGET_SECONDS", 0.3)
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    started = time.monotonic()
    result = pe.get_public_egress()
    release.set()
    assert time.monotonic() - started < 3
    assert result["ipv4"]["address"] == V4
    assert (result["ipv6"]["status"], result["ipv6"]["reason"]) == ("failed", "timeout")


def _threads(prefix: str) -> int:
    return sum(1 for t in threading.enumerate() if t.name.startswith(prefix))


def test_hanging_dns_cannot_pile_up_threads_or_block_the_service(net, monkeypatch):
    release = threading.Event()
    started = []
    real = net.resolve_scripted

    def hanging(host, port, family=0, *args, **kwargs):
        started.append(host)
        release.wait(10)
        return real(host, port, family, *args, **kwargs)

    monkeypatch.setattr(pe.socket, "getaddrinfo", hanging)
    monkeypatch.setattr(pe, "DNS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(pe, "OVERALL_BUDGET_SECONDS", 2.0)
    monkeypatch.setattr(pe, "MIN_REFRESH_INTERVAL_SECONDS", 0.0)
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    try:
        for _ in range(6):  # repeated refreshes while all four resolver calls stay blocked
            began = time.monotonic()
            result = pe.get_public_egress(refresh=True)
            assert time.monotonic() - began < 2.0
            assert result["ipv4"]["status"] == result["ipv6"]["status"] == "failed"
        assert result["ipv4"]["reason"] == "busy"  # later refreshes are refused, not queued
        assert len(started) == 4  # only the first refresh reached the resolver
        assert _threads("public-egress-dns") <= pe.MAX_PENDING_DNS
        assert _threads("public-egress_") <= pe.MAX_PENDING_FETCHES
    finally:
        release.set()
    monkeypatch.setattr(pe.socket, "getaddrinfo", real)
    deadline = time.monotonic() + 5
    while True:  # slots are freed by the released threads; the service then works again
        recovered = pe.get_public_egress(refresh=True)
        if recovered["ipv4"]["status"] == "available" or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert recovered["ipv4"]["address"] == V4 and recovered["ipv6"]["address"] == V6


# ─── 10-12: wrong or non-public answers ──────────────────────────────────────


@pytest.mark.parametrize(
    ("family", "body"),
    [("ipv4", V6), ("ipv6", V4), ("ipv6", "::ffff:93.184.216.34"), ("ipv6", "64:ff9b::5db8:d822")],
)
def test_wrong_address_family_is_discarded(net, family, body):
    other = "ipv6" if family == "ipv4" else "ipv4"
    net.set_family(family, body)
    net.down(other, no_route())
    result = pe.get_public_egress()
    assert result[family]["status"] == "failed"
    assert result[family]["address"] is None
    assert result[family]["reason"] == "wrong_family"


@pytest.mark.parametrize(
    ("family", "body"),
    [
        ("ipv4", "10.1.2.3"),
        ("ipv4", "172.17.0.2"),
        ("ipv4", "192.168.1.5"),
        ("ipv4", "100.64.0.9"),
        ("ipv4", "127.0.0.1"),
        ("ipv4", "169.254.1.1"),
        ("ipv4", "0.0.0.0"),  # noqa: S104
        ("ipv4", "224.0.0.1"),
        ("ipv4", "192.0.2.10"),
        ("ipv6", "::1"),
        ("ipv6", "::"),
        ("ipv6", "fe80::1"),
        ("ipv6", "fd12:3456::1"),
        ("ipv6", "ff02::1"),
        ("ipv6", "2001:db8::1"),
    ],
)
def test_private_and_special_answers_are_never_shown(net, family, body):
    other = "ipv6" if family == "ipv4" else "ipv4"
    net.set_family(family, body)
    net.down(other, no_route())
    result = pe.get_public_egress()
    assert result[family]["status"] == "failed"
    assert result[family]["address"] is None
    assert result[family]["reason"] == "non_public_address"


@pytest.mark.parametrize("body", ["<html>8.8.8.8</html>", "error: 1.2.3.4 blocked", "", "8.8.8.8\x00", "999.1.1.1"])
def test_non_ip_answers_are_rejected(net, body):
    net.set_family("ipv4", body)
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "failed"
    assert result["ipv4"]["address"] is None


def test_blocked_dns_answer_is_not_contacted(net):
    for provider in pe.PROVIDERS["ipv4"]:
        net.dns[("ipv4", provider.host)] = ["127.0.0.1"]
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["reason"] == "blocked_target"
    assert not [c for c in net.connects if c[0] == "ipv4"]


def test_connects_to_the_validated_address_with_the_provider_hostname(net):
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    pe.get_public_egress()
    ipv4_connects = [c for c in net.connects if c[0] == "ipv4"]
    assert {c[2] for c in ipv4_connects} == {p.host for p in pe.PROVIDERS["ipv4"]}
    assert {c[1] for c in ipv4_connects} == {V4}


# ─── 13-15: redirect, TLS, size ──────────────────────────────────────────────


def test_redirect_is_not_followed(net):
    net.set_family("ipv4", "", status=302, extra="Location: https://evil.example/\r\n")
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "failed"
    assert result["ipv4"]["reason"] == "redirect"
    assert not [c for c in net.connects if c[2] == "evil.example"]


def test_http_error_status_is_failed(net):
    net.set_family("ipv4", V4, status=503)
    net.down("ipv6", no_route())
    assert pe.get_public_egress()["ipv4"]["reason"] == "http_status"


def test_invalid_certificate_is_failed(net):
    net.down("ipv4", ssl.SSLCertVerificationError(1, "certificate verify failed"))
    net.down("ipv6", ssl.SSLCertVerificationError(1, "certificate verify failed"))
    result = pe.get_public_egress()
    assert result["ipv4"]["reason"] == "tls_failed"
    assert result["ipv6"]["address"] is None


def test_oversized_answer_is_rejected(net):
    net.set_family("ipv4", "8" * (pe.MAX_RESPONSE_BYTES + 50))
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["reason"] == "response_too_large"


def _self_signed(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "c.pem", directory / "k.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def test_open_tls_really_verifies_the_certificate(monkeypatch):
    """Real TLS handshake against a local server with an untrusted certificate: must be refused."""
    with TemporaryDirectory(prefix="justup-egress-") as tmp:
        cert, key = _self_signed(Path(tmp))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve() -> None:
            try:
                conn, _ = server.accept()
                try:
                    context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    pass
                finally:
                    conn.close()
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        monkeypatch.setattr(pe, "HTTPS_PORT", port)
        try:
            with pytest.raises(ssl.SSLCertVerificationError):
                pe._open_tls("ipv4", "127.0.0.1", "localhost", 3.0)  # noqa: SLF001
        finally:
            server.close()
            thread.join(2)


def test_open_tls_uses_exactly_the_requested_family(monkeypatch):
    seen: list[int] = []

    class Boom:
        def __init__(self, family, *_args):
            seen.append(family)

        def settimeout(self, _t):
            pass

        def connect(self, _addr):
            raise OSError(errno.ENETUNREACH, "unreachable")

        def close(self):
            pass

    monkeypatch.setattr(pe.socket, "socket", Boom)
    with pytest.raises(OSError):
        pe._open_tls("ipv6", V6, "ipv6.icanhazip.com", 1.0)  # noqa: SLF001
    with pytest.raises(OSError):
        pe._open_tls("ipv4", V4, "ipv4.icanhazip.com", 1.0)  # noqa: SLF001
    assert seen == [socket.AF_INET6, socket.AF_INET]


# ─── 16: disagreement, stale ─────────────────────────────────────────────────


def test_different_answers_from_independent_providers_are_inconsistent(net):
    first, second = pe.PROVIDERS["ipv4"]
    net.answers[("ipv4", first.host)] = reply(V4)
    net.answers[("ipv4", second.host)] = reply(V4_ALT)
    net.down("ipv6", no_route())
    result = pe.get_public_egress()["ipv4"]
    assert result["status"] == "inconsistent"
    assert result["address"] is None
    assert result["observed"] == sorted([V4, V4_ALT])


def test_last_good_value_is_reported_as_stale_when_the_family_breaks(net, monkeypatch):
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    first = pe.get_public_egress()
    assert first["ipv6"]["status"] == "available"
    net.down("ipv6", no_route())
    monkeypatch.setattr(pe, "MIN_REFRESH_INTERVAL_SECONDS", 0.0)
    second = pe.get_public_egress(refresh=True)
    assert second["ipv6"]["status"] == "stale"
    assert second["ipv6"]["address"] == V6
    assert second["ipv6"]["observed_at"] is not None
    assert second["ipv4"]["status"] == "available"


def test_stale_value_expires(net, monkeypatch):
    net.set_family("ipv6", V6)
    net.down("ipv4", no_route())
    pe.get_public_egress()
    net.down("ipv6", no_route())
    monkeypatch.setattr(pe, "MIN_REFRESH_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(pe, "STALE_MAX_AGE_SECONDS", -1.0)
    assert pe.get_public_egress(refresh=True)["ipv6"]["status"] == "unavailable"


# ─── 17-19: cache, concurrency, refresh ──────────────────────────────────────


def test_cache_ttl(net, monkeypatch):
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    first = pe.get_public_egress()
    calls = len(net.connects)
    second = pe.get_public_egress()
    assert second["cached"] is True
    assert second["ipv4"] == first["ipv4"]
    assert len(net.connects) == calls
    monkeypatch.setattr(pe, "CACHE_TTL_SECONDS", 0.0)
    assert pe.get_public_egress()["cached"] is False
    assert len(net.connects) > calls


def test_parallel_requests_share_one_lookup(net, monkeypatch):
    gate = threading.Event()
    real = net.open_tls

    def gated(family, address, host, timeout):
        gate.wait(2)
        return real(family, address, host, timeout)

    monkeypatch.setattr(pe, "_open_tls", gated)
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    results: list[pe.EgressResult] = []
    threads = [threading.Thread(target=lambda: results.append(pe.get_public_egress())) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(results) == 6
    assert len(net.connects) == 4  # two providers x two families, once for all six callers
    assert all(r["ipv4"]["address"] == V4 for r in results)


def test_manual_refresh_bypasses_a_fresh_cache_but_keeps_a_minimum_interval(net, monkeypatch):
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    pe.get_public_egress()
    calls = len(net.connects)
    assert pe.get_public_egress(refresh=True)["cached"] is True  # inside the minimum interval
    assert len(net.connects) == calls
    monkeypatch.setattr(pe, "MIN_REFRESH_INTERVAL_SECONDS", 0.0)
    assert pe.get_public_egress(refresh=True)["cached"] is False
    assert len(net.connects) == calls * 2


def test_proxy_environment_is_reported_and_not_used(net, monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    assert pe.get_public_egress()["proxy_env"] is False
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    assert pe.get_public_egress()["proxy_env"] is True  # flag follows the environment even on a cached result
    assert not [c for c in net.connects if "proxy" in c[2]]


# ─── 20-21: API ──────────────────────────────────────────────────────────────


@pytest.fixture
def api(env, monkeypatch):  # noqa: F811 - the imported fixture is intentionally shadowed by its argument
    monkeypatch.setattr(pe, "get_public_egress", _stub)
    return env


def _stub(*, refresh: bool = False):
    _stub.calls.append(refresh)
    empty = {
        "status": "unavailable",
        "address": None,
        "observed_at": None,
        "sources": [],
        "observed": [],
        "reason": None,
    }
    return {
        "ipv4": {**empty, "status": "available", "address": V4, "observed_at": "2026-11-05T19:00:00Z"},
        "ipv6": empty,
        "checked_at": "2026-11-05T19:00:00Z",
        "cached": False,
        "proxy_env": False,
    }


_stub.calls = []  # type: ignore[attr-defined]


def test_api_returns_separate_results_without_caching_headers(api):
    _stub.calls.clear()
    resp = api["admin"].call("GET", "/settings/network/public-egress")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, no-store"
    body = resp.json()
    assert body["ipv4"]["address"] == V4 and body["ipv6"]["status"] == "unavailable"
    assert set(body) == {"ipv4", "ipv6", "checked_at", "cached", "proxy_env"}
    assert _stub.calls == [False]


def test_api_refresh_flag_is_passed_on(api):
    _stub.calls.clear()
    assert api["admin"].call("GET", "/settings/network/public-egress?refresh=true").status_code == 200
    assert _stub.calls == [True]


def test_api_requires_admin(api):
    assert api["user"].call("GET", "/settings/network/public-egress").status_code == 403
    from fastapi.testclient import TestClient

    assert TestClient(api["app"]).get("/settings/network/public-egress").status_code in {401, 403}


def test_api_is_rate_limited_but_refresh_does_not_bypass_it(api):
    codes = [api["admin"].call("GET", "/settings/network/public-egress?refresh=true").status_code for _ in range(14)]
    assert codes[:12] == [200] * 12
    assert 429 in codes[12:]


def test_api_does_not_write_settings_or_leak_into_other_payloads(api):
    from app.db import sqlite as sqlite_db

    def snapshot():
        conn = sqlite_db.connect(api["db_path"])
        try:
            return [tuple(r) for r in conn.execute("SELECT * FROM settings ORDER BY 1")]
        finally:
            sqlite_db.close_connection(conn)

    before = snapshot()
    api["admin"].call("GET", "/settings/network/public-egress")
    assert snapshot() == before
    settings_payload = api["admin"].call("GET", "/settings").text
    assert V4 not in settings_payload and "egress" not in settings_payload.lower()
    assert V4 not in api["admin"].client.get("/healthz").text


# ─── 22-23: template contract ────────────────────────────────────────────────


def test_network_fragment_places_the_card_after_geolocation_without_form_fields(env):  # noqa: F811
    resp = env["admin"].client.get(
        "/ui/fragments/settings-network", headers={"Authorization": f"Bearer {env['admin_token']}"}
    )
    assert resp.status_code == 200
    html = resp.text
    geo, egress = html.index('<h2 class="h6 mb-0">Geolocation</h2>'), html.index("Public outbound IPs")
    assert egress > geo
    card = html[html.rindex("<details", 0, egress) : html.index("</details>", egress)]
    assert 'data-endpoint="/settings/network/public-egress"' in card
    assert " name=" not in card and "<input" not in card  # nothing the settings autosave could pick up
    assert card.count('type="button"') == 3
    for label in ("Copy IPv4 address", "Copy IPv6 address", "Refresh", "Last checked", "Routing, proxies or NAT"):
        assert label in card
