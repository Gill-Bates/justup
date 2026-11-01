#!/usr/bin/env python3
#
# tests/test_ip_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""External IP lookup privacy, fallback and source contracts."""

# ruff: noqa: SLF001, ARG002 - white-box tests of private resolver state

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

import pytest

from app.services import geo_resolver as gr
from app.services import ip_lookup, provider_lookup
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture(autouse=True)
def clear_lookup_state():
    ip_lookup.reset_provider_state()
    yield
    ip_lookup.reset_provider_state()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Controllable monotonic clock for cache and breaker expiry (module-local, not global)."""
    now = [1000.0]
    monkeypatch.setattr(ip_lookup, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return HTTPError("https://example.invalid", code, "x", headers, None)


def _pro(**overrides):
    return {
        "status": "success",
        "lat": 0.0,
        "lon": 0.0,
        "city": "City A",
        "country": "Germany",
        "countryCode": "DE",
        "as": "AS123 Example",
        "isp": "Provider A",
        **overrides,
    }


def _who(**overrides):
    return {
        "success": True,
        "latitude": 51.0,
        "longitude": 7.0,
        "city": "City B",
        "country": "Germany",
        "country_code": "DE",
        "connection": {"asn": 456, "isp": "Provider B"},
        **overrides,
    }


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "2001:db8::1", "not-an-ip", None])
def test_non_public_input_never_reaches_provider(monkeypatch: pytest.MonkeyPatch, ip) -> None:
    monkeypatch.setattr(ip_lookup, "_fetch_json", lambda *_args: pytest.fail("unexpected request"))
    assert ip_lookup.lookup_public_ip(ip) is None


def test_https_transport_does_not_log_api_key_or_follow_redirects(monkeypatch, caplog) -> None:
    captured = {"urls": []}

    class Opener:
        def open(self, request, *, timeout):
            captured["urls"].append(request.full_url)
            captured["timeout"] = timeout
            raise HTTPError(request.full_url, 302, "redirect", {}, None)

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(ip_lookup, "build_opener", fake_build_opener)
    with caplog.at_level(logging.DEBUG, logger="app.services.ip_lookup"):
        assert ip_lookup.lookup_public_ip("8.8.8.8", api_key="private-test-key") is None
    assert any(url.startswith("https://pro.ip-api.com/json/8.8.8.8?") for url in captured["urls"])
    assert all(url.startswith("https://") for url in captured["urls"])
    assert any(isinstance(handler, HTTPRedirectHandler) for handler in captured["handlers"])
    assert "private-test-key" not in caplog.text


def test_all_available_providers_are_queried_even_when_one_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fetch(source, _ip, _key):
        calls.append(source)
        return _pro() if source == "ip-api-pro" else _who()

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    attempted, results = ip_lookup.lookup_online_sources("8.8.8.8", api_key="private-test-key")
    assert attempted == ("ip-api-pro", "ipwho.is")
    assert sorted(calls) == ["ip-api-pro", "ipwho.is"]
    assert results["ip-api-pro"]["city"] == "City A"
    assert results["ipwho.is"]["city"] == "City B"


def test_no_api_key_skips_pro_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(ip_lookup, "_fetch_json", lambda source, *_a: calls.append(source) or _who())
    attempted, results = ip_lookup.lookup_online_sources("8.8.8.8")
    assert attempted == ("ipwho.is",)
    assert calls == ["ipwho.is"] and set(results) == {"ipwho.is"}


def test_pro_failure_keeps_ipwhois_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ip_lookup, "_fetch_json", lambda source, *_a: None if source == "ip-api-pro" else _who())
    info = ip_lookup.lookup_public_ip("8.8.8.8", api_key="private-test-key")
    assert info["geo_source"] == info["network_source"] == "ipwho.is"
    assert (info["lat"], info["lon"], info["asn"]) == (51.0, 7.0, "AS456")


def test_network_metadata_merges_by_priority_without_mixing_location(monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch(source, _ip, _key):
        return _pro(city="", isp="") if source == "ip-api-pro" else _who(connection={"isp": "Provider B"})

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    info = ip_lookup.lookup_public_ip("8.8.8.8", api_key="private-test-key")
    assert (info["asn"], info["provider"]) == ("AS123", "Provider B")
    assert info["network_source"] == "ip-api-pro + ipwho.is"
    # Location fields all come from one provider; 0.0 is a valid coordinate.
    assert (info["lat"], info["lon"], info["city"], info["geo_source"]) == (0.0, 0.0, "", "ip-api-pro")


def test_http_429_with_retry_after_pauses_only_that_provider(monkeypatch, clock) -> None:
    calls = []

    def fetch(source, *_a):
        calls.append(source)
        if source == "ip-api-pro":
            raise ip_lookup.ProviderThrottledError(120.0)
        return _who()

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    _attempted, results = ip_lookup.lookup_online_sources("8.8.8.8", api_key="k")
    assert results["ip-api-pro"] is None and results["ipwho.is"]["city"] == "City B"
    clock[0] += 60  # inside the lock: no new Pro request, ipwho.is still served (cache)
    ip_lookup.lookup_online_sources("8.9.9.9", api_key="k")
    assert calls.count("ip-api-pro") == 1
    clock[0] += 61  # lock over: the provider takes part again
    ip_lookup.lookup_online_sources("8.7.7.7", api_key="k")
    assert calls.count("ip-api-pro") == 2


def test_http_429_is_not_cached_as_negative_result(monkeypatch, clock) -> None:
    state = {"throttled": True}

    def fetch(_source, *_a):
        if state["throttled"]:
            raise ip_lookup.ProviderThrottledError(None)
        return _who()

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    assert ip_lookup.lookup_public_ip("8.8.8.8") is None
    state["throttled"] = False
    clock[0] += ip_lookup._RETRY_AFTER_DEFAULT + 1
    assert ip_lookup.lookup_public_ip("8.8.8.8")["city"] == "City B"


def test_real_429_response_parses_retry_after(monkeypatch) -> None:
    class Opener:
        def open(self, request, *, timeout):
            raise _http_error(429, "30")

    monkeypatch.setattr(ip_lookup, "build_opener", lambda *_h: Opener())
    with pytest.raises(ip_lookup.ProviderThrottledError) as caught:
        ip_lookup._fetch_json("ipwho.is", "8.8.8.8", "")
    assert caught.value.retry_after == 30.0
    assert ip_lookup._parse_retry_after("garbage") is None
    assert ip_lookup._parse_retry_after("999999") == ip_lookup._RETRY_AFTER_MAX


def test_transient_failures_back_off_boundedly_and_recover(monkeypatch, clock) -> None:
    calls = []
    state = {"ok": False}

    def fetch(_source, *_a):
        calls.append(1)
        return _who() if state["ok"] else None

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    assert ip_lookup.lookup_public_ip("8.8.8.8") is None
    clock[0] += 5
    assert ip_lookup.lookup_public_ip("8.8.8.8") is None
    assert len(calls) == 1  # inside the backoff window
    clock[0] += 11
    assert ip_lookup.lookup_public_ip("8.8.8.8") is None  # second failure doubles the window
    clock[0] += 20
    assert len(calls) == 2
    state["ok"] = True
    clock[0] += 11
    assert ip_lookup.lookup_public_ip("8.8.8.8")["city"] == "City B"
    assert not ip_lookup._breaker  # a success clears the failure state


def test_failure_window_is_capped(monkeypatch, clock) -> None:
    monkeypatch.setattr(ip_lookup, "_fetch_json", lambda *_a: None)
    for _ in range(12):
        ip_lookup.lookup_public_ip("8.8.8.8")
        clock[0] += ip_lookup._BACKOFF_MAX + 1
    assert all(until - clock[0] <= ip_lookup._BACKOFF_MAX for until, _n in ip_lookup._breaker.values())


def test_same_ip_is_fetched_once_per_provider(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(ip_lookup, "_fetch_json", lambda source, *_a: calls.append(source) or _who())
    for _ in range(5):
        ip_lookup.lookup_public_ip("8.8.8.8")
    assert calls == ["ipwho.is"]


def test_concurrent_identical_requests_share_one_fetch(monkeypatch) -> None:
    started, release, calls = threading.Event(), threading.Event(), []

    def fetch(source, *_a):
        calls.append(source)
        started.set()
        release.wait(2)
        return _who()

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ip_lookup.lookup_public_ip("8.8.8.8"))) for _ in range(4)]
    for thread in threads:
        thread.start()
    started.wait(2)
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(3)
    assert calls == ["ipwho.is"]
    assert len(results) == 4 and all(r and r["city"] == "City B" for r in results)


def test_budget_returns_without_waiting_for_a_slow_provider(monkeypatch) -> None:
    release = threading.Event()

    def fetch(source, *_a):
        if source == "ip-api-pro":
            release.wait(2)
        return _who() if source == "ipwho.is" else _pro()

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    started = time.monotonic()
    _attempted, results = ip_lookup.lookup_online_sources("8.8.8.8", api_key="k", budget_seconds=0.2)
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 1.5
    assert results["ip-api-pro"] is None and results["ipwho.is"] is not None


def test_budget_is_derived_from_provider_timeout() -> None:
    assert ip_lookup.ONLINE_BUDGET_SECONDS == ip_lookup._TIMEOUT_SECONDS + 1.0


def test_key_change_cannot_reuse_previous_pro_result(monkeypatch) -> None:
    calls = []

    def fetch(source, _ip, key):
        if source != "ip-api-pro":
            return None
        calls.append(key)
        return _pro(isp=key)

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    first = ip_lookup.lookup_public_ip("8.8.8.8", api_key="first-key")
    second = ip_lookup.lookup_public_ip("8.8.8.8", api_key="second-key")
    assert first["provider"] == "first-key"
    assert second["provider"] == "second-key"
    assert calls == ["first-key", "second-key"]
    assert all("first-key" not in str(key) for key in [*ip_lookup._cache, *ip_lookup._breaker])


def test_provider_report_uses_shared_resolver_and_keeps_local_location(monkeypatch) -> None:
    candidate = gr.build_candidate(gr.SRC_GEOLITE2, 52.5, 13.4, city="Berlin", country_code="DE")
    monkeypatch.setattr(provider_lookup, "resolve_target_ip", lambda _host: "8.8.8.8")
    monkeypatch.setattr(provider_lookup, "lookup_public_ip", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider_lookup, "resolve_location", lambda _ip, _host: (gr.select([candidate], [gr.SRC_GEOLITE2]), {})
    )
    monkeypatch.setattr(provider_lookup, "get_runtime_bool_fail_closed", lambda *_args: True)
    monkeypatch.setattr(provider_lookup, "get_ip_api_key", lambda: "")
    monkeypatch.setattr(provider_lookup, "get_runtime_int", lambda *_args, **_kwargs: 1800)
    info = provider_lookup.lookup_provider_info("example.com")
    assert info == {
        "ip": "8.8.8.8",
        "asn": "-",
        "provider": "-",
        "location": "Berlin, DE",
        "country_code": "DE",
        "source": "GeoLite2",
        "location_accuracy": "Approximate; radius unavailable",
    }


def test_provider_report_shows_online_place_name_with_local_position(monkeypatch) -> None:
    coarse = gr.build_candidate(
        gr.SRC_GEOLITE2, 52.5, 13.4, city="Brandenburg", country_code="DE", city_is_region=True, accuracy_radius_km=200
    )
    online = gr.build_candidate(gr.SRC_IPWHO, 52.4, 13.0, city="Potsdam", country_code="DE")
    decision = gr.select([coarse, online], [gr.SRC_GEOLITE2, gr.SRC_IPWHO])
    monkeypatch.setattr(provider_lookup, "resolve_target_ip", lambda _host: "8.8.8.8")
    monkeypatch.setattr(provider_lookup, "lookup_public_ip", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider_lookup, "resolve_location", lambda _ip, _host: (decision, {}))
    monkeypatch.setattr(provider_lookup, "get_runtime_bool_fail_closed", lambda *_args: True)
    monkeypatch.setattr(provider_lookup, "get_ip_api_key", lambda: "")
    monkeypatch.setattr(provider_lookup, "get_runtime_int", lambda *_args, **_kwargs: 1800)
    info = provider_lookup.lookup_provider_info("example.com")
    assert info["location"] == "Potsdam, DE"
    assert info["source"] == "GeoLite2, place name ipwho.is"
    assert (decision.selected.latitude, decision.selected.longitude) == (52.5, 13.4)  # position unchanged


@pytest.mark.parametrize(
    ("place", "code", "expected"),
    [
        ("Berlin", "DE", "Berlin, DE"),
        ("Berlin, DE", "DE", "Berlin, DE"),
        ("DE", "DE", "DE"),
        (None, "de", "DE"),
        ("Berlin", "", "Berlin"),
    ],
)
def test_country_code_is_formatted_exactly_once(place, code, expected) -> None:
    assert gr.format_place(place, code) == expected
