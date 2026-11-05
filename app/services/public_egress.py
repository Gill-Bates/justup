#!/usr/bin/env python3
#
# app/services/public_egress.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Observed public egress IPv4/IPv6 address of this process (Settings > Network).

The address is what an external HTTPS "IP echo" endpoint sees for a connection opened by
this process, one connection per address family and provider. Nothing is derived from
local interfaces, DNS answers of the host name, the incoming request or proxy headers.

Contract:
- Fixed provider list, no user-supplied URLs; plain text answer, parsed only as an IP address.
- Family is forced per lookup (``AF_INET`` / ``AF_INET6``); a failed IPv6 lookup never falls back to IPv4.
- The name is resolved once, every answer must be globally routable, and the connection goes to the
  validated address (TLS still verifies the certificate against the provider host name).
- No redirects, no proxy: the lookup is direct from this process. Environment proxies that HTTP
  monitors may use (``HTTPS_PROXY`` ...) are NOT used here and are reported as ``proxy_env``.
- Independent providers that disagree are reported as ``inconsistent``, never silently resolved.
- Results are cached in memory only (never persisted); concurrent callers share one lookup.
"""

from __future__ import annotations

import errno
import http.client
import ipaddress
import logging
import os
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

from app.services.ip_lookup import public_ip
from app.utils.ssrf import is_blocked_ip

_log = logging.getLogger(__name__)

Family = Literal["ipv4", "ipv6"]
Status = Literal["available", "unavailable", "failed", "inconsistent", "stale"]

CACHE_TTL_SECONDS = 300.0
MIN_REFRESH_INTERVAL_SECONDS = 10.0  # a forced refresh inside this window reuses the cache
STALE_MAX_AGE_SECONDS = 86400.0  # a last good value older than this is dropped, not shown as stale
CONNECT_TIMEOUT_SECONDS = 4.0
OVERALL_BUDGET_SECONDS = 8.0
MAX_RESPONSE_BYTES = 256
MAX_ADDRESSES_PER_HOST = 2
HTTPS_PORT = 443

_FAMILIES: dict[Family, socket.AddressFamily] = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}
_FAMILY_VERSION: dict[Family, int] = {"ipv4": 4, "ipv6": 6}
_PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
# NAT64: an address from this range is a translated IPv4 address, not a native IPv6 egress address.
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


@dataclass(frozen=True)
class Provider:
    """One fixed echo endpoint. The host name only resolves to the named address family."""

    host: str
    path: str


# Plain-text echo services with family-specific host names (A-only / AAAA-only DNS records), operated by
# two unrelated organisations: Cloudflare (icanhazip) and ipify. Both answer HTTPS GET with the bare IP.
PROVIDERS: dict[Family, tuple[Provider, ...]] = {
    "ipv4": (Provider("ipv4.icanhazip.com", "/"), Provider("api4.ipify.org", "/")),
    "ipv6": (Provider("ipv6.icanhazip.com", "/"), Provider("api6.ipify.org", "/")),
}


class FamilyResult(TypedDict):
    status: Status
    address: str | None
    observed_at: str | None
    sources: list[str]
    observed: list[str]  # all distinct addresses seen (more than one only when inconsistent)
    reason: str | None


class EgressResult(TypedDict):
    ipv4: FamilyResult
    ipv6: FamilyResult
    checked_at: str
    cached: bool
    proxy_env: bool


class EchoLookupError(Exception):
    """A single provider lookup failed; ``unavailable`` marks a missing route/record for the family."""

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


def parse_echo_address(text: str, family: Family) -> str:
    """Return the canonical public address of ``family`` or raise ``EchoLookupError``."""
    value = text.strip()
    if not value or len(value) > 45:
        raise EchoLookupError("invalid_response")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise EchoLookupError("invalid_response") from None
    if ip.version != _FAMILY_VERSION[family]:
        raise EchoLookupError("wrong_family")
    if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped is not None or any(ip in net for net in _NAT64)):
        raise EchoLookupError("wrong_family")
    canonical = public_ip(str(ip))  # is_global; rejects RFC 1918, CGNAT, ULA, link-local, documentation, ...
    if canonical is None or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_reserved:
        raise EchoLookupError("non_public_address")
    return canonical


# ─────────────────────────────────────────────────────────────────────────────
# Single provider lookup
# ─────────────────────────────────────────────────────────────────────────────

_NO_ROUTE_ERRNOS = {
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.EAFNOSUPPORT,
    errno.EADDRNOTAVAIL,
    errno.ENETDOWN,
    errno.EPROTONOSUPPORT,
}
_NO_RECORD_GAI = {
    getattr(socket, name)
    for name in ("EAI_NONAME", "EAI_NODATA", "EAI_ADDRFAMILY", "EAI_FAMILY")
    if hasattr(socket, name)
}


def _resolve(host: str, family: Family) -> list[str]:
    """Resolve ``host`` for exactly ``family`` and return validated, globally routable addresses."""
    try:
        infos = socket.getaddrinfo(host, HTTPS_PORT, _FAMILIES[family], socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EchoLookupError("dns_failed", unavailable=exc.errno in _NO_RECORD_GAI) from None
    except UnicodeError:
        raise EchoLookupError("dns_failed") from None
    addresses: list[str] = []
    for info in infos:
        if info[0] != _FAMILIES[family]:
            continue
        text = str(info[4][0]).split("%")[0]
        ip = ipaddress.ip_address(text)
        if is_blocked_ip(ip, allow_private=False):
            raise EchoLookupError("blocked_target")
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise EchoLookupError("dns_failed", unavailable=True)
    return addresses[:MAX_ADDRESSES_PER_HOST]


def _open_tls(family: Family, address: str, host: str, timeout: float) -> ssl.SSLSocket:
    """TCP connect to the validated ``address`` in ``family`` and verify TLS against ``host``."""
    sock = socket.socket(_FAMILIES[family], socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((address, HTTPS_PORT))
        context = ssl.create_default_context()  # CERT_REQUIRED + hostname check
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context.wrap_socket(sock, server_hostname=host)
    except BaseException:
        sock.close()
        raise


class _Connection(http.client.HTTPSConnection):
    """HTTPS connection pinned to one validated address and address family."""

    def __init__(self, host: str, family: Family, address: str, timeout: float) -> None:
        super().__init__(host, HTTPS_PORT, timeout=timeout)
        self._family = family
        self._address = address

    def connect(self) -> None:
        self.sock = _open_tls(self._family, self._address, self.host, self.timeout)


def _classify_os_error(exc: OSError) -> EchoLookupError:
    if exc.errno in _NO_ROUTE_ERRNOS:
        return EchoLookupError("no_route", unavailable=True)
    if isinstance(exc, TimeoutError):
        return EchoLookupError("timeout")
    return EchoLookupError("connect_failed")


def _fetch_one(provider: Provider, family: Family) -> str:
    """Ask one provider over ``family``; return the validated public address or raise ``EchoLookupError``."""
    last: EchoLookupError = EchoLookupError("connect_failed")
    for address in _resolve(provider.host, family):
        conn = _Connection(provider.host, family, address, CONNECT_TIMEOUT_SECONDS)
        try:
            conn.request(
                "GET",
                provider.path,
                headers={"Accept": "text/plain", "User-Agent": "justUp!", "Connection": "close"},
            )
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise EchoLookupError("redirect")
            if response.status != 200:
                raise EchoLookupError("http_status")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise EchoLookupError("response_too_large")
            try:
                text = body.decode("ascii")
            except UnicodeDecodeError:
                raise EchoLookupError("invalid_response") from None
            return parse_echo_address(text, family)
        except EchoLookupError as exc:
            last = exc
            if exc.reason in {"redirect", "http_status", "response_too_large", "invalid_response", "wrong_family"}:
                raise
        except ssl.SSLError:
            raise EchoLookupError("tls_failed") from None
        except (OSError, http.client.HTTPException) as exc:
            last = _classify_os_error(exc) if isinstance(exc, OSError) else EchoLookupError("invalid_response")
        finally:
            conn.close()
    raise last


# ─────────────────────────────────────────────────────────────────────────────
# Combination, cache, single flight
# ─────────────────────────────────────────────────────────────────────────────

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="public-egress")
_lock = threading.Lock()
_cached: tuple[float, EgressResult] | None = None
_last_good: dict[Family, tuple[float, str, list[str]]] = {}  # family -> (monotonic time, address, sources)
_inflight: threading.Event | None = None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def proxy_env_configured() -> bool:
    """True if a proxy environment variable is set (HTTP monitors may then leave via another address)."""
    return any(os.environ.get(name) or os.environ.get(name.lower()) for name in _PROXY_ENV)


def _combine(
    family: Family, outcomes: list[tuple[Provider, str | EchoLookupError]], now_mono: float, now: datetime
) -> FamilyResult:
    successes = [(prov, out) for prov, out in outcomes if isinstance(out, str)]
    failures = [out for _prov, out in outcomes if isinstance(out, EchoLookupError)]
    distinct = sorted({str(out) for _prov, out in successes})
    if len(distinct) == 1:
        sources = [prov.host for prov, _out in successes]
        _last_good[family] = (now_mono, distinct[0], sources)
        return {
            "status": "available",
            "address": distinct[0],
            "observed_at": _iso(now),
            "sources": sources,
            "observed": distinct,
            "reason": None,
        }
    if len(distinct) > 1:
        return {
            "status": "inconsistent",
            "address": None,
            "observed_at": _iso(now),
            "sources": [prov.host for prov, _out in successes],
            "observed": distinct,
            "reason": "providers_disagree",
        }
    all_unavailable = bool(failures) and all(f.unavailable for f in failures)
    reason = failures[0].reason if failures else "no_provider"
    status: Status = "unavailable" if all_unavailable else "failed"
    previous = _last_good.get(family)
    if previous and now_mono - previous[0] <= STALE_MAX_AGE_SECONDS:
        observed_at = _iso(now - timedelta(seconds=now_mono - previous[0]))
        return {
            "status": "stale",
            "address": previous[1],
            "observed_at": observed_at,
            "sources": previous[2],
            "observed": [previous[1]],
            "reason": reason,
        }
    return {"status": status, "address": None, "observed_at": None, "sources": [], "observed": [], "reason": reason}


def _lookup_all() -> EgressResult:
    futures = {
        (family, provider): _executor.submit(_fetch_one, provider, family)
        for family, providers in PROVIDERS.items()
        for provider in providers
    }
    wait(list(futures.values()), timeout=OVERALL_BUDGET_SECONDS)
    now_mono = time.monotonic()
    now = datetime.now(UTC)
    results: dict[Family, FamilyResult] = {}
    with _lock:
        for family in ("ipv4", "ipv6"):
            outcomes: list[tuple[Provider, str | EchoLookupError]] = []
            for provider in PROVIDERS[family]:
                future = futures[(family, provider)]
                if not future.done():
                    future.cancel()
                    outcomes.append((provider, EchoLookupError("timeout")))
                    continue
                try:
                    outcomes.append((provider, future.result()))
                except EchoLookupError as exc:
                    outcomes.append((provider, exc))
                except Exception:  # noqa: BLE001 - a provider bug must not break the settings page
                    _log.exception("Unexpected public egress lookup error (%s, %s)", family, provider.host)
                    outcomes.append((provider, EchoLookupError("internal_error")))
            results[family] = _combine(family, outcomes, now_mono, now)
    return {
        "ipv4": results["ipv4"],
        "ipv6": results["ipv6"],
        "checked_at": _iso(now),
        "cached": False,
        "proxy_env": proxy_env_configured(),
    }


def get_public_egress(*, refresh: bool = False) -> EgressResult:
    """Return the cached result or run one shared lookup (blocking; call from a worker thread).

    ``refresh`` skips a still-fresh cache but not the minimum refresh interval.
    """
    global _cached, _inflight
    while True:
        with _lock:
            now = time.monotonic()
            if _cached is not None:
                age = now - _cached[0]
                limit = MIN_REFRESH_INTERVAL_SECONDS if refresh else CACHE_TTL_SECONDS
                if age < limit:
                    return {**_cached[1], "cached": True, "proxy_env": proxy_env_configured()}
            waiting = _inflight
            if waiting is None:
                _inflight = waiting = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            waiting.wait(OVERALL_BUDGET_SECONDS + 2.0)
            with _lock:
                if _cached is not None and time.monotonic() - _cached[0] < OVERALL_BUDGET_SECONDS + 4.0:
                    return {**_cached[1], "cached": True, "proxy_env": proxy_env_configured()}
            continue
        try:
            result = _lookup_all()
            with _lock:
                _cached = (time.monotonic(), result)
            return result
        finally:
            with _lock:
                _inflight = None
            waiting.set()


def reset_state() -> None:
    """Drop cache and last-good values (tests)."""
    global _cached, _inflight
    with _lock:
        _cached = None
        _inflight = None
        _last_good.clear()
