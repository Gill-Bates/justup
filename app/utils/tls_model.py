#!/usr/bin/env python3
#
# app/utils/tls_model.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Scanner-independent result model for TLS security checks.

The PDF report and every other consumer read only these types. Nothing in here
imports the scanning library, so its result objects can change without
touching the rendering code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# Deprecated protocols (RFC 8996) every scan must report on; a missing one was not tested.
LEGACY_PROTOCOL_NAMES = ("SSL 2.0", "SSL 3.0", "TLS 1.0", "TLS 1.1")


class Verdict(StrEnum):
    """Outcome of one check. UNKNOWN is never a pass: it means not tested, failed or unclear."""

    SECURE = "secure"
    INSECURE = "insecure"
    UNKNOWN = "unknown"


class ScanStatus(StrEnum):
    COMPLETED = "completed"  # every scan command produced a result
    PARTIAL = "partial"  # some commands failed; their checks are UNKNOWN
    FAILED = "failed"  # nothing usable (invalid target, no connection, deadline)


@dataclass(frozen=True)
class Check:
    verdict: Verdict
    detail: str = ""

    @classmethod
    def unknown(cls, detail: str = "") -> Check:
        return cls(Verdict.UNKNOWN, detail)

    @classmethod
    def from_flag(cls, insecure: bool | None, detail: str = "") -> Check:
        """Map a tri-state 'problem confirmed' flag; None stays UNKNOWN."""
        if insecure is None:
            return cls(Verdict.UNKNOWN, detail)
        return cls(Verdict.INSECURE if insecure else Verdict.SECURE, detail)


@dataclass(frozen=True)
class ProtocolResult:
    name: str  # "SSL 2.0" ... "TLS 1.3"
    legacy: bool  # SSL 2.0/3.0, TLS 1.0/1.1
    supported: bool | None  # None = scan command failed or did not run
    cipher_suites: tuple[str, ...] = ()

    @property
    def check(self) -> Check:
        if self.supported is None:
            return Check.unknown()
        if self.legacy:
            return Check.from_flag(self.supported)
        return Check(Verdict.SECURE)  # offering or omitting a modern protocol is not a flaw on its own


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    issuer: str
    valid_from: datetime
    valid_until: datetime
    days_remaining: int
    signature_algorithm: str
    key_type: str
    key_size: int | None
    san: tuple[str, ...]
    chain_length: int
    trust: Check  # chain validates against at least one trust store
    expiry: Check
    signature: Check  # SHA-1 / MD5 detection
    key_strength: Check
    hostname_match: Check
    chain_order: Check

    @property
    def has_sha1_signature(self) -> bool | None:
        v = self.signature.verdict
        return None if v is Verdict.UNKNOWN else v is Verdict.INSECURE


