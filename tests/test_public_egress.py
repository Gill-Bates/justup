#!/usr/bin/env python3
#
# tests/test_public_egress.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Public egress lookup (Settings > Network): all network access is mocked, nothing leaves the machine."""

from __future__ import annotations

import errno
import socket
import threading
import time

import pytest
from gillbates_public_ip import FAMILIES, Target

from app.services import geo_resolver as gr
from app.services import public_egress as pe
from app.utils.constants import SettingKeys
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests.test_api_hardening import _encryption_key, env  # noqa: F401 - fixtures reused for the API tests

V4 = "93.184.216.34"
V6 = "2606:2800:220:1:248:1893:25c8:1946"
V4_OTHER = "198.51.100.7"  # documentation range is non-global; used only where a *different* value is needed
V4_ALT = "8.8.4.4"


class Net:
    """Scripted echo targets: ``answers[(family, name)]`` is the answer text or an exception to raise."""

    NAMES = ("icanhazip", "ipify")

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers: dict[tuple[str, str], str | BaseException] = {}
        self.connects: list[tuple[str, str]] = []
        self.gate: threading.Event | None = None
        self.hang: dict[str, threading.Event] = {}
        targets = tuple(Target(name, family, self._fetcher(family, name)) for family in FAMILIES for name in self.NAMES)
        monkeypatch.setattr(pe, "TARGETS", targets)
        monkeypatch.setattr(pe, "_local_probe", lambda _family: "10.0.0.1")

    def _fetcher(self, family: str, name: str):
        def fetch(_timeout: float) -> str:
            self.connects.append((family, name))
            if self.gate is not None:
                self.gate.wait(2)
            if family in self.hang:
                self.hang[family].wait(5)
            answer = self.answers.get((family, name))
            if isinstance(answer, BaseException):
                raise answer
            if answer is None:
                raise OSError(errno.ECONNREFUSED, "no scripted answer")
            return answer

        return fetch

    def set_family(self, family: str, body) -> None:
        for name in self.NAMES:
            self.answers[(family, name)] = body

    def down(self, family: str, exc: BaseException) -> None:
        self.set_family(family, exc)


_REAL_DESCRIBE = pe.describe_address
STORE: dict[str, object] = {}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    # Hermetic: the lookup tests never read the local GeoLite2 file; the geo tests call _REAL_DESCRIBE.
    monkeypatch.setattr(pe, "describe_address", lambda _address: None)
    # The persisted state lives in a dict, never in the developer's real settings database.
    monkeypatch.setattr(pe, "get_runtime_setting", lambda _key, default=None: STORE.get("state", default))
    monkeypatch.setattr(pe, "store_runtime_setting", lambda _key, value: STORE.__setitem__("state", value) or True)
    monkeypatch.setattr(pe, "geoip_signature", lambda: (1, 2, 3))
    monkeypatch.setattr(pe, "asn_signature", lambda: None)  # never the developer's real ASN file
    STORE.clear()
    pe.reset_state()
    yield
    pe.reset_state()
    STORE.clear()


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
    assert sorted(result["ipv4"]["sources"]) == sorted(Net.NAMES)
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
    net.down("ipv4", no_route())
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
    assert {family for family, _name in net.connects} == {"ipv4", "ipv6"}
    assert all(name in Net.NAMES for _family, name in net.connects)


def test_one_provider_down_still_available_from_the_other(net):
    first, second = Net.NAMES
    net.answers[("ipv4", first)] = OSError(errno.ECONNREFUSED, "refused")
    net.answers[("ipv4", second)] = V4
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "available"
    assert result["ipv4"]["sources"] == [second]


# ─── 6-9: failures ───────────────────────────────────────────────────────────


def test_no_internet_is_unavailable_for_both(net):
    net.down("ipv4", no_route())
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == result["ipv6"]["status"] == "unavailable"
    assert result["ipv4"]["address"] is None


def test_dns_failure_is_a_failed_lookup_not_an_address(net):
    for family in ("ipv4", "ipv6"):
        net.set_family(family, socket.gaierror(socket.EAI_AGAIN, "temporary failure"))
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
    net.hang["ipv6"] = release
    monkeypatch.setattr(pe, "OVERALL_BUDGET_SECONDS", 0.3)
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    started = time.monotonic()
    result = pe.get_public_egress()
    release.set()
    assert time.monotonic() - started < 3
    assert result["ipv4"]["address"] == V4
    assert (result["ipv6"]["status"], result["ipv6"]["reason"]) == ("failed", "timeout")


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


# ─── 16: disagreement, stale ─────────────────────────────────────────────────


