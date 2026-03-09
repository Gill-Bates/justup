#!/usr/bin/env python3
#
# app/utils/sslyze_scanner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SSLyze-based SSL/TLS analysis for PDF reports."""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from sslyze import Scanner, ServerScanRequest, ServerNetworkLocation, ScanCommand, ServerNetworkConfiguration
from sslyze.errors import ServerHostnameCouldNotBeResolved, ConnectionToServerFailed

_log = logging.getLogger(__name__)


@dataclass
class SSLSummary:
    """Summarized SSL/TLS scan results."""
    
    # Certificate info
    cert_subject: str
    cert_issuer: str
    cert_valid_from: str
    cert_valid_until: str
    cert_days_remaining: int
    cert_signature_algorithm: str
    cert_key_type: str
    cert_key_size: int
    cert_san_count: int
    
    # Protocol support
    supports_ssl2: bool
    supports_ssl3: bool
    supports_tls10: bool
    supports_tls11: bool
    supports_tls12: bool
    supports_tls13: bool
    
    # Preferred cipher suites (best/recommended)
    tls12_cipher: Optional[str]
    tls13_cipher: Optional[str]
    
    # Vulnerabilities
    vulnerable_heartbleed: bool
    vulnerable_ccs_injection: bool
    vulnerable_robot: bool
    supports_secure_renegotiation: bool
    supports_compression: bool  # CRIME attack
    
    # Overall grade hints
    has_weak_protocols: bool  # SSL2, SSL3, TLS1.0, TLS1.1
    has_sha1_signature: bool
    certificate_trusted: bool


def _parse_target(target: str) -> tuple[Optional[str], int]:
    if "://" in target:
        parsed = urlparse(target)
        return parsed.hostname, parsed.port or 443

    if target.startswith("["):
        end = target.find("]")
        if end != -1:
            host = target[1:end]
            rest = target[end + 1 :]
            if rest.startswith(":"):
                try:
                    return host, int(rest[1:])
                except ValueError:
                    return host, 443
            return host, 443

    parts = target.rsplit(":", 1)
    if len(parts) == 2:
        try:
            return parts[0], int(parts[1])
        except ValueError:
            return target, 443

    return target, 443


