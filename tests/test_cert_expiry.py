#!/usr/bin/env python3
#
# tests/test_cert_expiry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Certificate expiry semantics: floored day count, shared severity, expired certificates stay visible."""

from __future__ import annotations

import asyncio
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from socket import socket

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.monitor import checks
from app.utils.status_helper import cert_severity
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [(-1, "expired"), (0, "critical"), (3, "critical"), (4, "warning"), (7, "warning"), (8, "ok")],
)
def test_cert_severity_boundaries(days_left, expected):
    assert cert_severity(days_left) == expected


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _issue_expired_chain(tmp_path: Path) -> tuple[Path, Path, Path]:
    now = datetime.now(UTC)
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("test-ca"))
        .issuer_name(_name("test-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("probe.test"))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=10))
        .not_valid_after(now - timedelta(hours=12))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("probe.test")]), critical=False)
        # Python's default context is X509_STRICT and demands an authority key identifier.
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, chain_file, key_file = tmp_path / "ca.pem", tmp_path / "chain.pem", tmp_path / "key.pem"
    ca_file.write_bytes(_pem(ca))
    chain_file.write_bytes(_pem(leaf))
    key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return ca_file, chain_file, key_file


def test_expired_certificate_reports_negative_remaining_time(tmp_path, monkeypatch):
    ca_file, chain_file, key_file = _issue_expired_chain(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(chain_file, key_file)
    listener = socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def _serve() -> None:
        # Two connections: the verifying handshake (rejected by the client) and the unverified retry.
        for _ in range(2):
            try:
                conn, _addr = listener.accept()
                with server_ctx.wrap_socket(conn, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                continue

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    async def _resolve(host, *, skip_ssrf_check):  # noqa: ARG001 - mirrors the patched signature
        return "127.0.0.1", True

    monkeypatch.setattr(checks, "_resolve_probe_target", _resolve)
    try:
        result = asyncio.run(checks.cert_expiration("probe.test", port, timeout_seconds=3))
    finally:
        listener.close()
        thread.join(timeout=3)

    assert result.ok, result.detail
    assert -1.0 < result.value["remaining_days"] < 0.0


def test_expired_certificate_retry_shares_the_timeout_budget(tmp_path, monkeypatch):
    ca_file, chain_file, key_file = _issue_expired_chain(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(chain_file, key_file)
    listener = socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    release = threading.Event()

    def _serve() -> None:
        # First handshake completes after 0.6 s; the retry is never answered until released.
        try:
            conn, _addr = listener.accept()
            time.sleep(0.6)
            with server_ctx.wrap_socket(conn, server_side=True):
                pass
        except (OSError, ssl.SSLError):
            pass
        try:
            stalled, _addr = listener.accept()
            release.wait(5)
            stalled.close()
        except OSError:
            pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    async def _resolve(host, *, skip_ssrf_check):  # noqa: ARG001 - mirrors the patched signature
        return "127.0.0.1", True

    monkeypatch.setattr(checks, "_resolve_probe_target", _resolve)
    started = time.monotonic()
    try:
        result = asyncio.run(checks.cert_expiration("probe.test", port, timeout_seconds=1.0))
    finally:
        elapsed = time.monotonic() - started
        release.set()
        listener.close()
        thread.join(timeout=3)

    assert not result.ok
    assert result.detail == "timeout"
    # Without a shared budget this takes about 0.6 s + 1.0 s.
    assert elapsed < 1.35, elapsed


def test_unverified_retry_must_not_turn_expiry_into_ok(tmp_path, monkeypatch):
    ca_file, expired_chain, expired_key = _issue_expired_chain(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    forged = (
        x509.CertificateBuilder()
        .subject_name(_name("probe.test"))
        .issuer_name(_name("probe.test"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3000))
        .sign(key, hashes.SHA256())
    )
    forged_chain, forged_key = tmp_path / "forged.pem", tmp_path / "forged-key.pem"
    forged_chain.write_bytes(_pem(forged))
    forged_key.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    contexts = []
    for chain, pkey in ((expired_chain, expired_key), (forged_chain, forged_key)):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(chain, pkey)
        contexts.append(ctx)
    listener = socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def _serve() -> None:
        for ctx in contexts:
            try:
                conn, _addr = listener.accept()
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except (OSError, ssl.SSLError):
                continue

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    async def _resolve(host, *, skip_ssrf_check):  # noqa: ARG001 - mirrors the patched signature
        return "127.0.0.1", True

    monkeypatch.setattr(checks, "_resolve_probe_target", _resolve)
    try:
        result = asyncio.run(checks.cert_expiration("probe.test", port, timeout_seconds=3))
    finally:
        listener.close()
        thread.join(timeout=3)

    assert not result.ok
    assert result.detail == "certificate changed between attempts"
