#!/usr/bin/env python3
#
# app/utils/tls_scanner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""TLS scan execution and normalization (SSLyze adapter).

Responsibilities, kept apart on purpose:
  * target validation  -> tls_target.py
  * execution          -> ``scan_tls`` / ``scan_tls_async`` (bounded pool, deadline)
  * normalization      -> ``normalize_scan_result`` (the only code reading SSLyze objects)
  * result model       -> tls_model.py

A scan never returns "nothing": every failure becomes a FAILED result and every
check that could not be evaluated is UNKNOWN, so a failure cannot be mistaken
for a clean bill of health.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import threading
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtensionOID
from sslyze import (
    ScanCommand,
    Scanner,
    ServerNetworkConfiguration,
    ServerNetworkLocation,
    ServerScanRequest,
)
from sslyze.plugins.robot.implementation import RobotScanResultEnum
from sslyze.scanner.scan_command_attempt import ScanCommandAttemptStatusEnum

from .ssrf import redact_url
from .tls_model import (
    LEGACY_PROTOCOL_NAMES,
    CertificateInfo,
    Check,
    ProtocolResult,
    ScanStatus,
    TlsScanResult,
    Verdict,
)
from .tls_target import TlsTargetError, resolve_target

_log = logging.getLogger(__name__)

# Bundled trust-store certificates with legacy serials trigger a cryptography warning we cannot act on.
# A process-wide filter: catch_warnings() is not thread-safe and the warning fires in SSLyze threads.
warnings.filterwarnings("ignore", message=r"Parsed a .*serial number")

# (label, SSLyze attribute, legacy). SSL 2.0/3.0 and TLS 1.0/1.1 are deprecated (RFC 8996).
_PROTOCOLS = (
    (LEGACY_PROTOCOL_NAMES[0], "ssl_2_0_cipher_suites", True),
    (LEGACY_PROTOCOL_NAMES[1], "ssl_3_0_cipher_suites", True),
    (LEGACY_PROTOCOL_NAMES[2], "tls_1_0_cipher_suites", True),
    (LEGACY_PROTOCOL_NAMES[3], "tls_1_1_cipher_suites", True),
    ("TLS 1.2", "tls_1_2_cipher_suites", False),
    ("TLS 1.3", "tls_1_3_cipher_suites", False),
)
_SCAN_COMMANDS = frozenset(
    {
        ScanCommand.CERTIFICATE_INFO,
        ScanCommand.SSL_2_0_CIPHER_SUITES,
        ScanCommand.SSL_3_0_CIPHER_SUITES,
        ScanCommand.TLS_1_0_CIPHER_SUITES,
        ScanCommand.TLS_1_1_CIPHER_SUITES,
        ScanCommand.TLS_1_2_CIPHER_SUITES,
        ScanCommand.TLS_1_3_CIPHER_SUITES,
        ScanCommand.HEARTBLEED,
        ScanCommand.OPENSSL_CCS_INJECTION,
        ScanCommand.ROBOT,
        ScanCommand.SESSION_RENEGOTIATION,
        ScanCommand.TLS_COMPRESSION,
    }
)
_WEAK_HASHES = frozenset({"sha1", "md5", "md2", "md4"})
_MIN_RSA_BITS = 2048
_MIN_EC_BITS = 224

# Bounded parallelism: at most this many scans run (and queue) at once.
MAX_CONCURRENT_SCANS = 2
# Admission limit for running plus queued scans; further requests fail fast as "busy" instead of piling up.
MAX_PENDING_SCANS = 8
_admission = threading.BoundedSemaphore(MAX_PENDING_SCANS)
DEFAULT_DEADLINE_SECONDS = 120.0
_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SCANS, thread_name_prefix="tls-scan")
        return _pool