def test_different_answers_from_independent_providers_are_inconsistent(net):
    first, second = Net.NAMES
    net.answers[("ipv4", first)] = V4
    net.answers[("ipv4", second)] = V4_ALT
    net.down("ipv6", no_route())
    result = pe.get_public_egress()["ipv4"]
    assert result["status"] == "inconsistent"
    assert result["address"] is None
    assert result["observed"] == sorted([V4, V4_ALT])


def _refresh(monkeypatch):
    monkeypatch.setattr(pe, "MIN_REFRESH_INTERVAL_SECONDS", 0.0)
    return pe.get_public_egress(refresh=True)


@pytest.mark.parametrize("failure", ["no_route", "timeout", "private"])
def test_a_failing_refresh_keeps_the_cached_address_available(net, monkeypatch, failure):
    net.set_family("ipv4", V4)
    net.set_family("ipv6", V6)
    assert pe.get_public_egress()["ipv6"]["status"] == "available"
    if failure == "private":
        net.set_family("ipv6", "10.0.0.1")  # a non-public answer is discarded, never shown
    else:
        net.down("ipv6", no_route() if failure == "no_route" else TimeoutError())
    second = _refresh(monkeypatch)
    assert second["ipv6"]["status"] == "available"
    assert second["ipv6"]["address"] == V6
    assert second["ipv6"]["reason"] is None
    assert second["ipv4"]["status"] == "available"
    assert "stale" not in str(second)


def test_a_cached_address_never_expires_into_a_failure(net, monkeypatch):
    net.set_family("ipv6", V6)
    net.down("ipv4", no_route())
    pe.get_public_egress()
    net.down("ipv6", no_route())
    pe._known["ipv6"]["observed_at"] = "2000-01-01T00:00:00Z"  # noqa: SLF001
    assert _refresh(monkeypatch)["ipv6"]["status"] == "available"


def test_first_ever_failure_and_never_available_ipv6_keep_their_failure_states(net):
    net.down("ipv4", TimeoutError())
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "failed" and result["ipv4"]["address"] is None
    assert result["ipv6"]["status"] == "unavailable" and result["ipv6"]["address"] is None
    assert STORE == {}  # nothing cached, nothing stored


def test_a_confirmed_different_address_replaces_the_cache_and_drops_the_old_place(net, monkeypatch):
    places = {V4: "Bonn", V4_ALT: "Berlin"}
    monkeypatch.setattr(
        pe,
        "describe_address",
        lambda a: {"country_code": "DE", "country": "Germany", "city": places[a], "asn": None, "as_org": None},
    )
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    assert pe.get_public_egress()["ipv4"]["geo"]["city"] == "Bonn"
    net.set_family("ipv4", V4_ALT)
    changed = _refresh(monkeypatch)["ipv4"]
    assert changed["address"] == V4_ALT and changed["geo"]["city"] == "Berlin"
    net.down("ipv4", TimeoutError())
    kept = _refresh(monkeypatch)["ipv4"]
    assert kept["address"] == V4_ALT and kept["geo"]["city"] == "Berlin"


def test_the_cached_address_and_its_geo_survive_a_restart_with_a_failing_refresh(net, monkeypatch):
    monkeypatch.setattr(
        pe,
        "describe_address",
        lambda _a: {"country_code": "DE", "country": "Germany", "city": "Bonn", "asn": None, "as_org": None},
    )
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    pe.get_public_egress()
    assert STORE["state"]["ipv4"]["address"] == V4 and "ipv6" not in STORE["state"]
    pe.reset_state()  # restart: memory is gone, the store stays
    monkeypatch.setattr(pe, "describe_address", lambda _a: pytest.fail("geo must come from the cache"))
    net.down("ipv4", TimeoutError())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "available" and result["ipv4"]["address"] == V4
    assert result["ipv4"]["geo"]["city"] == "Bonn"
    assert result["ipv6"]["status"] == "unavailable"


