#!/usr/bin/env python3
#
# tests/test_geo_resolver.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Quality matrix, determinism and fallback contracts of the shared geo resolver (no network)."""

# ruff: noqa: SLF001, ARG001, ARG005 - white-box tests of private resolver state

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

import pytest

from app.services import geo_resolver as gr
from app.services import ip_lookup, traceroute_geo
from app.utils.constants import GeoSource, SettingKeys
from tests import _support  # noqa: F401 - imported for its sys.path side effect

BERLIN = (52.52, 13.40)
MUNICH = (48.14, 11.58)


def _cand(source: str, pos=BERLIN, **kwargs: Any) -> gr.GeoCandidate:
    kwargs.setdefault("city", "Berlin")
    kwargs.setdefault("country_code", "DE")
    candidate = gr.build_candidate(source, pos[0], pos[1], **kwargs)
    assert candidate is not None
    return candidate


def _geolite(pos=BERLIN, radius=10, **kwargs):
    return _cand(gr.SRC_GEOLITE2, pos, accuracy_radius_km=radius, **kwargs)


ALL = [gr.SRC_GEOLITE2, gr.SRC_POP, gr.SRC_IP_API_PRO, gr.SRC_IPWHO]


# --- Pure selection -----------------------------------------------------------------------------


def test_equal_quality_prefers_geolite2_and_is_not_precise_from_agreement() -> None:
    decision = gr.select([_cand(gr.SRC_IPWHO), _cand(gr.SRC_IP_API_PRO), _geolite(radius=None)], ALL)
    assert decision.selected.source == gr.SRC_GEOLITE2
    assert "no evidenced accuracy" in decision.selection_reason
    # Agreeing online providers plus a radius-less GeoLite2 never become "precise" (invariant F).
    assert decision.accuracy_class == gr.ACCURACY_APPROXIMATE


def test_precise_geolite2_beats_contradicting_online_candidate() -> None:
    online = _cand(gr.SRC_IPWHO, MUNICH, city="Munich")
    decision = gr.select([online, _geolite()], ALL)
    assert decision.selected.source == gr.SRC_GEOLITE2
    assert decision.accuracy_class == gr.ACCURACY_PRECISE
    assert decision.contradictions and "ipwho.is" in decision.contradictions[0]


def test_coarse_geolite2_keeps_its_position_and_takes_only_the_place_name_from_online() -> None:
    coarse = _geolite(radius=200, city="Brandenburg", city_is_region=True)
    online = _cand(gr.SRC_IPWHO, (52.4, 13.0), city="Potsdam")
    decision = gr.select([coarse, online], ALL)
    assert decision.selected.source == gr.SRC_GEOLITE2  # position, source and accuracy class stay local
    assert (decision.selected.latitude, decision.selected.longitude) == BERLIN
    assert decision.place is not None and decision.place.source == gr.SRC_IPWHO
    assert decision.place_name == "Potsdam"
    assert decision.accuracy_class == gr.ACCURACY_APPROXIMATE
    assert "text only" in decision.selection_reason
    assert decision.as_dict()["place_source"] == gr.SRC_IPWHO


def test_online_never_replaces_an_imprecise_local_position() -> None:
    limited = _geolite(radius=100)
    online = _cand(gr.SRC_IPWHO, (48.0, 11.0), city="Elsewhere")
    decision = gr.select([online, limited], ALL)
    assert decision.selected is limited and decision.place is None
    assert decision.contradictions  # the disagreement is reported, not resolved by guessing


def test_online_city_in_other_country_does_not_replace_coarse_geolite2() -> None:
    coarse = _geolite(radius=200, city="Brandenburg", city_is_region=True)
    other = _cand(gr.SRC_IPWHO, (52.4, 13.0), city="Elsewhere", country_code="PL")
    assert gr.select([coarse, other], ALL).selected.source == gr.SRC_GEOLITE2


def test_unique_high_quality_pop_is_rated_on_its_own_evidence() -> None:
    pop = _cand(gr.SRC_POP, MUNICH, city="Munich", confidence=95)
    decision = gr.select([pop, _cand(gr.SRC_IPWHO)], ALL)
    assert decision.selected.source == gr.SRC_POP
    assert decision.accuracy_class == gr.ACCURACY_PRECISE