def scan_ssl(
    target: str,
    timeout: int = 10,
    scan_commands: Optional[set[ScanCommand]] = None,
) -> Optional[SSLSummary]:
    """Run SSLyze scan and return summarized results.
    
    Args:
        target: Hostname or URL to scan (e.g., "example.com" or "https://example.com")
        timeout: Connection timeout in seconds
        
    Returns:
        SSLSummary with scan results, or None if scan failed
    """
    hostname, port = _parse_target(target)

    if not hostname:
        _log.warning("Cannot extract hostname from target: %s", target)
        return None

    if not (1 <= port <= 65535):
        _log.warning("Invalid port %s for target %s", port, target)
        return None

    if scan_commands is None:
        scan_commands = {
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

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Parsed a negative serial number",
                category=DeprecationWarning,
                module="cryptography",
            )

            location = ServerNetworkLocation(hostname=hostname, port=port)
            net_cfg = ServerNetworkConfiguration(
                tls_server_name_indication=hostname,
                network_timeout=timeout,
            )

            scan_request = ServerScanRequest(
                server_location=location,
                network_configuration=net_cfg,
                scan_commands=scan_commands,
            )

            scanner = Scanner()
            scanner.queue_scans([scan_request])
            results = list(scanner.get_results())
            if not results:
                _log.warning("No SSLyze results for %s", hostname)
                return None

            result = results[0]
            if result.scan_status.value != "COMPLETED":
                _log.warning("SSLyze scan not completed: %s", result.scan_status)
                return None

        cert_info = result.scan_result.certificate_info
        cert_subject = cert_issuer = "-"
        cert_valid_from = cert_valid_until = "-"
        days_remaining = 0
        sig_algo = "-"
        key_type = "-"
        key_size = None
        san_count = 0
        cert_trusted = False
        has_sha1 = False

        if cert_info and cert_info.result and cert_info.result.certificate_deployments:
            cert_deployment = cert_info.result.certificate_deployments[0]
            chain = cert_deployment.received_certificate_chain
            if chain:
                leaf_cert = chain[0]
                cert_subject = leaf_cert.subject.rfc4514_string()
                cert_issuer = leaf_cert.issuer.rfc4514_string()
                cert_valid_from = leaf_cert.not_valid_before_utc.strftime("%Y-%m-%d")
                cert_valid_until = leaf_cert.not_valid_after_utc.strftime("%Y-%m-%d")

                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                days_remaining = (leaf_cert.not_valid_after_utc - now).days

                sig_algo = getattr(
                    getattr(leaf_cert, "signature_hash_algorithm", None),
                    "name",
                    leaf_cert.signature_algorithm_oid.dotted_string,
                )

                public_key = leaf_cert.public_key()
                key_size = getattr(public_key, "key_size", None)
                key_type = type(public_key).__name__.replace("PublicKey", "").replace("_", "")

                from cryptography.x509.oid import ExtensionOID

                try:
                    san_ext = leaf_cert.extensions.get_extension_for_oid(
                        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
                    )
                    san_count = len(san_ext.value)
                except Exception:
                    san_count = 0

                cert_trusted = cert_deployment.verified_certificate_chain is not None
                has_sha1 = "sha1" in sig_algo.lower()
        
        # Protocol support
        def has_accepted_ciphers(cipher_result) -> bool:
            if cipher_result and cipher_result.result:
                return len(cipher_result.result.accepted_cipher_suites) > 0
            return False
        
        ssl2_result = result.scan_result.ssl_2_0_cipher_suites
        ssl3_result = result.scan_result.ssl_3_0_cipher_suites
        tls10_result = result.scan_result.tls_1_0_cipher_suites
        tls11_result = result.scan_result.tls_1_1_cipher_suites
        tls12_result = result.scan_result.tls_1_2_cipher_suites
        tls13_result = result.scan_result.tls_1_3_cipher_suites
        
        supports_ssl2 = has_accepted_ciphers(ssl2_result)
        supports_ssl3 = has_accepted_ciphers(ssl3_result)
        supports_tls10 = has_accepted_ciphers(tls10_result)
        supports_tls11 = has_accepted_ciphers(tls11_result)
        supports_tls12 = has_accepted_ciphers(tls12_result)
        supports_tls13 = has_accepted_ciphers(tls13_result)
        
        # Preferred ciphers
        tls12_cipher = (
            tls12_result.result.accepted_cipher_suites[0].cipher_suite.name
            if tls12_result and tls12_result.result and tls12_result.result.accepted_cipher_suites
            else None
        )
        
        tls13_cipher = None
        if tls13_result and tls13_result.result and tls13_result.result.accepted_cipher_suites:
            tls13_cipher = tls13_result.result.accepted_cipher_suites[0].cipher_suite.name
        
        # Vulnerabilities
        heartbleed = result.scan_result.heartbleed
        vulnerable_heartbleed = heartbleed.result.is_vulnerable_to_heartbleed if heartbleed and heartbleed.result else False
        
        ccs = result.scan_result.openssl_ccs_injection
        vulnerable_ccs = ccs.result.is_vulnerable_to_ccs_injection if ccs and ccs.result else False
        
        robot = result.scan_result.robot

        from sslyze.plugins.robot.implementation import RobotScanResultEnum

        vulnerable_robot = (
            robot.result.robot_result
            in {
                RobotScanResultEnum.VULNERABLE_WEAK_ORACLE,
                RobotScanResultEnum.VULNERABLE_STRONG_ORACLE,
            }
            if robot and robot.result
            else False
        )
        
        reneg = result.scan_result.session_renegotiation
        supports_secure_reneg = reneg.result.supports_secure_renegotiation if reneg and reneg.result else False
        
        compression = result.scan_result.tls_compression
        supports_compression = compression.result.supports_compression if compression and compression.result else False
        
        has_weak = supports_ssl2 or supports_ssl3 or supports_tls10 or supports_tls11
        
        _log.info("SSLyze scan completed for %s:%d", hostname, port)
        
        return SSLSummary(
            cert_subject=cert_subject,
            cert_issuer=cert_issuer,
            cert_valid_from=cert_valid_from,
            cert_valid_until=cert_valid_until,
            cert_days_remaining=days_remaining,
            cert_signature_algorithm=sig_algo,
            cert_key_type=key_type,
            cert_key_size=key_size or 0,
            cert_san_count=san_count,
            supports_ssl2=supports_ssl2,
            supports_ssl3=supports_ssl3,
            supports_tls10=supports_tls10,
            supports_tls11=supports_tls11,
            supports_tls12=supports_tls12,
            supports_tls13=supports_tls13,
            tls12_cipher=tls12_cipher,
            tls13_cipher=tls13_cipher,
            vulnerable_heartbleed=vulnerable_heartbleed,
            vulnerable_ccs_injection=vulnerable_ccs,
            vulnerable_robot=vulnerable_robot,
            supports_secure_renegotiation=supports_secure_reneg,
            supports_compression=supports_compression,
            has_weak_protocols=has_weak,
            has_sha1_signature=has_sha1,
            certificate_trusted=cert_trusted,
        )
        
    except ServerHostnameCouldNotBeResolved as e:
        _log.warning("SSLyze: hostname could not be resolved: %s", e)
        return None
    except ConnectionToServerFailed as e:
        _log.warning("SSLyze: connection to server failed: %s", e)
        return None
    except Exception as e:
        _log.exception("SSLyze scan failed: %s", e)
        return None


__all__ = ["SSLSummary", "scan_ssl"]
