#!/usr/bin/env python3
#
# tests/test_geo_asn_local.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Local GeoLite2-ASN lookup and its use for the public egress card; no network, a tiny hand-built mmdb."""

# ruff: noqa: SLF001, ARG001, PTH105 - white-box tests of module state; fixtures used for their side effects
from __future__ import annotations

import os

import pytest

from app.services import public_egress as pe
from app.services import traceroute_geo as tg
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests._mmdb_builder import build_asn_mmdb
from tests.test_api_hardening import _encryption_key, env  # noqa: F401 - fixtures reused for the API test
from tests.test_public_egress import V4, V6, Net, _decision, no_route

NETWORKS = {
    "93.184.216.0/24": (3320, "Deutsche Telekom AG"),
    "2606:2800::/32": (15133, "Edgecast Inc."),
    "8.8.8.0/24": (15169, None),  # record without an organisation
}


@pytest.fixture
def asn_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Data dir whose geoip/ folder is the only place the ASN file is looked up."""
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(tg, "_asn_reader", None)
    monkeypatch.setattr(tg, "_asn_stamp", None)
    monkeypatch.setattr(tg, "_asn_failed_stamp", None)
    yield tmp_path / "geoip"
    if tg._asn_reader is not None:
        tg._asn_reader.close()


def _write(directory, networks=NETWORKS):
    build_asn_mmdb(directory / "GeoLite2-ASN.mmdb", networks)


def _replace(directory, content: bytes) -> None:
    """Replace the file the way the updater does: new inode via rename, never an in-place rewrite."""
    tmp = directory / "geoip_tmp.mmdb.tmp"
    tmp.write_bytes(content)
    os.replace(tmp, directory / "GeoLite2-ASN.mmdb")


def test_lookup_returns_number_and_organisation_for_ipv4_and_ipv6(asn_dir) -> None:
    _write(asn_dir)
    assert tg.geolocate_asn(V4) == ("AS3320", "Deutsche Telekom AG")
    assert tg.geolocate_asn(V6) == ("AS15133", "Edgecast Inc.")
    assert tg.geolocate_asn("8.8.8.8") == ("AS15169", None)


def test_missing_file_gives_nothing_and_does_not_raise(asn_dir) -> None:
    assert tg.asn_signature() is None
    assert tg.geolocate_asn(V4) == (None, None)


def test_file_appearing_later_is_picked_up(asn_dir) -> None:
    assert tg.geolocate_asn(V4) == (None, None)
    _write(asn_dir)
    assert tg.geolocate_asn(V4) == ("AS3320", "Deutsche Telekom AG")


def test_corrupt_file_gives_nothing_and_does_not_raise(asn_dir) -> None:
    asn_dir.mkdir(parents=True)
    (asn_dir / "GeoLite2-ASN.mmdb").write_bytes(b"this is not a database" * 50)
    assert tg.geolocate_asn(V4) == (None, None)


def test_city_database_in_the_asn_slot_gives_nothing(asn_dir) -> None:
    build_asn_mmdb(asn_dir / "GeoLite2-ASN.mmdb", NETWORKS, database_type="GeoLite2-City")
    assert tg.geolocate_asn(V4) == (None, None)  # reader.asn() refuses a non-ASN database


def test_invalid_address_is_not_looked_up(asn_dir) -> None:
    _write(asn_dir)
    assert tg.geolocate_asn("not-an-ip") == (None, None) and tg.geolocate_asn("") == (None, None)


@pytest.mark.parametrize(
    "address",
    ["10.0.0.1", "192.168.1.5", "127.0.0.1", "169.254.1.1", "100.64.0.1", "224.0.0.1", "::1", "fe80::1", "fd00::1"],
)
def test_private_reserved_addresses_are_not_looked_up(asn_dir, address) -> None:
    _write(asn_dir, {"0.0.0.0/0": (1, "Everything")})
    assert tg.geolocate_asn(address) == (None, None)


def test_address_not_in_the_database_gives_nothing(asn_dir) -> None:
    _write(asn_dir)
    assert tg.geolocate_asn("1.1.1.1") == (None, None)


def test_replaced_file_is_reloaded(asn_dir) -> None:
    _write(asn_dir)
    assert tg.geolocate_asn(V4)[0] == "AS3320"
    before = tg.asn_signature()
    build_asn_mmdb(asn_dir / "new.tmp", {"93.184.216.0/24": (64500, "Replacement GmbH")})
    os.replace(asn_dir / "new.tmp", asn_dir / "GeoLite2-ASN.mmdb")
    assert tg.asn_signature() != before
    assert tg.geolocate_asn(V4) == ("AS64500", "Replacement GmbH")


def test_unreadable_replacement_keeps_the_previous_reader(asn_dir) -> None:
    _write(asn_dir)
    assert tg.geolocate_asn(V4)[0] == "AS3320"
    _replace(asn_dir, b"truncated download")
    assert tg.geolocate_asn(V4) == ("AS3320", "Deutsche Telekom AG")
    assert tg.geolocate_asn(V4) == ("AS3320", "Deutsche Telekom AG")
    (asn_dir / "GeoLite2-ASN.mmdb").unlink()
    assert tg.geolocate_asn(V4) == ("AS3320", "Deutsche Telekom AG")


def test_path_stays_inside_the_geoip_dir(asn_dir, tmp_path) -> None:
    assert tg._get_asn_path() == tmp_path / "geoip" / "GeoLite2-ASN.mmdb"


# ─── public egress enrichment ────────────────────────────────────────────────


@pytest.fixture
def egress(monkeypatch: pytest.MonkeyPatch, asn_dir):
    """Real ``describe_address`` with the real ASN reader; City resolution and the online lookup are stubbed."""
    state: dict = {"online_calls": [], "enhanced": False}
    monkeypatch.setattr(pe, "resolve_location", lambda _a: _decision())
    monkeypatch.setattr(pe, "geolocate_country", lambda _a: None)
    monkeypatch.setattr(pe, "online_fallback_enabled", lambda: state["enhanced"])
    monkeypatch.setattr(pe, "get_runtime_bool_fail_closed", lambda _k, _d: True)
    monkeypatch.setattr(pe, "get_ip_api_key", lambda: "")
    monkeypatch.setattr(pe, "online_cache_ttl_seconds", lambda: 60)

    def online(address, **_kw):
        state["online_calls"].append(address)
        return {"asn": "AS9999", "provider": "Online Org"}

    monkeypatch.setattr(pe, "lookup_public_ip", online)
    return state


def test_describe_uses_the_local_asn_without_enhanced_accuracy_and_without_network(egress, asn_dir) -> None:
    _write(asn_dir)
    geo = pe.describe_address(V4)
    assert geo is not None
    assert geo["asn"] == "AS3320" and geo["as_org"] == "Deutsche Telekom AG" and geo["city"] == "Bonn"
    assert egress["online_calls"] == []


def test_describe_without_the_file_has_geo_only(egress) -> None:
    geo = pe.describe_address(V4)
    assert geo == {"country_code": "DE", "country": "Germany", "city": "Bonn", "asn": None, "as_org": None}
    assert egress["online_calls"] == []


def test_describe_with_a_corrupt_file_has_geo_only(egress, asn_dir) -> None:
    asn_dir.mkdir(parents=True)
    (asn_dir / "GeoLite2-ASN.mmdb").write_bytes(b"garbage" * 100)
    geo = pe.describe_address(V4)
    assert geo is not None and geo["asn"] is None and geo["city"] == "Bonn"


def test_describe_private_address_gets_no_asn(egress, asn_dir) -> None:
    _write(asn_dir, {"10.0.0.0/8": (1, "Private")})
    geo = pe.describe_address("10.1.2.3")
    assert geo is not None and geo["asn"] is None


def test_local_value_wins_over_the_online_one_with_enhanced_accuracy(egress, asn_dir) -> None:
    _write(asn_dir)
    egress["enhanced"] = True
    geo = pe.describe_address(V4)
    assert (geo["asn"], geo["as_org"]) == ("AS3320", "Deutsche Telekom AG")
    assert egress["online_calls"] == []  # complete local answer: no request at all


def test_online_fills_only_what_the_local_file_lacks_and_only_with_enhanced_accuracy(egress, asn_dir) -> None:
    _write(asn_dir)
    assert pe.describe_address("8.8.8.8")["as_org"] is None  # off: no online fill
    assert egress["online_calls"] == []
    egress["enhanced"] = True
    geo = pe.describe_address("8.8.8.8")
    assert (geo["asn"], geo["as_org"]) == ("AS15169", "Online Org")  # number stays local
    assert egress["online_calls"] == ["8.8.8.8"]
    geo = pe.describe_address("1.1.1.1")  # not in the local file at all
    assert (geo["asn"], geo["as_org"]) == ("AS9999", "Online Org")


# ─── cache lifecycle ─────────────────────────────────────────────────────────


def test_geo_and_asn_are_resolved_again_when_the_asn_file_changes(egress, asn_dir, monkeypatch) -> None:
    net = Net(monkeypatch)
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    monkeypatch.setattr(pe, "geoip_signature", lambda: (1, 2, 3))
    seen: list[str] = []
    real = pe.describe_address

    def describe(address):
        seen.append(address)
        return real(address)

    monkeypatch.setattr(pe, "describe_address", describe)
    pe.reset_state()
    monkeypatch.setattr(pe, "get_runtime_setting", lambda _k, default=None: default)
    monkeypatch.setattr(pe, "store_runtime_setting", lambda _k, _v: True)
    first = pe.get_public_egress(refresh=True)["ipv4"]["geo"]
    assert first["asn"] is None and seen == [V4]
    _write(asn_dir)
    monkeypatch.setattr(pe, "MIN_REFRESH_INTERVAL_SECONDS", 0.0)
    second = pe.get_public_egress(refresh=True)["ipv4"]["geo"]
    assert seen == [V4, V4] and second["asn"] == "AS3320"
    pe.get_public_egress(refresh=True)
    assert seen == [V4, V4]  # unchanged files: the cached geo stands
    # an unreadable replacement never drops the cached IP or its geo
    _replace(asn_dir, b"broken")
    kept = pe.get_public_egress(refresh=True)["ipv4"]
    assert kept["address"] == V4 and kept["geo"]["asn"] == "AS3320"
    pe.reset_state()


# ─── real route ──────────────────────────────────────────────────────────────


def test_route_returns_asn_without_enhanced_accuracy_and_without_any_outbound_call(env, monkeypatch) -> None:  # noqa: F811
    from app.services import ip_lookup

    net = Net(monkeypatch)
    net.set_family("ipv4", V4)
    net.down("ipv6", no_route())
    outbound: list[str] = []

    def forbidden(*args, **_kw):
        outbound.append(repr(args)[:80])
        raise AssertionError("no third-party request may be made for the local ASN")

    monkeypatch.setattr(pe, "lookup_public_ip", forbidden)
    monkeypatch.setattr(ip_lookup, "lookup_public_ip", forbidden)
    monkeypatch.setattr(ip_lookup, "build_opener", forbidden)
    monkeypatch.setattr(tg, "_query_online", forbidden)
    monkeypatch.setattr(pe, "online_fallback_enabled", lambda: False)
    monkeypatch.setattr(pe, "resolve_location", lambda _a: _decision())
    monkeypatch.setattr(pe, "geolocate_country", lambda _a: None)
    monkeypatch.setattr(tg, "_asn_reader", None)
    monkeypatch.setattr(tg, "_asn_stamp", None)
    monkeypatch.setattr(tg, "_asn_failed_stamp", None)
    monkeypatch.setattr(pe, "MIN_REFRESH_INTERVAL_SECONDS", 0.0)
    pe.reset_state()
    data_dir = env["db_path"].parent.parent  # the env fixture points JUSTUP_DATA_DIR at its temp dir
    assert os.environ["JUSTUP_DATA_DIR"] == str(data_dir)
    try:
        # file absent: geo only, no error
        body = env["admin"].call("GET", "/settings/network/public-egress?refresh=true").json()
        geo = body["ipv4"]["geo"]
        assert body["ipv4"]["status"] == "available" and geo["city"] == "Bonn" and geo["asn"] is None
        # file present: asn fields in the real response
        _write(data_dir / "geoip")
        body = env["admin"].call("GET", "/settings/network/public-egress?refresh=true").json()
        geo = body["ipv4"]["geo"]
        assert (geo["asn"], geo["as_org"], geo["city"]) == ("AS3320", "Deutsche Telekom AG", "Bonn")
        assert outbound == []
    finally:
        if tg._asn_reader is not None:
            tg._asn_reader.close()
        pe.reset_state()