def test_unverified_pop_is_never_precise() -> None:
    pop = _cand(gr.SRC_POP, MUNICH, city="Munich", confidence=gr.normalize_pop_confidence("medium"))
    decision = gr.select([pop], ALL)
    assert decision.selected.source == gr.SRC_POP
    assert decision.accuracy_class == gr.ACCURACY_APPROXIMATE


def test_contradicting_precise_claims_are_downgraded_not_averaged() -> None:
    pop = _cand(gr.SRC_POP, MUNICH, city="Munich", confidence=95)
    decision = gr.select([pop, _geolite()], ALL)
    assert decision.selected.source == gr.SRC_GEOLITE2  # tie-breaker, coordinates untouched
    assert (decision.selected.latitude, decision.selected.longitude) == BERLIN
    assert decision.accuracy_class == gr.ACCURACY_APPROXIMATE
    assert "downgraded" in decision.selection_reason


def test_zero_coordinates_are_valid() -> None:
    candidate = gr.build_candidate(
        gr.SRC_GEOLITE2, 0.0, 0.0, city="Null Island", country_code="GH", accuracy_radius_km=5
    )
    assert candidate is not None
    decision = gr.select([candidate], [gr.SRC_GEOLITE2])
    assert (decision.selected.latitude, decision.selected.longitude) == (0.0, 0.0)
    assert decision.accuracy_class == gr.ACCURACY_PRECISE


@pytest.mark.parametrize(("lat", "lon"), [(91, 0), (0, 181), (float("nan"), 0), (None, 1), (True, 1), ("1", "2")])
def test_invalid_coordinates_discard_the_candidate(lat, lon) -> None:
    assert gr.build_candidate(gr.SRC_IPWHO, lat, lon, city="X", country_code="DE") is None


def test_no_candidates_is_a_clean_unknown_state() -> None:
    decision = gr.select([], [gr.SRC_GEOLITE2, gr.SRC_IPWHO])
    assert decision.selected is None
    assert decision.accuracy_class == gr.ACCURACY_UNKNOWN
    assert decision.sources_succeeded == ()
    assert decision.is_fallback is False


def test_decision_is_independent_of_candidate_arrival_order() -> None:
    cands = [
        _geolite(radius=200, city="Brandenburg", city_is_region=True),
        _cand(gr.SRC_IP_API_PRO, (52.4, 13.0), city="Potsdam"),
        _cand(gr.SRC_IPWHO, MUNICH, city="Munich"),
        _cand(gr.SRC_POP, BERLIN, confidence=70),
    ]
    outcomes = {gr.select(order, ALL) for order in itertools.permutations(cands)}
    assert len(outcomes) == 1


def test_metadata_with_only_geolite2_and_fallback_flag() -> None:
    only = gr.select([_geolite()], [gr.SRC_GEOLITE2])
    assert only.sources_considered == (gr.SRC_GEOLITE2,) and only.sources_succeeded == (gr.SRC_GEOLITE2,)
    assert only.is_fallback is False
    fell_back = gr.select([_geolite()], ALL)
    assert fell_back.is_fallback is True
    assert fell_back.as_dict()["selected_source"] == gr.SRC_GEOLITE2


def test_unproven_online_never_outranks_evidenced_radius() -> None:
    assert gr.quality_key(_geolite(radius=100)) > gr.quality_key(_cand(gr.SRC_IP_API_PRO))
    assert gr.quality_key(_geolite(radius=10)) > gr.quality_key(_geolite(radius=100))


# --- Resolver integration (collect -> validate -> select) --------------------------------------------


