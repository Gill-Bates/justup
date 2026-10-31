#!/usr/bin/env python3
#
# tests/test_tls_scanner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""TLS scanner contracts: target validation, normalization, tri-state verdicts and local-server scans.

Integration cases scan throw-away TLS servers on localhost (certificates are generated at
runtime). Heartbleed, CCS injection and ROBOT need a vulnerable server, which is not practical
locally; those checks are covered with synthetic SSLyze result objects only.
"""

# ruff: noqa: SLF001
from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import socket
import ssl
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from sslyze.plugins.robot.implementation import RobotScanResultEnum

from app.utils import tls_scanner, tls_target
from app.utils.tls_model import ScanStatus, Verdict
from app.utils.tls_scanner import ScanCommandAttemptStatusEnum as Status
from app.utils.tls_target import TlsTargetError

# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("192.0.2.1", ("192.0.2.1", 443)),
        ("2001:db8::1", ("2001:db8::1", 443)),
        ("[2001:db8::1]", ("2001:db8::1", 443)),
        ("[2001:db8::1]:8443", ("2001:db8::1", 8443)),
        ("example.org:8443", ("example.org", 8443)),
        ("Example.ORG", ("example.org", 443)),
        ("https://example.org:9443/x", ("example.org", 9443)),
        ("https://[2001:db8::1]/", ("2001:db8::1", 443)),
    ],
)
def test_split_host_port(target, expected):
    assert tls_target._split_host_port(target) == expected


@pytest.mark.parametrize(
    "target", ["", "   ", "example.org:0", "example.org:65536", "example.org:abc", "https://:443", "[2001:db8::1"]
)
def test_invalid_targets_are_rejected(target):
    with pytest.raises(TlsTargetError):
        tls_target.resolve_target(target)


@pytest.mark.parametrize("host", ["bad_host.example", "-a.example", "a..example", "x" * 64 + ".example"])
def test_invalid_hostnames_are_rejected(host):
    with pytest.raises(TlsTargetError):
        tls_target._normalize_hostname(host)


def test_idn_hostname_becomes_ascii_sni():
    assert tls_target._normalize_hostname("bücher.example") == "xn--bcher-kva.example"


def test_resolve_target_pins_first_validated_address():
    with mock.patch.object(tls_target, "resolve_validated", return_value=[ipaddress.ip_address("2001:db8::7")]):
        t = tls_target.resolve_target("[2001:db8::7]:8443")
    assert (t.hostname, t.port, str(t.ip_address), t.is_ip_literal) == ("2001:db8::7", 8443, "2001:db8::7", True)
    with mock.patch.object(tls_target, "resolve_validated", return_value=[ipaddress.ip_address("10.0.0.5")]):
        t = tls_target.resolve_target("Internal.Example:9443")
    assert (t.hostname, t.port, str(t.ip_address), t.is_ip_literal) == ("internal.example", 9443, "10.0.0.5", False)


def test_blocked_or_unresolvable_target_fails_closed():
    with mock.patch.object(tls_target, "resolve_validated", return_value=None), pytest.raises(TlsTargetError):
        tls_target.resolve_target("example.org")


# ---------------------------------------------------------------------------
# Normalization with synthetic SSLyze results
# ---------------------------------------------------------------------------


def _attempt(status, result=None, reason=None):
    return SimpleNamespace(status=status, result=result, error_reason=reason)


def _ciphers(*names):
    return SimpleNamespace(
        accepted_cipher_suites=[SimpleNamespace(cipher_suite=SimpleNamespace(name=n)) for n in names]
    )


def _make_cert(*, days_valid=90, key=None, hash_alg=None, cn="example.org", san=("example.org",), issuer_cn=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn or cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=max(1, -days_valid + 2) if days_valid < 0 else 1))
        .not_valid_after(now + dt.timedelta(days=days_valid))
    )
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in san]), critical=False)
    return builder.sign(key, hash_alg or hashes.SHA256()), key


class _WithHash:
    """Certificate proxy reporting a different signature hash (cryptography will not sign with SHA-1)."""

    def __init__(self, cert, hash_alg):
        self._cert, self.signature_hash_algorithm = cert, hash_alg

    def __getattr__(self, name):
        return getattr(self._cert, name)


def _cert_attempt(cert, *, verified=True, order_ok=True):
    deployment = SimpleNamespace(
        received_certificate_chain=[cert],
        verified_certificate_chain=[cert] if verified else None,
        received_chain_has_valid_order=order_ok,
    )
    return _attempt(Status.COMPLETED, SimpleNamespace(certificate_deployments=[deployment]))


def _server_result(**overrides):
    ok = Status.COMPLETED
    cert, _ = _make_cert()
    scan = SimpleNamespace(
        certificate_info=_cert_attempt(cert),
        ssl_2_0_cipher_suites=_attempt(ok, _ciphers()),
        ssl_3_0_cipher_suites=_attempt(ok, _ciphers()),
        tls_1_0_cipher_suites=_attempt(ok, _ciphers()),
        tls_1_1_cipher_suites=_attempt(ok, _ciphers()),
        tls_1_2_cipher_suites=_attempt(ok, _ciphers("ECDHE-RSA-AES128-GCM-SHA256", "ECDHE-RSA-AES256-GCM-SHA384")),
        tls_1_3_cipher_suites=_attempt(ok, _ciphers("TLS_AES_256_GCM_SHA384")),
        heartbleed=_attempt(ok, SimpleNamespace(is_vulnerable_to_heartbleed=False)),
        openssl_ccs_injection=_attempt(ok, SimpleNamespace(is_vulnerable_to_ccs_injection=False)),
        robot=_attempt(ok, SimpleNamespace(robot_result=RobotScanResultEnum.NOT_VULNERABLE_NO_ORACLE)),
        session_renegotiation=_attempt(ok, SimpleNamespace(supports_secure_renegotiation=True)),
        tls_compression=_attempt(ok, SimpleNamespace(supports_compression=False)),
    )
    for k, v in overrides.items():
        setattr(scan, k, v)
    return SimpleNamespace(scan_status=SimpleNamespace(value="COMPLETED"), scan_result=scan)


def _normalize(result):
    return tls_scanner.normalize_scan_result(result, "example.org", 443, "93.184.216.34", dt.datetime.now(dt.UTC))


def test_clean_scan_is_secure_everywhere():
    r = _normalize(_server_result())
    assert r.status is ScanStatus.COMPLETED and not r.command_errors
    assert {k: c.verdict for k, c in r.checks.items()} == dict.fromkeys(r.checks, Verdict.SECURE)
    assert [p.supported for p in r.protocols] == [False, False, False, False, True, True]
    assert r.preferred_cipher == "TLS_AES_256_GCM_SHA384"
    assert r.protocol("TLS 1.2").cipher_suites == ("ECDHE-RSA-AES128-GCM-SHA256", "ECDHE-RSA-AES256-GCM-SHA384")


def test_certificate_fields_are_extracted():
    cert, _ = _make_cert(days_valid=45, cn="host.example", san=("host.example", "www.host.example"))
    c = _normalize(_server_result(certificate_info=_cert_attempt(cert))).certificate
    assert c.subject == "CN=host.example" and c.issuer == "CN=host.example"
    assert c.days_remaining in (44, 45)
    assert c.valid_until == cert.not_valid_after_utc and c.valid_from == cert.not_valid_before_utc
    assert (c.key_type, c.key_size) == ("EC", 256)
    assert c.signature_algorithm == "sha256" and c.has_sha1_signature is False
    assert c.san == ("host.example", "www.host.example") and c.chain_length == 1
    assert c.trust.verdict is Verdict.SECURE and c.expiry.verdict is Verdict.SECURE


def test_certificate_without_san_and_rsa_key():
    cert, _ = _make_cert(key=rsa.generate_private_key(65537, 2048), san=())
    c = _normalize(_server_result(certificate_info=_cert_attempt(cert))).certificate
    assert c.san == () and (c.key_type, c.key_size) == ("RSA", 2048)
    assert c.key_strength.verdict is Verdict.SECURE


def test_weak_certificate_properties_are_insecure():
    cert, _ = _make_cert(key=rsa.generate_private_key(65537, 1024), san=("other.example",))  # noqa: S505 - deliberately weak fixture
    cert = _WithHash(cert, hashes.SHA1())  # noqa: S303 - OpenSSL refuses to sign with SHA-1, so fake the report
    c = _normalize(_server_result(certificate_info=_cert_attempt(cert, verified=False, order_ok=False))).certificate
    assert c.has_sha1_signature is True and c.signature.verdict is Verdict.INSECURE
    assert c.key_strength.verdict is Verdict.INSECURE
    assert c.trust.verdict is Verdict.INSECURE
    assert c.hostname_match.verdict is Verdict.INSECURE
    assert c.chain_order.verdict is Verdict.INSECURE


@pytest.mark.parametrize(
    ("san", "host", "matches"),
    [
        (("example.org",), "example.org", True),
        (("*.example.org",), "www.example.org", True),
        (("*.example.org",), "a.b.example.org", False),
        (("*.example.org",), "example.org", False),
        (("example.org",), "evil.example", False),
        ((), "example.org", False),
    ],
)
def test_hostname_matching(san, host, matches):
    cert, _ = _make_cert(san=san)
    assert tls_scanner._hostname_matches(cert, host) is matches


def test_hostname_matching_ip_san():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ip")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("2001:db8::1"))]), False)
        .sign(key, hashes.SHA256())
    )
    assert tls_scanner._hostname_matches(cert, "2001:db8::1") is True
    assert tls_scanner._hostname_matches(cert, "2001:db8::2") is False


def test_expired_certificate_is_insecure():
    cert, _ = _make_cert(days_valid=-5)
    c = _normalize(_server_result(certificate_info=_cert_attempt(cert))).certificate
    assert c.expiry.verdict is Verdict.INSECURE and c.days_remaining < 0


def test_legacy_protocols_are_insecure_and_modern_ones_are_not():
    r = _normalize(
        _server_result(
            tls_1_0_cipher_suites=_attempt(Status.COMPLETED, _ciphers("AES128-SHA")),
            ssl_3_0_cipher_suites=_attempt(Status.COMPLETED, _ciphers("AES128-SHA")),
        )
    )
    assert r.protocol("TLS 1.0").check.verdict is Verdict.INSECURE
    assert r.protocol("TLS 1.1").check.verdict is Verdict.SECURE
    assert r.protocol("TLS 1.3").check.verdict is Verdict.SECURE
    assert r.weak_protocols.verdict is Verdict.INSECURE and r.weak_protocols.detail == "SSL 3.0, TLS 1.0"


def test_no_modern_protocol_is_insecure():
    r = _normalize(
        _server_result(
            tls_1_2_cipher_suites=_attempt(Status.COMPLETED, _ciphers()),
            tls_1_3_cipher_suites=_attempt(Status.COMPLETED, _ciphers()),
        )
    )
    assert r.modern_protocol.verdict is Verdict.INSECURE and r.preferred_cipher is None


def test_vulnerabilities_are_insecure():
    r = _normalize(
        _server_result(
            heartbleed=_attempt(Status.COMPLETED, SimpleNamespace(is_vulnerable_to_heartbleed=True)),
            openssl_ccs_injection=_attempt(Status.COMPLETED, SimpleNamespace(is_vulnerable_to_ccs_injection=True)),
            robot=_attempt(
                Status.COMPLETED, SimpleNamespace(robot_result=RobotScanResultEnum.VULNERABLE_STRONG_ORACLE)
            ),
            session_renegotiation=_attempt(Status.COMPLETED, SimpleNamespace(supports_secure_renegotiation=False)),
            tls_compression=_attempt(Status.COMPLETED, SimpleNamespace(supports_compression=True)),
        )
    )
    for check in (r.heartbleed, r.ccs_injection, r.robot, r.secure_renegotiation, r.compression):
        assert check.verdict is Verdict.INSECURE


@pytest.mark.parametrize("status", [Status.ERROR, Status.NOT_SCHEDULED])
def test_failed_or_unscheduled_commands_are_unknown_never_secure(status):
    names = [
        "certificate_info",
        "heartbleed",
        "openssl_ccs_injection",
        "robot",
        "session_renegotiation",
        "tls_compression",
        "ssl_2_0_cipher_suites",
        "ssl_3_0_cipher_suites",
        "tls_1_0_cipher_suites",
        "tls_1_1_cipher_suites",
        "tls_1_2_cipher_suites",
        "tls_1_3_cipher_suites",
    ]
    r = _normalize(_server_result(**{n: _attempt(status) for n in names}))
    assert r.status is ScanStatus.PARTIAL and len(r.command_errors) == len(names)
    assert r.certificate is None
    assert all(c.verdict is Verdict.UNKNOWN for c in r.checks.values())
    assert r.preferred_cipher is None


def test_one_failed_command_does_not_hide_the_others():
    r = _normalize(
        _server_result(heartbleed=_attempt(Status.ERROR, reason=SimpleNamespace(value="CONNECTIVITY_ISSUE")))
    )
    assert r.status is ScanStatus.PARTIAL
    assert r.command_errors == {"heartbleed": "CONNECTIVITY_ISSUE"}
    assert r.heartbleed.verdict is Verdict.UNKNOWN
    assert r.ccs_injection.verdict is Verdict.SECURE and r.weak_protocols.verdict is Verdict.SECURE


@pytest.mark.parametrize(
    ("robot", "verdict"),
    [
        (RobotScanResultEnum.NOT_VULNERABLE_RSA_NOT_SUPPORTED, Verdict.SECURE),
        (RobotScanResultEnum.VULNERABLE_WEAK_ORACLE, Verdict.INSECURE),
        (RobotScanResultEnum.UNKNOWN_INCONSISTENT_RESULTS, Verdict.UNKNOWN),
    ],
)
def test_robot_mapping_treats_inconclusive_as_unknown(robot, verdict):
    r = _normalize(_server_result(robot=_attempt(Status.COMPLETED, SimpleNamespace(robot_result=robot))))
    assert r.robot.verdict is verdict


def test_missing_result_objects_are_unknown():
    r = _normalize(_server_result(heartbleed=None, robot=_attempt(Status.COMPLETED, None)))
    assert r.heartbleed.verdict is Verdict.UNKNOWN and r.robot.verdict is Verdict.UNKNOWN


def _protocol_set(offered=(), skip=(), untested=()):
    from app.utils.tls_model import ProtocolResult

    names = ("SSL 2.0", "SSL 3.0", "TLS 1.0", "TLS 1.1")
    return tuple(ProtocolResult(n, True, None if n in untested else n in offered) for n in names if n not in skip)


def _with_protocols(protocols):
    from app.utils.tls_model import TlsScanResult

    return TlsScanResult(host="h", port=443, status=ScanStatus.COMPLETED, protocols=protocols)


def test_weak_protocols_model_characterization():
    # Complete legacy set, none offered -> SECURE; any offered -> INSECURE.
    assert _with_protocols(_protocol_set()).weak_protocols.verdict is Verdict.SECURE
    assert _with_protocols(_protocol_set(offered=("TLS 1.0",))).weak_protocols.verdict is Verdict.INSECURE
    # A legacy protocol that was not tested (missing or failed) is never a false SECURE.
    assert _with_protocols(_protocol_set(skip=("SSL 3.0",))).weak_protocols.verdict is Verdict.UNKNOWN
    assert _with_protocols(_protocol_set(untested=("SSL 3.0",))).weak_protocols.verdict is Verdict.UNKNOWN
    assert _with_protocols(()).weak_protocols.verdict is Verdict.UNKNOWN
    # Four results that are not the expected four protocols do not count as a complete set.
    dup = _protocol_set(skip=("SSL 3.0", "TLS 1.1")) + _protocol_set(skip=("SSL 3.0", "TLS 1.1"))
    assert _with_protocols(dup).weak_protocols.verdict is Verdict.UNKNOWN


def test_scanner_protocol_table_matches_model_legacy_names():
    from app.utils.tls_model import LEGACY_PROTOCOL_NAMES

    scanner_legacy = tuple(lbl for lbl, _attr, legacy in tls_scanner._PROTOCOLS if legacy)
    assert scanner_legacy == LEGACY_PROTOCOL_NAMES


def test_weak_protocol_insecure_wins_over_unknown():
    r = _normalize(
        _server_result(
            tls_1_0_cipher_suites=_attempt(Status.COMPLETED, _ciphers("X")),
            ssl_2_0_cipher_suites=_attempt(Status.ERROR),
        )
    )
    assert r.weak_protocols.verdict is Verdict.INSECURE
    r = _normalize(_server_result(ssl_2_0_cipher_suites=_attempt(Status.ERROR)))
    assert r.weak_protocols.verdict is Verdict.UNKNOWN


def test_scan_without_result_is_failed():
    result = SimpleNamespace(scan_status=SimpleNamespace(value="ERROR_NO_CONNECTIVITY"), scan_result=None)
    r = tls_scanner.normalize_scan_result(result, "h", 443, None, dt.datetime.now(dt.UTC))
    assert r.status is ScanStatus.FAILED and r.error


# ---------------------------------------------------------------------------
# Execution: failures, deadline, cleanup, logging
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def allow_loopback():
    """The SSRF guard blocks loopback by design; tests scan their own localhost servers."""

    def fake(host, **_kw):
        return (
            [ipaddress.ip_address(host)]
            if host.replace(".", "").isdigit() or ":" in host
            else [ipaddress.ip_address("127.0.0.1")]
        )

    with mock.patch.object(tls_target, "resolve_validated", side_effect=fake):
        yield


@pytest.fixture(autouse=True)
def _fresh_pool():
    tls_scanner.shutdown_tls_scanner()
    yield
    tls_scanner.shutdown_tls_scanner()


def test_log_label_drops_query_of_schemeless_target(caplog):
    caplog.set_level(logging.DEBUG)
    tls_scanner.scan_tls("bad host/p?token=SECRETTOKEN")
    assert "SECRETTOKEN" not in caplog.text


def test_scanner_exception_becomes_failed_result():
    with mock.patch.object(tls_scanner, "_run_sslyze", side_effect=RuntimeError("boom with secret")):
        r = tls_scanner.scan_tls("https://127.0.0.1:9")
    assert r.status is ScanStatus.FAILED and "secret" not in (r.error or "")
    # The pool still works after the failure.
    with mock.patch.object(tls_scanner, "_run_sslyze", return_value=tls_scanner._failed_result("h", 1, "x")):
        assert tls_scanner.scan_tls("https://127.0.0.1:9").error == "x"


def test_deadline_yields_failed_result_and_pool_recovers():
    release = threading.Event()

    def slow(*_a, **_k):
        release.wait(5)
        return tls_scanner._failed_result("h", 1, "late")

    with mock.patch.object(tls_scanner, "_run_sslyze", side_effect=slow):
        r = tls_scanner.scan_tls("https://127.0.0.1:9", deadline=0.2)
        release.set()
    assert r.status is ScanStatus.FAILED and "deadline" in r.error
    tls_scanner.shutdown_tls_scanner()
    assert tls_scanner._pool is None


def test_concurrency_is_bounded():
    running, peak, lock = 0, 0, threading.Lock()

    def work(*_a, **_k):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        threading.Event().wait(0.1)
        with lock:
            running -= 1
        return tls_scanner._failed_result("h", 1, "x")

    with mock.patch.object(tls_scanner, "_run_sslyze", side_effect=work):
        threads = [threading.Thread(target=tls_scanner.scan_tls, args=("https://127.0.0.1:9",)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert peak <= tls_scanner.MAX_CONCURRENT_SCANS


def test_blocking_dns_is_admission_bounded_and_deadline_bound():
    release = threading.Event()

    def blocking_resolve(_target):
        release.wait(10)
        raise TlsTargetError("unresolvable")

    with mock.patch.object(tls_scanner, "resolve_target", side_effect=blocking_resolve):
        # Deadline covers the DNS lookup: a hanging resolver cannot hold the caller.
        r = tls_scanner.scan_tls("slow.example", deadline=0.2)
        assert r.status is ScanStatus.FAILED and "deadline" in r.error
        release.set()
        tls_scanner.shutdown_tls_scanner()
        release.clear()

        # More than MAX_PENDING_SCANS hanging lookups: the surplus is rejected without resolving.
        threads = [
            threading.Thread(target=tls_scanner.scan_tls, args=("slow.example",), kwargs={"deadline": 30})
            for _ in range(tls_scanner.MAX_PENDING_SCANS)
        ]
        for t in threads:
            t.start()
        for _ in range(200):
            if not tls_scanner._admission.acquire(blocking=False):
                break
            tls_scanner._admission.release()
            threading.Event().wait(0.02)
        extra = tls_scanner.scan_tls("slow.example", deadline=30)
        release.set()
        for t in threads:
            t.join(15)
    assert extra.status is ScanStatus.FAILED and "busy" in extra.error


def test_resolved_ip_is_pinned_into_the_scan():
    resolved = tls_target.TlsTarget("a.example", 8443, ipaddress.ip_address("192.0.2.9"), False)
    with (
        mock.patch.object(tls_scanner, "resolve_target", return_value=resolved),
        mock.patch.object(tls_scanner, "_run_sslyze", return_value=tls_scanner._failed_result("h", 1, "x")) as run,
    ):
        tls_scanner.scan_tls("a.example:8443")
    run.assert_called_once_with("a.example", 8443, "192.0.2.9", "a.example", 10)


def test_async_wrapper_runs_off_the_event_loop():
    import asyncio

    def probe(*_a, **_k):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return tls_scanner._failed_result("h", 1, "ok")

    with mock.patch.object(tls_scanner, "_run_sslyze", side_effect=probe):
        assert asyncio.run(tls_scanner.scan_tls_async("https://127.0.0.1:9")).error == "ok"


def test_secrets_are_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    with mock.patch.object(tls_scanner, "_run_sslyze", side_effect=RuntimeError("token=SECRETTOKEN")):
        tls_scanner.scan_tls("https://user:hunter2@127.0.0.1:9/p?token=SECRETTOKEN")
    tls_scanner.scan_tls("https://user:hunter2@:bad/p?token=SECRETTOKEN")
    text = caplog.text
    assert "hunter2" not in text and "SECRETTOKEN" not in text


# ---------------------------------------------------------------------------
# Integration: scans against local throw-away TLS servers
# ---------------------------------------------------------------------------


def _pem(cert, key, tmp_path, name):
    cert_file, key_file = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return str(cert_file), str(key_file)


class _TlsServer:
    def __init__(self, ctx: ssl.SSLContext | None, family=socket.AF_INET, bind="127.0.0.1"):
        self.ctx = ctx
        self.sock = socket.socket(family)
        self.sock.bind((bind, 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                if self.ctx is None:
                    conn.close()  # handshake failure: drop the connection
                    continue
                conn.settimeout(3)
                self.ctx.wrap_socket(conn, server_side=True).close()
            except (OSError, ssl.SSLError):
                conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def tls_server(tmp_path):
    servers = []

    def start(*, days_valid=30, cn="localhost", configure=None, plain_close=False, **cert_kwargs):
        ctx = None
        if not plain_close:
            cert, key = _make_cert(days_valid=days_valid, cn=cn, san=(cn,), **cert_kwargs)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*_pem(cert, key, tmp_path, f"s{len(servers)}"))
            if configure:
                configure(ctx)
        server = _TlsServer(ctx)
        servers.append(server)
        return server

    yield start
    for s in servers:
        s.close()


def _scan(server, host="localhost"):
    return tls_scanner.scan_tls(f"https://{host}:{server.port}", timeout=5, deadline=90)


def test_self_signed_server_scan(tls_server):
    r = _scan(tls_server())
    assert r.status is ScanStatus.COMPLETED, r.command_errors
    assert r.ip_address == "127.0.0.1" and r.port > 0
    cert = r.certificate
    assert cert.subject == "CN=localhost" and cert.san == ("localhost",)
    assert cert.trust.verdict is Verdict.INSECURE  # self-signed, in no trust store
    assert cert.expiry.verdict is Verdict.SECURE and cert.hostname_match.verdict is Verdict.SECURE
    assert r.protocol("TLS 1.3").supported is True and r.protocol("TLS 1.2").supported is True
    assert r.protocol("SSL 2.0").supported is False and r.protocol("SSL 3.0").supported is False
    assert r.heartbleed.verdict is Verdict.SECURE and r.ccs_injection.verdict is Verdict.SECURE
    assert r.compression.verdict is Verdict.SECURE
    assert r.preferred_cipher


def test_expired_certificate_server(tls_server):
    r = _scan(tls_server(days_valid=-3))
    assert r.certificate.expiry.verdict is Verdict.INSECURE and r.certificate.days_remaining < 0


def test_hostname_mismatch_server(tls_server):
    r = _scan(tls_server(cn="other.example"))
    assert r.certificate.hostname_match.verdict is Verdict.INSECURE


def test_tls13_only_server(tls_server):
    def only13(ctx):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3

    r = _scan(tls_server(configure=only13))
    assert r.protocol("TLS 1.3").supported is True and r.protocol("TLS 1.2").supported is False
    assert r.modern_protocol.verdict is Verdict.SECURE and r.weak_protocols.verdict is Verdict.SECURE


def test_tls12_only_server(tls_server):
    def only12(ctx):
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2

    r = _scan(tls_server(configure=only12))
    assert r.protocol("TLS 1.2").supported is True and r.protocol("TLS 1.3").supported is False


def test_legacy_tls10_server_is_flagged(tls_server):
    def legacy(ctx):
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.set_ciphers("ALL:@SECLEVEL=0")
        except (ssl.SSLError, ValueError):
            pytest.skip("local OpenSSL cannot offer TLS 1.0")

    r = _scan(tls_server(configure=legacy))
    if r.protocol("TLS 1.0").supported is not True:
        pytest.skip("local OpenSSL build does not negotiate TLS 1.0")
    assert r.weak_protocols.verdict is Verdict.INSECURE and "TLS 1.0" in r.weak_protocols.detail


def test_unreachable_server_is_failed_not_secure():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = tls_scanner.scan_tls(f"https://127.0.0.1:{port}", timeout=3, deadline=60)
    assert r.status is ScanStatus.FAILED and r.certificate is None
    assert all(c.verdict is Verdict.UNKNOWN for c in r.checks.values())


def test_handshake_failure_is_failed_not_secure(tls_server):
    r = _scan(tls_server(plain_close=True), host="127.0.0.1")
    assert r.status is ScanStatus.FAILED
    assert all(c.verdict is Verdict.UNKNOWN for c in r.checks.values())


def test_ipv6_server_scan(tmp_path):
    if not socket.has_ipv6:
        pytest.skip("IPv6 unavailable")
    cert, key = _make_cert(cn="localhost", san=("localhost",))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*_pem(cert, key, tmp_path, "v6"))
    try:
        server = _TlsServer(ctx, family=socket.AF_INET6, bind="::1")
    except OSError:
        pytest.skip("no IPv6 loopback")
    try:
        r = tls_scanner.scan_tls(f"[::1]:{server.port}", timeout=5, deadline=90)
        assert r.status is ScanStatus.COMPLETED and r.ip_address == "::1"
        assert r.certificate is not None
    finally:
        server.close()