def test_the_cached_address_is_persisted_in_the_settings_table(net, monkeypatch, tmp_path):
    from app.db import sqlite as sqlite_db
    from app.utils import runtime_settings

    monkeypatch.undo()  # the autouse stubs would hide the real store
    monkeypatch.setattr(pe, "describe_address", lambda _address: None)
    monkeypatch.setattr(runtime_settings, "_path_memo", None)
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "sqlite" / "justup.db"
    db_path.parent.mkdir()
    conn = sqlite_db.connect(db_path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    runtime_settings.clear_runtime_cache()
    pe.reset_state()
    net = Net(monkeypatch)
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    pe.get_public_egress()
    runtime_settings.clear_runtime_cache()
    stored = runtime_settings.get_runtime_setting(SettingKeys.PUBLIC_EGRESS_STATE)
    assert stored["ipv4"]["address"] == V4 and "ipv6" not in stored
    pe.reset_state()
    net.down("ipv4", TimeoutError())
    assert pe.get_public_egress()["ipv4"]["address"] == V4
    pe.reset_state()


def test_a_tampered_stored_state_is_ignored(net):
    STORE["state"] = {"ipv4": {"address": "10.0.0.1"}, "ipv6": {"address": V4}}  # private; wrong family
    net.down("ipv4", TimeoutError())
    net.down("ipv6", no_route())
    result = pe.get_public_egress()
    assert result["ipv4"]["status"] == "failed" and result["ipv6"]["status"] == "unavailable"


def test_geo_is_resolved_again_only_when_databases_or_settings_change(net, monkeypatch):
    seen: list[str] = []

    def describe(address):
        seen.append(address)
        return {"country_code": "DE", "country": "Germany", "city": "Bonn", "asn": None, "as_org": None}

    monkeypatch.setattr(pe, "describe_address", describe)
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    pe.get_public_egress()
    _refresh(monkeypatch)
    assert seen == [V4]
    monkeypatch.setattr(pe, "geoip_signature", lambda: (9, 2, 3))  # GeoLite2 file replaced
    _refresh(monkeypatch)
    assert seen == [V4, V4]


def test_a_failed_new_description_keeps_the_cached_one(net, monkeypatch):
    monkeypatch.setattr(
        pe,
        "describe_address",
        lambda _a: {"country_code": "DE", "country": "Germany", "city": "Bonn", "asn": None, "as_org": None},
    )
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    pe.get_public_egress()
    monkeypatch.setattr(pe, "geoip_signature", lambda: (9, 2, 3))
    monkeypatch.setattr(pe, "describe_address", lambda _a: None)
    assert _refresh(monkeypatch)["ipv4"]["geo"]["city"] == "Bonn"


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


def test_parallel_requests_share_one_lookup(net):
    gate = net.gate = threading.Event()
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
    assert not [c for c in net.connects if "proxy" in c[1]]


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
        "geo": None,
    }
    return {
        "ipv4": {
            **empty,
            "status": "available",
            "address": V4,
            "observed_at": "2026-11-05T19:00:00Z",
            "geo": {"country_code": "DE", "country": "Germany", "city": None, "asn": "AS3320", "as_org": "DTAG"},
        },
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
    assert [row for row in snapshot() if row not in before] == []  # only the cached address may be stored
    settings_payload = api["admin"].call("GET", "/settings").text
    assert V4 not in settings_payload and "egress" not in settings_payload.lower()
    assert V4 not in api["admin"].client.get("/healthz").text


# ─── geolocation of the found addresses ──────────────────────────────────────


def _decision(country: str | None = "DE", city: str | None = "Bonn"):
    if country is None:
        return gr.select([], [gr.SRC_GEOLITE2]), {}
    candidate = gr.build_candidate(
        gr.SRC_GEOLITE2, 50.73, 7.1, city=city, country_code=country, accuracy_radius_km=20.0
    )
    return gr.select([candidate], [gr.SRC_GEOLITE2]), {}


def _geo_env(
    monkeypatch,
    *,
    enhanced: bool,
    provider: bool = True,
    info=None,
    decision=None,
    country_only=None,
    local_asn=(None, None),
):
    asn_calls: list[str] = []
    monkeypatch.setattr(pe, "geolocate_asn", lambda _a: local_asn)
    monkeypatch.setattr(pe, "resolve_location", lambda _a: decision or _decision())
    monkeypatch.setattr(pe, "geolocate_country", lambda _a: country_only)
    monkeypatch.setattr(pe, "online_fallback_enabled", lambda: enhanced)
    monkeypatch.setattr(pe, "get_runtime_bool_fail_closed", lambda _k, _d: provider)
    monkeypatch.setattr(pe, "get_ip_api_key", lambda: "")
    monkeypatch.setattr(pe, "online_cache_ttl_seconds", lambda: 60)

    def fake_lookup(address, **_kw):
        asn_calls.append(address)
        return info

    monkeypatch.setattr(pe, "lookup_public_ip", fake_lookup)
    return asn_calls


ASN_INFO = {"asn": "AS3320", "provider": "Deutsche Telekom AG"}


def test_describe_address_combines_place_and_asn_with_enhanced_accuracy(monkeypatch):
    calls = _geo_env(monkeypatch, enhanced=True, info=ASN_INFO)
    geo = _REAL_DESCRIBE(V4)
    assert geo == {
        "country_code": "DE",
        "country": "Germany",
        "city": "Bonn",
        "asn": "AS3320",
        "as_org": "Deutsche Telekom AG",
    }
    assert calls == [V4]


def test_describe_address_without_enhanced_accuracy_sends_nothing_to_providers(monkeypatch):
    calls = _geo_env(monkeypatch, enhanced=False, info=ASN_INFO)
    geo = _REAL_DESCRIBE(V4)
    assert geo is not None and geo["asn"] is None and geo["as_org"] is None and geo["country_code"] == "DE"
    assert calls == []


def test_describe_address_takes_the_asn_from_the_local_file_without_enhanced_accuracy(monkeypatch):
    calls = _geo_env(monkeypatch, enhanced=False, info=ASN_INFO, local_asn=("AS64500", "Local Org"))
    geo = _REAL_DESCRIBE(V4)
    assert (geo["asn"], geo["as_org"]) == ("AS64500", "Local Org")
    assert calls == []


def test_describe_address_honours_the_provider_lookup_switch(monkeypatch):
    calls = _geo_env(monkeypatch, enhanced=True, provider=False, info=ASN_INFO)
    assert _REAL_DESCRIBE(V4)["asn"] is None
    assert calls == []


def test_describe_address_country_only_uses_the_local_country_record(monkeypatch):
    _geo_env(monkeypatch, enhanced=False, decision=_decision(None), country_only="FR")
    assert _REAL_DESCRIBE(V4) == {
        "country_code": "FR",
        "country": "France",
        "city": None,
        "asn": None,
        "as_org": None,
    }


def test_describe_address_without_any_data_is_none(monkeypatch):
    _geo_env(monkeypatch, enhanced=True, info=None, decision=_decision(None))
    assert _REAL_DESCRIBE(V4) is None


def test_describe_address_never_raises(monkeypatch):
    _geo_env(monkeypatch, enhanced=True, info=ASN_INFO)

    def boom(_address):
        raise RuntimeError("database broken")

    monkeypatch.setattr(pe, "resolve_location", boom)
    assert _REAL_DESCRIBE(V4) is None


def test_describe_address_private_address_and_missing_database_are_omitted():
    # Real resolver, no stubs: a private address is never looked up and nothing is raised.
    assert _REAL_DESCRIBE("10.1.2.3") is None
    assert _REAL_DESCRIBE("fd00::1") is None


def test_geo_follows_the_cache_lifecycle_and_only_covers_found_addresses(net, monkeypatch):
    seen: list[str] = []

    def describe(address):
        seen.append(address)
        return {"country_code": "DE", "country": "Germany", "city": None, "asn": None, "as_org": None}

    monkeypatch.setattr(pe, "describe_address", describe)
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    first = pe.get_public_egress()
    assert first["ipv4"]["geo"]["country_code"] == "DE"
    assert first["ipv6"]["geo"] is None and seen == [V4]  # no address, no description
    second = pe.get_public_egress()
    assert second["cached"] is True and second["ipv4"]["geo"] == first["ipv4"]["geo"]
    assert seen == [V4]  # served from the cache, no second description
    pe.get_public_egress(refresh=True)  # inside the minimum interval: still the cached result
    assert seen == [V4]


def test_a_hanging_geolocation_does_not_block_the_addresses(net, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(pe, "describe_address", lambda _a: release.wait(10) and None)
    monkeypatch.setattr(pe, "GEO_BUDGET_SECONDS", 0.2)
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    try:
        result = pe.get_public_egress()
        assert result["ipv4"]["status"] == "available" and result["ipv4"]["geo"] is None
    finally:
        release.set()


# ─── 22-23: template contract ────────────────────────────────────────────────


def test_network_fragment_places_the_card_after_geolocation_without_form_fields(env):  # noqa: F811
    resp = env["admin"].client.get(
        "/ui/fragments/settings-network", headers={"Authorization": f"Bearer {env['admin_token']}"}
    )
    assert resp.status_code == 200
    html = resp.text
    geo, egress = html.index('<h2 class="h6 mb-0">Geolocation</h2>'), html.index("Public outbound IPs")
    assert egress > geo
    card = html[html.rindex('<div class="card ', 0, egress) : html.index("</form>", egress)]
    assert 'data-endpoint="/settings/network/public-egress"' in card
    assert " name=" not in card and "<input" not in card  # nothing the settings autosave could pick up
    assert card.count('type="button"') == 3
    for label in ("Copy IPv4 address", "Copy IPv6 address", "Refresh", "Last checked"):
        assert label in card
    assert card.count("data-egress-geo") == 2  # one geo line per family, hidden until the API reports data