def shutdown_tls_scanner() -> None:
    """Release the worker pool (application shutdown). Queued scans are cancelled."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _completed_result(attempt: Any) -> Any:
    """Result of a COMPLETED scan attempt; None for error, not-scheduled or missing attempts."""
    if attempt is None or getattr(attempt, "status", None) != ScanCommandAttemptStatusEnum.COMPLETED:
        return None
    return getattr(attempt, "result", None)


def _failed_result(host: str, port: int, error: str, ip: str | None = None) -> TlsScanResult:
    return TlsScanResult(host=host, port=port, status=ScanStatus.FAILED, error=error, ip_address=ip)


def _key_info(public_key: Any) -> tuple[str, int | None, Check]:
    if isinstance(public_key, rsa.RSAPublicKey):
        size = public_key.key_size
        return "RSA", size, Check.from_flag(size < _MIN_RSA_BITS, f"RSA {size}-bit")
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        size = public_key.key_size
        return "EC", size, Check.from_flag(size < _MIN_EC_BITS, f"{public_key.curve.name}")
    if isinstance(public_key, dsa.DSAPublicKey):
        return "DSA", public_key.key_size, Check(Verdict.INSECURE, "DSA is deprecated")
    if isinstance(public_key, ed25519.Ed25519PublicKey):
        return "Ed25519", 256, Check(Verdict.SECURE)
    if isinstance(public_key, ed448.Ed448PublicKey):
        return "Ed448", 456, Check(Verdict.SECURE)
    return (
        type(public_key).__name__.removesuffix("PublicKey") or "unknown",
        None,
        Check.unknown("unrecognized key type"),
    )


def _san_entries(cert: x509.Certificate) -> tuple[str, ...]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except (x509.ExtensionNotFound, ValueError):
        return ()
    return tuple(str(v) for v in ext.value.get_values_for_type(x509.DNSName)) + tuple(
        str(v) for v in ext.value.get_values_for_type(x509.IPAddress)
    )


def _hostname_matches(cert: x509.Certificate, host: str) -> bool:
    """RFC 6125 subset: SAN only (no CN fallback), single left-most wildcard label, IP SANs for IP targets."""
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except (x509.ExtensionNotFound, ValueError):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ip in san.get_values_for_type(x509.IPAddress)
    host = host.lower().rstrip(".")
    for pattern in (p.lower().rstrip(".") for p in san.get_values_for_type(x509.DNSName)):
        if pattern == host:
            return True
        if pattern.startswith("*.") and "." in host:
            label, _, rest = host.partition(".")
            if label and rest == pattern[2:]:
                return True
    return False


def _normalize_certificate(attempt: Any, host: str, now: datetime) -> tuple[CertificateInfo | None, Check]:
    """Return (certificate info, outcome of the certificate command)."""
    res = _completed_result(attempt)
    if res is None:
        return None, Check.unknown("certificate scan failed or did not run")
    deployments = getattr(res, "certificate_deployments", None)
    if not deployments or not deployments[0].received_certificate_chain:
        return None, Check.unknown("server presented no certificate")
    deployment = deployments[0]
    chain = deployment.received_certificate_chain
    leaf = chain[0]
    try:
        valid_from, valid_until = leaf.not_valid_before_utc, leaf.not_valid_after_utc
        hash_alg = leaf.signature_hash_algorithm
        sig_name = hash_alg.name if hash_alg is not None else leaf.signature_algorithm_oid.dotted_string
        key_type, key_size, key_check = _key_info(leaf.public_key())
        subject, issuer = leaf.subject.rfc4514_string(), leaf.issuer.rfc4514_string()
    except (ValueError, TypeError, AttributeError) as exc:
        _log.warning("Certificate could not be parsed: %s", type(exc).__name__)
        return None, Check.unknown("certificate could not be parsed")

    days = (valid_until - now).days
    verified = getattr(deployment, "verified_certificate_chain", None)
    trust = (
        Check(Verdict.SECURE, "chain validates against a trust store")
        if verified
        else Check(Verdict.INSECURE, "chain does not validate against any trust store")
    )
    expiry = Check.from_flag(valid_until <= now or valid_from > now, f"{days} days remaining")
    signature = Check.from_flag(sig_name.lower() in _WEAK_HASHES, sig_name)
    order_ok = getattr(deployment, "received_chain_has_valid_order", None)
    info = CertificateInfo(
        subject=subject,
        issuer=issuer,
        valid_from=valid_from,
        valid_until=valid_until,
        days_remaining=days,
        signature_algorithm=sig_name,
        key_type=key_type,
        key_size=key_size,
        san=_san_entries(leaf),
        chain_length=len(chain),
        trust=trust,
        expiry=expiry,
        signature=signature,
        key_strength=key_check,
        hostname_match=Check.from_flag(not _hostname_matches(leaf, host)),
        chain_order=Check.unknown() if order_ok is None else Check.from_flag(not order_ok),
    )
    return info, Check(Verdict.SECURE)


def _normalize_protocol(label: str, legacy: bool, attempt: Any) -> ProtocolResult:
    res = _completed_result(attempt)
    if res is None:
        return ProtocolResult(label, legacy, None)
    names = tuple(str(s.cipher_suite.name) for s in res.accepted_cipher_suites)
    return ProtocolResult(label, legacy, bool(names), names)


def _flag_check(attempt: Any, attr: str, *, insecure_when: bool) -> Check:
    res = _completed_result(attempt)
    value = None if res is None else getattr(res, attr, None)
    if value is None:
        return Check.unknown("scan command failed or did not run")
    return Check.from_flag(bool(value) is insecure_when)


def _robot_check(attempt: Any) -> Check:
    res = _completed_result(attempt)
    value = None if res is None else getattr(res, "robot_result", None)
    if value in (RobotScanResultEnum.VULNERABLE_WEAK_ORACLE, RobotScanResultEnum.VULNERABLE_STRONG_ORACLE):
        return Check(Verdict.INSECURE, str(value.value))
    if value in (RobotScanResultEnum.NOT_VULNERABLE_NO_ORACLE, RobotScanResultEnum.NOT_VULNERABLE_RSA_NOT_SUPPORTED):
        return Check(Verdict.SECURE, str(value.value))
    return Check.unknown("ROBOT result inconclusive" if value is not None else "scan command failed or did not run")


def normalize_scan_result(server_result: Any, host: str, port: int, ip: str | None, now: datetime) -> TlsScanResult:
    """Convert an SSLyze ServerScanResult into the justUp model (the only place that reads SSLyze objects)."""
    scan = getattr(server_result, "scan_result", None)
    if scan is None:
        return _failed_result(host, port, "server could not be reached", ip)

    cert, cert_check = _normalize_certificate(getattr(scan, "certificate_info", None), host, now)
    protocols = tuple(_normalize_protocol(lbl, legacy, getattr(scan, attr, None)) for lbl, attr, legacy in _PROTOCOLS)
    errors: dict[str, str] = {}
    for command in _SCAN_COMMANDS:
        attempt = getattr(scan, command.value, None)
        if _completed_result(attempt) is None:
            reason = getattr(attempt, "error_reason", None)
            errors[command.value] = str(getattr(reason, "value", reason or "not completed"))
    return TlsScanResult(
        host=host,
        port=port,
        status=ScanStatus.PARTIAL if errors else ScanStatus.COMPLETED,
        ip_address=ip,
        certificate=cert,
        certificate_check=cert_check,
        protocols=protocols,
        heartbleed=_flag_check(getattr(scan, "heartbleed", None), "is_vulnerable_to_heartbleed", insecure_when=True),
        ccs_injection=_flag_check(
            getattr(scan, "openssl_ccs_injection", None), "is_vulnerable_to_ccs_injection", insecure_when=True
        ),
        robot=_robot_check(getattr(scan, "robot", None)),
        secure_renegotiation=_flag_check(
            getattr(scan, "session_renegotiation", None), "supports_secure_renegotiation", insecure_when=False
        ),
        compression=_flag_check(getattr(scan, "tls_compression", None), "supports_compression", insecure_when=True),
        command_errors=errors,
    )


def _run_sslyze(host: str, port: int, ip: str, sni: str | None, timeout: int) -> TlsScanResult:
    location = ServerNetworkLocation(hostname=host, port=port, ip_address=ip)
    net_cfg = ServerNetworkConfiguration(tls_server_name_indication=sni or host, network_timeout=timeout)
    request = ServerScanRequest(
        server_location=location, network_configuration=net_cfg, scan_commands=set(_SCAN_COMMANDS)
    )
    scanner = Scanner(per_server_concurrent_connections_limit=3, concurrent_server_scans_limit=1)
    scanner.queue_scans([request])
    results = list(scanner.get_results())
    if not results:
        return _failed_result(host, port, "scanner returned no result", ip)
    result = results[0]
    if getattr(result.scan_status, "value", None) != "COMPLETED":
        return _failed_result(host, port, "connection to server failed", ip)
    return normalize_scan_result(result, host, port, ip, datetime.now(UTC))


def _resolve_and_scan(target: str, label: str, timeout: int) -> TlsScanResult:
    """Worker body: resolve and SSRF-check once, then scan the pinned IP (DNS-pinning kept)."""
    try:
        resolved = resolve_target(target)
    except TlsTargetError as exc:
        _log.warning("TLS scan refused for %s: %s", label, exc)
        return _failed_result(label, 0, str(exc))
    host, port, ip = resolved.hostname, resolved.port, str(resolved.ip_address)
    return _run_sslyze(host, port, ip, host, timeout)


def scan_tls(target: str, timeout: int = 10, deadline: float = DEFAULT_DEADLINE_SECONDS) -> TlsScanResult:
    """Scan ``target`` (blocking) and return a normalized result; never raises, never returns None.

    ``timeout`` bounds each connection; ``deadline`` bounds the whole scan including queueing.
    """
    label = redact_url(target if "://" in target else f"//{target}").removeprefix("://")
    # Admission comes first so a blocking DNS lookup can neither pile up unbounded nor escape the deadline.
    if not _admission.acquire(blocking=False):
        _log.warning("TLS scan of %s rejected: %d scans already pending", label, MAX_PENDING_SCANS)
        return _failed_result(label, 0, "scanner busy: too many scans pending, try again later")
    try:
        future: Future[TlsScanResult] = _executor().submit(_resolve_and_scan, target, label, timeout)
    except BaseException:
        _admission.release()
        raise
    # The slot is held until the scan really ended (also after a deadline), so abandoned scans still count.
    future.add_done_callback(lambda _f: _admission.release())
    try:
        result = future.result(timeout=deadline)
    except FutureTimeout:
        future.cancel()  # effective only if still queued; a running scan ends by its own connection timeouts
        _log.warning("TLS scan of %s exceeded %.0fs deadline", label, deadline)
        return _failed_result(label, 0, f"scan exceeded {deadline:.0f}s deadline")
    except Exception as exc:  # noqa: BLE001 - scan boundary: any scanner failure becomes a FAILED result
        _log.warning("TLS scan of %s failed: %s", label, type(exc).__name__)
        return _failed_result(label, 0, f"scan error: {type(exc).__name__}")
    host, port = result.host, result.port
    _log.info("TLS scan of %s:%d finished: %s", host, port, result.status.value)
    return result


async def scan_tls_async(target: str, timeout: int = 10, deadline: float = DEFAULT_DEADLINE_SECONDS) -> TlsScanResult:
    """Event-loop-safe wrapper: the blocking scan runs in a worker thread."""
    return await asyncio.to_thread(scan_tls, target, timeout, deadline)


__all__ = [
    "MAX_CONCURRENT_SCANS",
    "normalize_scan_result",
    "scan_tls",
    "scan_tls_async",
    "shutdown_tls_scanner",
]
