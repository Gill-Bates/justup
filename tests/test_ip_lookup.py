#!/usr/bin/env python3
#
# tests/test_ip_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""External IP lookup privacy, fallback and source contracts."""

from __future__ import annotations

import logging
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler

import pytest

from app.services import ip_lookup, provider_lookup
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture(autouse=True)
def clear_lookup_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ip_lookup, "_cache", ip_lookup.OrderedDict())


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
    assert captured["urls"][0].startswith("https://pro.ip-api.com/json/8.8.8.8?")
    assert all(url.startswith("https://") for url in captured["urls"])
    assert any(isinstance(handler, HTTPRedirectHandler) for handler in captured["handlers"])
    assert "private-test-key" not in caplog.text


def test_pro_failure_falls_back_to_ipwhois(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fetch(source, _ip, _key):
        calls.append(source)
        return None if source == "ip-api-pro" else _who()

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    info = ip_lookup.lookup_public_ip("8.8.8.8", api_key="private-test-key")
    assert calls == ["ip-api-pro", "ipwho.is"]
    assert info["geo_source"] == info["network_source"] == "ipwho.is"
    assert (info["lat"], info["lon"], info["asn"]) == (51.0, 7.0, "AS456")


def test_complementary_provider_fields_keep_location_from_one_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch(source, _ip, _key):
        if source == "ip-api-pro":
            return _pro(city="", isp="")
        return _who(connection={"isp": "Provider B"})

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    info = ip_lookup.lookup_public_ip("8.8.8.8", api_key="private-test-key")
    assert (info["lat"], info["lon"], info["city"]) == (51.0, 7.0, "City B")
    assert info["geo_source"] == "ipwho.is"
    assert (info["asn"], info["provider"]) == ("AS123", "Provider B")
    assert info["network_source"] == "ip-api-pro + ipwho.is"


def test_partial_city_without_coordinates_is_not_paired_with_primary_coordinates(monkeypatch) -> None:
    monkeypatch.setattr(
        ip_lookup,
        "_fetch_json",
        lambda source, _ip, _key: _pro(city="") if source == "ip-api-pro" else _who(latitude=None, longitude=None),
    )
    info = ip_lookup.lookup_public_ip("8.8.8.8", api_key="private-test-key")
    assert (info["lat"], info["lon"], info["city"]) == (0.0, 0.0, "")
    assert info["geo_source"] == "ip-api-pro"


def test_failed_provider_cache_expires_before_successful_fallback(monkeypatch) -> None:
    clock = [0.0]
    calls = []
    monkeypatch.setattr(ip_lookup.time, "monotonic", lambda: clock[0])

    def fetch(source, _ip, _key):
        calls.append(source)
        return None if source == "ip-api-pro" else _who()

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    for moment in (0.0, 299.0, 301.0):
        clock[0] = moment
        assert ip_lookup.lookup_public_ip("8.8.8.8", api_key="private-test-key", ttl_seconds=600)
    assert calls == ["ip-api-pro", "ipwho.is", "ip-api-pro"]


def test_key_change_cannot_reuse_previous_pro_result(monkeypatch) -> None:
    calls = []

    def fetch(_source, _ip, key):
        calls.append(key)
        return _pro(isp=key)

    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    first = ip_lookup.lookup_public_ip("8.8.8.8", api_key="first-key")
    second = ip_lookup.lookup_public_ip("8.8.8.8", api_key="second-key")
    assert first["provider"] == "first-key"
    assert second["provider"] == "second-key"
    assert calls == ["first-key", "second-key"]


def test_provider_report_retains_offline_location_when_online_fails(monkeypatch) -> None:
    monkeypatch.setattr(provider_lookup, "resolve_target_ip", lambda _host: "8.8.8.8")
    monkeypatch.setattr(provider_lookup, "lookup_public_ip", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider_lookup,
        "geolocate_ip",
        lambda _ip: {"city": "Berlin", "country": "DE"},
    )
    monkeypatch.setattr(provider_lookup, "get_runtime_bool", lambda *_args: True)
    monkeypatch.setattr(provider_lookup, "get_ip_api_key", lambda: "")
    monkeypatch.setattr(provider_lookup, "get_runtime_int", lambda *_args, **_kwargs: 1800)
    info = provider_lookup.lookup_provider_info("example.com")
    assert info == {
        "ip": "8.8.8.8",
        "asn": "-",
        "provider": "-",
        "location": "Berlin",
        "country_code": "DE",
        "source": "GeoLite2",
    }