@dataclass(frozen=True)
class TlsScanResult:
    host: str
    port: int
    status: ScanStatus
    error: str | None = None  # human-readable reason when status is FAILED
    ip_address: str | None = None  # the address that was actually scanned
    certificate: CertificateInfo | None = None
    certificate_deployments: tuple[CertificateInfo, ...] = ()
    certificate_check: Check = field(default_factory=Check.unknown)  # certificate_info command outcome
    unparsed_certificates: int = 0  # deployments presented but not evaluable; they make the verdicts UNKNOWN
    protocols: tuple[ProtocolResult, ...] = ()
    mozilla_tls_profile: Check = field(default_factory=Check.unknown)
    fallback_scsv: Check = field(default_factory=Check.unknown)
    extended_master_secret: Check = field(default_factory=Check.unknown)
    tls_1_3_early_data: bool | None = None
    session_resumption: str | None = None
    elliptic_curves: str | None = None
    http_headers: str | None = None
    heartbleed: Check = field(default_factory=Check.unknown)
    ccs_injection: Check = field(default_factory=Check.unknown)
    robot: Check = field(default_factory=Check.unknown)
    secure_renegotiation: Check = field(default_factory=Check.unknown)
    compression: Check = field(default_factory=Check.unknown)  # CRIME
    command_errors: dict[str, str] = field(default_factory=dict)

    def protocol(self, name: str) -> ProtocolResult | None:
        return next((p for p in self.protocols if p.name == name), None)

    @property
    def weak_protocols(self) -> Check:
        """INSECURE if any legacy protocol is offered, UNKNOWN if one could not be tested, else SECURE."""
        legacy = [p for p in self.protocols if p.legacy]
        offered = [p.name for p in legacy if p.supported]
        if offered:  # a confirmed weak protocol outranks missing or unfinished checks
            return Check(Verdict.INSECURE, ", ".join(offered))
        if not set(LEGACY_PROTOCOL_NAMES) <= {p.name for p in legacy}:  # missing means not tested
            return Check.unknown("legacy protocol results missing")
        if any(p.supported is None for p in legacy):
            return Check.unknown()
        return Check(Verdict.SECURE)

    @property
    def modern_protocol(self) -> Check:
        """SECURE if TLS 1.2 or 1.3 is offered; INSECURE only when both are confirmed absent."""
        modern = [p for p in self.protocols if not p.legacy]
        if any(p.supported for p in modern):
            return Check(Verdict.SECURE)
        if len(modern) == 2 and all(p.supported is False for p in modern):
            return Check(Verdict.INSECURE, "neither TLS 1.2 nor TLS 1.3 offered")
        return Check.unknown()

    @property
    def cipher_suite_example(self) -> str | None:
        """Return one accepted suite without implying that it is the server's preference."""
        for name in ("TLS 1.3", "TLS 1.2"):
            proto = self.protocol(name)
            if proto and proto.cipher_suites:
                return proto.cipher_suites[0]
        return None

    def _certificate_verdict(self, attr: str) -> Check:
        """Aggregate one certificate check over all deployments: INSECURE before UNKNOWN before SECURE."""
        certs = self.certificate_deployments or ((self.certificate,) if self.certificate else ())
        multiple = len(certs) + self.unparsed_certificates > 1
        found = [(i, getattr(cert, attr)) for i, cert in enumerate(certs, start=1)]
        if self.unparsed_certificates:
            note = f"{self.unparsed_certificates} certificate deployment(s) could not be evaluated"
            found.append((0, Check.unknown(note)))
        for wanted in (Verdict.INSECURE, Verdict.UNKNOWN):
            hit = next(((i, c) for i, c in found if c.verdict is wanted), None)
            if hit is not None:
                index, check = hit
                if not multiple or index == 0:
                    return check
                return Check(wanted, f"Deployment {index}: {check.detail}".rstrip(": "))
        return found[0][1]

    @property
    def checks(self) -> dict[str, Check]:
        """All security verdicts by label, in report order."""
        out: dict[str, Check] = {}
        if self.certificate:
            out.update(
                {
                    "Certificate trust": self._certificate_verdict("trust"),
                    "Certificate expiry": self._certificate_verdict("expiry"),
                    "Signature algorithm": self._certificate_verdict("signature"),
                    "Key strength": self._certificate_verdict("key_strength"),
                    "Hostname match": self._certificate_verdict("hostname_match"),
                    "Chain order": self._certificate_verdict("chain_order"),
                }
            )
        else:
            out["Certificate"] = self.certificate_check
        out.update(
            {
                "Weak protocols": self.weak_protocols,
                "Modern protocol": self.modern_protocol,
                "Mozilla TLS profile (intermediate)": self.mozilla_tls_profile,
                "TLS_FALLBACK_SCSV": self.fallback_scsv,
                "Extended Master Secret": self.extended_master_secret,
                "Heartbleed": self.heartbleed,
                "OpenSSL CCS injection": self.ccs_injection,
                "ROBOT": self.robot,
                "Secure renegotiation": self.secure_renegotiation,
                "TLS compression (CRIME)": self.compression,
            }
        )
        return out