class _Env:
    """Fakes for every source; records which online lookups were requested."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.online_calls: list[str] = []
        self.geolite: dict[str, Any] | None = None
        self.pop: dict[str, Any] | None = None
        self.online: dict[str, dict[str, Any] | None] = {}
        self.switch = True  # online_geo_fallback_enabled
        self.delay = 0.0  # simulated provider latency
        monkeypatch.setattr(
            traceroute_geo,
            "get_runtime_bool_fail_closed",
            lambda key, default: self.switch if key == SettingKeys.ONLINE_GEO_FALLBACK_ENABLED else default,
        )
        monkeypatch.setattr(traceroute_geo, "geolocate_ip", lambda ip: self.geolite)
        monkeypatch.setattr(traceroute_geo, "infer_location_from_hostname", lambda host: self.pop)
        monkeypatch.setattr(traceroute_geo, "get_ip_api_key", lambda: "")
        monkeypatch.setattr(traceroute_geo, "_online_ttl_seconds", lambda: 600)
        monkeypatch.setattr(traceroute_geo, "lookup_online_sources", self._lookup)

    def _lookup(self, ip: str, **_kwargs: Any):
        self.online_calls.append(ip)
        time.sleep(self.delay)
        return tuple(self.online), dict(self.online)


def _info(city="Munich", lat=MUNICH[0], lon=MUNICH[1], cc="DE", source="ipwho.is") -> dict[str, Any]:
    return {"lat": lat, "lon": lon, "city": city, "country_code": cc, "country": cc, "geo_source": source}


LOCAL_HIGH = {"lat": BERLIN[0], "lon": BERLIN[1], "city": "Berlin", "country": "DE", "accuracy_radius_km": 10}
LOCAL = {**LOCAL_HIGH, "accuracy_radius_km": 100}  # limited quality: the online fallback is allowed


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Env:
    return _Env(monkeypatch)


def test_both_online_sources_fail_geolite2_is_returned(env: _Env) -> None:
    env.geolite = dict(LOCAL)
    env.online = {"ip-api-pro": None, "ipwho.is": None}
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert loc["city"] == "Berlin"
    decision = loc[traceroute_geo.META_DECISION]
    assert decision["is_fallback"] is True and decision["selected_source"] == gr.SRC_GEOLITE2
    assert decision["sources_succeeded"] == [gr.SRC_GEOLITE2]


def test_one_provider_success_does_not_hide_the_other(env: _Env) -> None:
    env.geolite = dict(LOCAL)
    env.online = {"ip-api-pro": _info(source="ip-api-pro"), "ipwho.is": _info()}
    decision = traceroute_geo.resolve_location("8.8.8.8")[0]
    assert set(decision.sources_succeeded) == {gr.SRC_GEOLITE2, gr.SRC_IP_API_PRO, gr.SRC_IPWHO}


def test_online_provider_with_invalid_coordinates_is_discarded(env: _Env) -> None:
    env.geolite = dict(LOCAL)
    env.online = {"ipwho.is": _info(lat=200.0)}
    decision = traceroute_geo.resolve_location("8.8.8.8")[0]
    assert decision.sources_succeeded == (gr.SRC_GEOLITE2,)
    assert decision.selected.city == "Berlin"


@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.0.1", "127.0.0.1", "169.254.1.1", "fd00::1", "::1"])
def test_private_ips_never_trigger_online_requests(env: _Env, ip: str) -> None:
    env.online = {"ipwho.is": _info()}
    traceroute_geo.geolocate_with_hostname_hint(ip, "core1.example.net")
    assert env.online_calls == []


def test_ipv6_public_address_is_resolved(env: _Env) -> None:
    env.geolite = dict(LOCAL)
    env.online = {"ipwho.is": _info()}
    loc = traceroute_geo.geolocate_with_hostname_hint("2606:4700:4700::1111")
    assert loc["city"] == "Berlin"
    assert env.online_calls == ["2606:4700:4700::1111"]
    assert traceroute_geo.geolocate_with_hostname_hint("not-an-ip") is None


def test_missing_geolite2_degrades_to_online_text_not_precision(env: _Env) -> None:
    env.online = {"ipwho.is": _info()}
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert loc["city"] == "Munich"
    assert traceroute_geo._classify_location(loc)[1] == "approximate"


def test_everything_missing_yields_unknown(env: _Env) -> None:
    assert traceroute_geo.geolocate_with_hostname_hint("8.8.8.8") is None
    decision = traceroute_geo.resolve_location("8.8.8.8")[0]
    assert decision.accuracy_class == gr.ACCURACY_UNKNOWN


def test_online_helper_crash_keeps_geolite2(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.geolite = dict(LOCAL)

    def boom(*_a: Any, **_k: Any):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(traceroute_geo, "lookup_online_sources", boom)
    assert traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")["city"] == "Berlin"


def test_pop_candidate_is_rated_by_normalised_confidence(env: _Env) -> None:
    env.pop = {
        "city": "Munich",
        "country": "DE",
        "lat": MUNICH[0],
        "lon": MUNICH[1],
        "confidence": "high",
        "source": "db",
    }
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8", "muc1.example.net")
    assert loc["city"] == "Munich" and loc[traceroute_geo.META_CONFIDENCE] == traceroute_geo.CONFIDENCE_HOSTNAME
    assert loc["accuracy_class"] == gr.ACCURACY_PRECISE
    env.pop["confidence"] = "medium"
    assert traceroute_geo.geolocate_with_hostname_hint("8.8.8.8", "muc1.example.net")["accuracy_class"] == "approximate"


def test_route_model_and_geo_points_agree_for_identical_input(env: _Env) -> None:
    env.geolite = dict(LOCAL_HIGH)
    env.online = {"ipwho.is": _info()}
    hops = [{"hop": 1, "ip": "8.8.8.8", "status": "ok"}]
    node = traceroute_geo.build_route_model(hops)[0]
    points = traceroute_geo.mtr_hops_to_geo_points(hops)
    assert node["mappable"] is True and len(points) == 1
    assert (node["location"]["lat"], node["location"]["city"]) == (points[0]["lat"], points[0]["city"])


def test_destination_with_zero_coordinates_is_kept(env: _Env) -> None:
    dest = {"lat": 0.0, "lon": 0.0, "city": "Null Island", "country": "GH", "accuracy_radius_km": 5}
    nodes = traceroute_geo.build_route_model([{"hop": 1, "ip": "8.8.8.8", "status": "ok"}], "1.1.1.1", dest)
    assert nodes[-1]["location"]["lat"] == 0.0


# --- GeoLite2-first quality table and the conditional online fallback --------------------------------


def _local(radius: float | None = 10, **overrides: Any) -> dict[str, Any]:
    return {**LOCAL_HIGH, "accuracy_radius_km": radius, **overrides}


@pytest.mark.parametrize(
    ("record", "quality", "online_expected"),
    [
        (_local(10), gr.QUALITY_HIGH, False),
        (_local(25.0), gr.QUALITY_HIGH, False),  # boundary is inclusive
        (_local(25.01), gr.QUALITY_LIMITED, True),
        (_local(100), gr.QUALITY_LIMITED, True),
        (_local(None), gr.QUALITY_UNEVIDENCED, True),  # city + country but no radius
        (_local(10, city_is_region=True), gr.QUALITY_COARSE, True),  # region/state only
        (_local(10, city=None, city_is_region=False), gr.QUALITY_VERY_COARSE, True),  # country only
        (None, gr.QUALITY_UNUSABLE, True),  # no valid coordinates
    ],
)
def test_geolite2_quality_table_decides_the_online_call(env: _Env, record, quality, online_expected) -> None:
    env.geolite = record
    env.online = {"ipwho.is": _info()}
    candidate = None
    if record is not None:
        candidate = gr.build_candidate(
            gr.SRC_GEOLITE2,
            record["lat"],
            record["lon"],
            city=record["city"],
            country_code=record["country"],
            accuracy_radius_km=record["accuracy_radius_km"],
            city_is_region=record.get("city_is_region", False),
        )
    assert gr.geolite2_quality(candidate) == quality
    traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert (env.online_calls == ["8.8.8.8"]) is online_expected


def test_radius_limit_is_one_documented_constant() -> None:
    assert gr.MAX_CITY_RADIUS_KM == 25


def test_high_quality_geolite2_makes_no_online_call_and_is_precise(env: _Env) -> None:
    env.geolite = dict(LOCAL_HIGH)
    env.online = {"ip-api-pro": _info(source="ip-api-pro"), "ipwho.is": _info()}
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert env.online_calls == []
    assert loc["city"] == "Berlin" and loc["accuracy_class"] == gr.ACCURACY_PRECISE


def test_online_result_never_displaces_the_limited_geolite2_position(env: _Env) -> None:
    env.geolite = dict(LOCAL)
    env.online = {"ipwho.is": _info()}  # Munich, ~500 km away, no evidenced accuracy
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert env.online_calls == ["8.8.8.8"]
    assert (loc["lat"], loc["lon"], loc["city"]) == (BERLIN[0], BERLIN[1], "Berlin")
    assert loc[traceroute_geo.META_CONFIDENCE] == traceroute_geo.CONFIDENCE_GEOIP
    assert loc[traceroute_geo.META_DECISION]["contradictions"]  # reported, position unchanged
    assert loc["accuracy_class"] == gr.ACCURACY_APPROXIMATE


def test_coarse_geolite2_gets_only_the_online_place_name(env: _Env) -> None:
    env.geolite = _local(200, city="Brandenburg", city_is_region=True)
    env.online = {"ipwho.is": _info(city="Potsdam", lat=52.4, lon=13.0)}
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert (loc["lat"], loc["lon"]) == BERLIN
    assert loc["city"] == "Potsdam" and loc["place_text_source"] == "ipwho.is"
    assert loc["city_is_region"] is True and loc["accuracy_class"] == gr.ACCURACY_APPROXIMATE


def test_missing_geolite2_uses_online_but_unlocated_stays_approximate(env: _Env) -> None:
    env.online = {"ipwho.is": _info()}
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert env.online_calls == ["8.8.8.8"] and loc["accuracy_class"] == gr.ACCURACY_APPROXIMATE


def test_verified_pop_avoids_the_online_call_but_a_hostname_guess_does_not(env: _Env) -> None:
    env.geolite = _local(None)  # insufficient GeoLite2
    env.online = {"ipwho.is": _info()}
    env.pop = {
        "city": "Berlin",
        "country": "DE",
        "lat": BERLIN[0],
        "lon": BERLIN[1],
        "confidence": "high",
        "source": "clli",
    }
    traceroute_geo.geolocate_with_hostname_hint("8.8.8.8", "ber1.example.net")
    assert env.online_calls == []
    env.pop["confidence"] = "medium"  # city only guessed from the hostname
    traceroute_geo.geolocate_with_hostname_hint("8.8.8.8", "ber1.example.net")
    assert env.online_calls == ["8.8.8.8"]


def test_switch_off_blocks_every_online_call_even_for_poor_geolite2(env: _Env) -> None:
    env.geolite = _local(None)
    env.online = {"ipwho.is": _info()}
    env.switch = False
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert env.online_calls == [] and loc["city"] == "Berlin"
    env.switch = True  # takes effect on the very next lookup, no restart
    traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert env.online_calls == ["8.8.8.8"]
    env.switch = False
    traceroute_geo.geolocate_with_hostname_hint("1.1.1.1")
    assert env.online_calls == ["8.8.8.8"]


def test_http_429_with_retry_after_falls_back_to_geolite2_without_hammering(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fetch(source: str, ip: str, _key: str):
        calls.append(ip)
        raise ip_lookup.ProviderThrottledError(120)

    ip_lookup.reset_provider_state()
    monkeypatch.setattr(ip_lookup, "_fetch_json", fetch)
    monkeypatch.setattr(traceroute_geo, "lookup_online_sources", ip_lookup.lookup_online_sources)
    env.geolite = dict(LOCAL)
    for ip in ("8.8.8.8", "8.8.4.4"):
        loc = traceroute_geo.geolocate_with_hostname_hint(ip)
        assert loc["city"] == "Berlin"
    assert calls == ["8.8.8.8"]  # the lock period from Retry-After stops further requests
    ip_lookup.reset_provider_state()


# --- Route-level online phase (bounded, deduplicated, deterministic) ---------------------------------


def _hops(count: int, duplicate_last: bool = False) -> list[dict[str, Any]]:
    hops = [{"hop": i + 1, "ip": f"8.8.{i // 250}.{i % 250 + 1}", "status": "ok"} for i in range(count)]
    if duplicate_last:
        hops.append({"hop": count + 1, "ip": hops[-1]["ip"], "status": "ok"})
    return hops


def test_route_with_many_distinct_ips_is_looked_up_concurrently_and_deduplicated(env: _Env) -> None:
    env.geolite = dict(LOCAL)
    env.online = {"ipwho.is": _info()}
    env.delay = 0.2
    hops = _hops(12, duplicate_last=True)
    started = time.monotonic()
    nodes = traceroute_geo.build_route_model(hops)
    elapsed = time.monotonic() - started
    assert len(nodes) == 13
    assert sorted(env.online_calls) == sorted({h["ip"] for h in hops})  # one request per distinct IP
    assert elapsed < 1.5  # sequential would take 12 x 0.2 s = 2.4 s; 4 workers need about 0.6 s


def test_route_online_budget_caps_the_total_time_and_keeps_local_results(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.geolite = dict(LOCAL)
    env.online = {"ipwho.is": _info()}
    env.delay = 0.6
    monkeypatch.setattr(traceroute_geo, "ROUTE_ONLINE_BUDGET_SECONDS", 0.2)
    started = time.monotonic()
    nodes = traceroute_geo.build_route_model(_hops(8))
    assert time.monotonic() - started < 1.0
    # GeoLite2 results stay usable: every hop keeps its local classification
    assert len(nodes) == 8 and all(n["geo_source"] == GeoSource.GEOIP_COARSE for n in nodes)


def test_route_result_does_not_depend_on_online_response_order(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.geolite = _local(200, city="Brandenburg", city_is_region=True)
    env.online = {"ipwho.is": _info(city="Potsdam", lat=52.4, lon=13.0)}
    hops = _hops(6)
    env.delay = 0.0
    baseline = traceroute_geo.build_route_model(hops)
    reverse_delay = {h["ip"]: 0.05 * (6 - i) for i, h in enumerate(hops)}
    original = env._lookup

    def staggered(ip: str, **kwargs: Any):
        time.sleep(reverse_delay[ip])
        return original(ip, **kwargs)

    monkeypatch.setattr(traceroute_geo, "lookup_online_sources", staggered)
    assert traceroute_geo.build_route_model(hops) == baseline


# --- GeoLite2 hot reload ---------------------------------------------------------------------------------


class _FakeReader:
    """Raises when used after close, like the MaxMind reader; returns its own id as latitude."""

    instances = 0

    def __init__(self, _path: str) -> None:
        type(self).instances += 1
        self.ident = type(self).instances
        self.closed = False

    def city(self, _ip: str):
        if self.closed:
            raise ValueError("Attempt to read from a closed MaxMind DB.")
        time.sleep(0.001)
        ns = type("N", (), {})
        record = ns()
        record.location = type("L", (), {"latitude": float(self.ident), "longitude": 1.0, "accuracy_radius": 10})()
        record.city = type("C", (), {"name": "Berlin"})()
        record.country = type("K", (), {"iso_code": "DE"})()
        record.subdivisions = None
        return record

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def geodb(monkeypatch: pytest.MonkeyPatch, tmp_path):
    db = tmp_path / "GeoLite2-City.mmdb"
    db.write_bytes(b"v1")
    _FakeReader.instances = 0
    monkeypatch.setattr(traceroute_geo, "_HAS_GEOIP", True)
    monkeypatch.setattr(traceroute_geo, "_get_geoip_path", lambda: db)
    monkeypatch.setattr(traceroute_geo.geoip2.database, "Reader", _FakeReader)
    monkeypatch.setattr(traceroute_geo, "_reader", None)
    monkeypatch.setattr(traceroute_geo, "_reader_stamp", None)
    monkeypatch.setattr(traceroute_geo, "_geo_cache", {})

    def replace(content: bytes) -> None:
        db.write_bytes(content)  # a different size is enough for the stamp to change

    return replace


def test_replaced_database_invalidates_cached_results(geodb) -> None:
    assert traceroute_geo.geolocate_ip("8.8.8.8")["lat"] == 1.0
    assert traceroute_geo.geolocate_ip("8.8.8.8")["lat"] == 1.0  # served from the cache
    geodb(b"version-two")
    assert traceroute_geo.geolocate_ip("8.8.8.8")["lat"] == 2.0  # a cached IP notices the swap


def test_country_lookup_accepts_record_without_coordinates(geodb, monkeypatch) -> None:
    monkeypatch.setattr(
        traceroute_geo, "_read_record", lambda _reader, _ip: (1, {"country": "DE", "lat": None, "lon": None})
    )
    assert traceroute_geo.geolocate_country("8.8.8.8") == "DE"
    assert traceroute_geo.geolocate_country("127.0.0.1") is None
    monkeypatch.setattr(traceroute_geo, "_read_record", lambda _reader, _ip: (1, {"country": "ZZ"}))
    assert traceroute_geo.geolocate_country("8.8.8.8") is None


def test_anycast_record_with_only_registered_country_yields_no_country(monkeypatch) -> None:
    # GeoLite2 gives anycast resolvers (1.1.1.1, 9.9.9.9) a registered_country but no country.
    from types import SimpleNamespace as Ns

    class Reader:
        def city(self, _ip):
            return Ns(
                location=Ns(latitude=None, longitude=None, accuracy_radius=None),
                city=Ns(name=None),
                country=Ns(iso_code=None),
                registered_country=Ns(iso_code="AU"),
                subdivisions=Ns(most_specific=Ns(name=None)),
            )

    monkeypatch.setattr(traceroute_geo, "_reader", None)
    monkeypatch.setattr(traceroute_geo, "_get_reader", Reader)
    _, record = traceroute_geo._read_record(Reader(), "1.1.1.1")
    assert record["country"] is None
    assert traceroute_geo.geolocate_country("1.1.1.1") is None


def test_lookup_holding_a_replaced_reader_retries_and_caches_nothing_stale(geodb, monkeypatch) -> None:
    stale = traceroute_geo._get_reader()  # thread A fetched the reader ...
    real_get = traceroute_geo._get_reader
    calls = {"n": 0}

    def get_reader():
        calls["n"] += 1
        if calls["n"] == 1:  # ... while thread B swaps the database and closes the old reader
            geodb(b"version-two")
            real_get()
            return stale
        return real_get()

    monkeypatch.setattr(traceroute_geo, "_get_reader", get_reader)
    assert stale.closed is False
    result = traceroute_geo.geolocate_ip("8.8.8.8")
    assert stale.closed is True and result is not None and result["lat"] == 2.0
    assert traceroute_geo._geo_cache["8.8.8.8"]["lat"] == 2.0


def test_concurrent_reloads_never_lose_or_cache_a_wrong_result(geodb) -> None:
    errors: list[str] = []
    stop = threading.Event()
    workers = 6
    done = [0] * workers  # completed lookups per worker (each worker writes only its own slot)

    def worker(n: int) -> None:
        while not stop.is_set():
            ip = f"8.8.{n}.{time.monotonic_ns() % 200 + 1}"
            if traceroute_geo.geolocate_ip(ip) is None:
                errors.append(ip)
            done[n] += 1

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    try:
        for version in range(15):
            geodb(b"x" * (version + 10))
            # geolocate_ip() gives up after a few swapped-reader retries, so a swap must not overtake the same
            # lookup repeatedly: every worker finishes a lookup after one swap before the next one.
            seen = list(done)
            deadline = time.monotonic() + 10
            while any(d <= s for d, s in zip(done, seen, strict=True)) and time.monotonic() < deadline:
                time.sleep(0.001)
            assert time.monotonic() < deadline, "lookups stalled"
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
    alive = [t.name for t in threads if t.is_alive()]
    assert not alive, f"geo lookup workers did not stop: {alive}"
    assert errors == []  # no transient failure is returned as "unknown"
    assert all(v is not None for v in traceroute_geo._geo_cache.values())
    latest = traceroute_geo._get_reader().ident
    assert all(v["lat"] == float(latest) for v in traceroute_geo._geo_cache.values())  # no pre-swap entry survived


@pytest.mark.parametrize(
    ("radius", "switch", "http_expected"),
    [(25.0, True, 0), (25.01, True, 2), (25.01, False, 0), (None, False, 0)],
)
def test_http_requests_follow_the_25_km_rule_and_the_switch(
    env: _Env, monkeypatch: pytest.MonkeyPatch, radius: float | None, switch: bool, http_expected: int
) -> None:
    http_calls: list[str] = []
    ip_lookup.reset_provider_state()
    monkeypatch.setattr(ip_lookup, "_fetch_json", lambda source, ip, _key: http_calls.append(source) or None)
    monkeypatch.setattr(traceroute_geo, "lookup_online_sources", ip_lookup.lookup_online_sources)
    monkeypatch.setattr(traceroute_geo, "get_ip_api_key", lambda: "k")
    env.geolite = _local(radius)
    env.switch = switch
    loc = traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert len(http_calls) == http_expected
    assert (loc["lat"], loc["lon"]) == BERLIN  # a failed or skipped fallback keeps the local position
    ip_lookup.reset_provider_state()
