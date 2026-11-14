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
- Resolver and fetch work runs in small pools behind hard pending limits: a hung ``getaddrinfo`` can pin at
  most ``MAX_PENDING_DNS`` threads, and while the limit is reached new lookups fail fast with reason ``busy``.
- Each address is described with the shared geolocation code (``describe_address``): the country and place
  come from the same resolver as the traceroute (local GeoLite2; the online sources only with Enhanced
  Accuracy). ASN and operator come from the local GeoLite2-ASN file without Enhanced Accuracy; the online
  providers fill only missing values and only with Enhanced Accuracy and Provider Lookup both on. No new provider.
- The last confirmed address of each family is cached together with its geolocation and persisted in the
  settings table (``SettingKeys.PUBLIC_EGRESS_STATE``), so it survives restarts. It is kept until a lookup
  confirms a different address; a failed lookup never turns it into a failure state. The geolocation is
  resolved again only when the address, the GeoLite2 file or the enrichment settings change.
- The full result is also cached in memory for a short time; concurrent callers share one lookup.
"""

from __future__ import annotations

import errno
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypedDict

from app.services.ip_lookup import lookup_public_ip, public_ip
from app.services.route_map_rules import country_names
from app.services.traceroute_geo import (
    asn_signature,
    geoip_signature,
    geolocate_asn,
    geolocate_country,
    online_cache_ttl_seconds,
    online_fallback_enabled,
    resolve_location,
)
from app.utils.constants import SettingKeys
from app.utils.runtime_settings import (
    get_ip_api_key,
    get_runtime_bool_fail_closed,
    get_runtime_setting,
    store_runtime_setting,
)
from app.utils.ssrf import is_blocked_ip

_log = logging.getLogger(__name__)

Family = Literal["ipv4", "ipv6"]
Status = Literal["available", "unavailable", "failed", "inconsistent"]

CACHE_TTL_SECONDS = 300.0
MIN_REFRESH_INTERVAL_SECONDS = 10.0  # a forced refresh inside this window reuses the cache
CONNECT_TIMEOUT_SECONDS = 4.0
OVERALL_BUDGET_SECONDS = 8.0
GEO_BUDGET_SECONDS = 5.0  # extra time for the geolocation of the found addresses (online sources are bounded)
DNS_TIMEOUT_SECONDS = 3.0  # getaddrinfo cannot be interrupted; the caller stops waiting after this
MAX_PENDING_DNS = 4  # lookups (running or hung) allowed at once; more are refused, never queued
MAX_PENDING_FETCHES = 8
MAX_RESPONSE_BYTES = 256
MAX_ADDRESSES_PER_HOST = 2
HTTPS_PORT = 443

_FAMILIES: dict[Family, socket.AddressFamily] = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}
_FAMILY_VERSION: dict[Family, int] = {"ipv4": 4, "ipv6": 6}
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


class GeoInfo(TypedDict):
    """Every field is optional information; a value that could not be determined is None."""

    country_code: str | None  # ISO 3166-1 alpha-2, upper case
    country: str | None  # English country name
    city: str | None  # place name (a region when no city is known)
    asn: str | None  # "AS3320"
    as_org: str | None  # operator of the ASN


class FamilyResult(TypedDict):
    status: Status
    address: str | None
    observed_at: str | None
    sources: list[str]
    observed: list[str]  # all distinct addresses seen (more than one only when inconsistent)
    reason: str | None
    geo: GeoInfo | None  # None: nothing determinable or not looked up (no address)


class KnownEntry(TypedDict):
    """The cached address of one family; ``geo_sig`` says which databases/settings produced ``geo``."""

    address: str
    observed_at: str
    sources: list[str]
    geo: GeoInfo | None
    geo_sig: list | None


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


# Validation


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


# Single provider lookup

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


def _resolve_blocking(host: str, family: Family) -> list[str]:
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


def _submit_bounded(
    executor: ThreadPoolExecutor, slots: threading.BoundedSemaphore, func: Callable, *args: object
) -> Future | None:
    """Submit unless ``slots`` is exhausted; a slot is freed when the task really ends, not when waiting stops."""
    if not slots.acquire(blocking=False):
        return None
    try:
        future = executor.submit(func, *args)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _f: slots.release())
    return future


def _resolve(host: str, family: Family) -> list[str]:
    """``_resolve_blocking`` with a hard wait limit and a bound on lookups that are still pinned."""
    future = _submit_bounded(_dns_executor, _dns_slots, _resolve_blocking, host, family)
    if future is None:
        raise EchoLookupError("busy")
    try:
        return future.result(timeout=DNS_TIMEOUT_SECONDS)
    except FutureTimeout:
        raise EchoLookupError("timeout") from None


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


# Combination, cache, single flight

_executor = ThreadPoolExecutor(max_workers=MAX_PENDING_FETCHES, thread_name_prefix="public-egress")
_fetch_slots = threading.BoundedSemaphore(MAX_PENDING_FETCHES)
_dns_executor = ThreadPoolExecutor(max_workers=MAX_PENDING_DNS, thread_name_prefix="public-egress-dns")
_dns_slots = threading.BoundedSemaphore(MAX_PENDING_DNS)
_lock = threading.Lock()
_cached: tuple[float, EgressResult] | None = None
_known: dict[Family, KnownEntry] = {}  # last confirmed address per family, with its geolocation
_known_loaded = False
_persisted: str | None = None  # JSON of the last stored/loaded state, to skip identical writes
_inflight: threading.Event | None = None


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def proxy_env_configured() -> bool:
    """True if a proxy environment variable is set (HTTP monitors may then leave via another address)."""
    return bool(
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")  # noqa: SIM112 - lower-case form is the common proxy convention
    )


def _combine(family: Family, outcomes: list[tuple[Provider, str | EchoLookupError]], now: datetime) -> FamilyResult:
    successes = [(prov, out) for prov, out in outcomes if isinstance(out, str)]
    failures = [out for _prov, out in outcomes if isinstance(out, EchoLookupError)]
    distinct = sorted({str(out) for _prov, out in successes})
    if len(distinct) == 1:
        sources = [prov.host for prov, _out in successes]
        previous = _known.get(family)
        same = previous is not None and previous["address"] == distinct[0]
        _known[family] = {
            "address": distinct[0],
            "observed_at": _iso(now),
            "sources": sources,
            "geo": previous["geo"] if same else None,  # a different address never inherits the old place
            "geo_sig": previous["geo_sig"] if same else None,
        }
        return _available(_known[family])
    if len(distinct) > 1:
        return {
            "status": "inconsistent",
            "address": None,
            "observed_at": _iso(now),
            "sources": [prov.host for prov, _out in successes],
            "observed": distinct,
            "reason": "providers_disagree",
            "geo": None,
        }
    known = _known.get(family)
    if known is not None:
        return _available(known)  # a failed refresh keeps the last confirmed address
    all_unavailable = bool(failures) and all(f.unavailable for f in failures)
    reason = failures[0].reason if failures else "no_provider"
    status: Status = "unavailable" if all_unavailable else "failed"
    return {
        "status": status,
        "address": None,
        "observed_at": None,
        "sources": [],
        "observed": [],
        "reason": reason,
        "geo": None,
    }


def _available(entry: KnownEntry) -> FamilyResult:
    return {
        "status": "available",
        "address": entry["address"],
        "observed_at": entry["observed_at"],
        "sources": list(entry["sources"]),
        "observed": [entry["address"]],
        "reason": None,
        "geo": entry["geo"],
    }


def _text_or_none(value: object, limit: int = 120) -> str | None:
    return value.strip()[:limit] or None if isinstance(value, str) else None


def _valid_geo(raw: object) -> GeoInfo | None:
    if not isinstance(raw, dict):
        return None
    code = raw.get("country_code")
    geo: GeoInfo = {
        "country_code": code if isinstance(code, str) and _COUNTRY_CODE_RE.match(code) else None,
        "country": _text_or_none(raw.get("country")),
        "city": _text_or_none(raw.get("city")),
        "asn": _text_or_none(raw.get("asn"), 32),
        "as_org": _text_or_none(raw.get("as_org")),
    }
    return geo if any(geo.values()) else None


def _valid_entry(family: Family, raw: object) -> KnownEntry | None:
    """A stored entry is trusted no further than a fresh answer: public address of the right family."""
    if not isinstance(raw, dict) or not isinstance(raw.get("address"), str):
        return None
    try:
        address = parse_echo_address(raw["address"], family)
    except EchoLookupError:
        return None
    sources = raw.get("sources")
    when = raw.get("observed_at")
    sig = raw.get("geo_sig")
    return {
        "address": address,
        "observed_at": when if isinstance(when, str) and len(when) <= 32 else "",
        "sources": [x for x in sources if isinstance(x, str)][:4] if isinstance(sources, list) else [],
        "geo": _valid_geo(raw.get("geo")),
        "geo_sig": sig if isinstance(sig, list) else None,
    }


def _ensure_known_loaded() -> None:
    """Load the persisted addresses once; a missing or unreadable store simply means nothing is cached."""
    global _known_loaded, _persisted
    with _lock:
        if _known_loaded:
            return
        _known_loaded = True
    stored = get_runtime_setting(SettingKeys.PUBLIC_EGRESS_STATE, None)
    if not isinstance(stored, dict):
        return
    with _lock:
        for family in PROVIDERS:
            entry = _valid_entry(family, stored.get(family))
            if entry is not None:
                _known.setdefault(family, entry)
        _persisted = json.dumps(_known, sort_keys=True)


def _persist_known() -> None:
    """Store the cached addresses when they changed; best effort, the memory copy stays authoritative."""
    global _persisted
    with _lock:
        payload = json.dumps(_known, sort_keys=True)
        if payload == _persisted or (not _known and _persisted is None):
            return
        snapshot = json.loads(payload)
    if store_runtime_setting(SettingKeys.PUBLIC_EGRESS_STATE, snapshot):
        with _lock:
            _persisted = payload


_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")


def _asn_expected() -> bool:
    """ASN and operator need Enhanced Accuracy and Provider Lookup, both on."""
    return online_fallback_enabled() and get_runtime_bool_fail_closed(SettingKeys.PROVIDER_LOOKUP_ENABLED, True)


def _asn_of(address: str) -> tuple[str | None, str | None]:
    """ASN and operator: the local GeoLite2-ASN file first (no third party, no setting needed).

    The online providers fill only what is still missing, and only when ``_asn_expected``.
    """
    asn, as_org = geolocate_asn(address)
    if (asn and as_org) or not _asn_expected():
        return asn, as_org
    info = lookup_public_ip(address, api_key=get_ip_api_key(), ttl_seconds=online_cache_ttl_seconds())
    if not info:
        return asn, as_org
    return asn or info["asn"] or None, as_org or info["provider"] or None


def describe_address(address: str) -> GeoInfo | None:
    """Country, place, ASN and operator of one public address, or None when none of them is known.

    Reuses the traceroute resolver (local GeoLite2 first, online sources only with Enhanced Accuracy) and the
    shared ip-api/ipwho.is lookup; a missing database, a private address or a provider failure leaves the
    affected fields None and never raises.
    """
    try:
        decision, _extras = resolve_location(address)
        chosen = decision.selected
        code = chosen.country_code if chosen else geolocate_country(address)  # country-only records have no position
        code = code if code and _COUNTRY_CODE_RE.match(code) else None
        city = (decision.place_name or None) if chosen else None
        asn, as_org = _asn_of(address)
    except Exception as exc:  # noqa: BLE001 - optional enrichment must never break the egress lookup
        _log.warning("Geolocation of a public egress address failed (%s)", type(exc).__name__)
        return None
    if not (code or city or asn or as_org):
        return None
    return {
        "country_code": code,
        "country": country_names().get(code) if code else None,
        "city": city,
        "asn": asn,
        "as_org": as_org,
    }


def _geo_signature() -> list:
    """What a geolocation depends on besides the address: the GeoLite2 City and ASN files and the ASN switches."""
    stamp, asn_stamp = geoip_signature(), asn_signature()
    return [list(stamp) if stamp else None, list(asn_stamp) if asn_stamp else None, _asn_expected()]


def _add_geo(results: dict[Family, FamilyResult]) -> None:
    """Describe the found addresses concurrently; a description that misses the budget stays None.

    A cached description is reused while the address, the databases and the settings are unchanged, and
    it also stands in when a new description comes back empty.
    """
    signature = _geo_signature()
    asn_expected = signature[2]
    pending: dict[Family, Future] = {}
    for family, result in results.items():
        address = result["address"]
        if result["status"] != "available" or not address:
            continue
        with _lock:
            entry = _known.get(family)
        geo = entry["geo"] if entry and entry["address"] == address else None
        if geo is not None and entry["geo_sig"] == signature and (geo["asn"] or not asn_expected):
            continue
        future = _submit_bounded(_executor, _fetch_slots, describe_address, address)
        if future is not None:
            pending[family] = future
    wait(list(pending.values()), timeout=GEO_BUDGET_SECONDS)
    with _lock:
        for family, future in pending.items():
            result = results[family]
            if future.done() and not future.cancelled() and future.exception() is None and future.result():
                result["geo"] = future.result()
            else:
                future.cancel()
            entry = _known.get(family)
            if entry and entry["address"] == result["address"] and result["geo"] is not None:
                entry["geo"], entry["geo_sig"] = result["geo"], signature


def _lookup_all() -> EgressResult:
    _ensure_known_loaded()
    futures = {
        (family, provider): _submit_bounded(_executor, _fetch_slots, _fetch_one, provider, family)
        for family, providers in PROVIDERS.items()
        for provider in providers
    }
    wait([f for f in futures.values() if f is not None], timeout=OVERALL_BUDGET_SECONDS)
    now = datetime.now(UTC)
    results: dict[Family, FamilyResult] = {}
    with _lock:
        for family in ("ipv4", "ipv6"):
            outcomes: list[tuple[Provider, str | EchoLookupError]] = []
            for provider in PROVIDERS[family]:
                future = futures[(family, provider)]
                if future is None:
                    outcomes.append((provider, EchoLookupError("busy")))
                    continue
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
            results[family] = _combine(family, outcomes, now)
    _add_geo(results)
    _persist_known()
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
            waiting.wait(OVERALL_BUDGET_SECONDS + GEO_BUDGET_SECONDS + 2.0)
            with _lock:
                if (
                    _cached is not None
                    and time.monotonic() - _cached[0] < OVERALL_BUDGET_SECONDS + GEO_BUDGET_SECONDS + 4.0
                ):
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
    """Drop the memory cache and the loaded addresses (tests); the stored copy stays."""
    global _cached, _inflight, _known_loaded, _persisted
    with _lock:
        _cached = None
        _inflight = None
        _known.clear()
        _known_loaded = False
        _persisted = None
