#!/usr/bin/env python3
#
# tests/test_ixp_geolocation.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001, ARG005
"""IXP prefix geolocation: snapshot import, index consistency, refresh safety and hop presentation.

PeeringDB is never contacted: every table is a fixture served through a fake ``_fetch_with_retry``.
"""

from __future__ import annotations

import ipaddress
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app.db import ixp_prefixes
from app.db import sqlite as sqlite_db
from app.services import geo_resolver as gr
from app.services import ixp_import, ixp_index, pop_codes, pop_refresh, provider_lookup, traceroute_geo
from app.services.pdf_route_map import is_approximate
from app.utils import maintenance
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect

METRO_IP = "80.81.192.10"  # inside the metro exchange prefix
METRO_IP_2 = "80.81.192.99"
DISTRIBUTED_IP = "193.0.0.10"  # inside the two-country exchange prefix
V6_IP = "2001:7f8::10"


def _fac(fac_id: int, city: str, country: str, lat: float, lon: float) -> dict:
    return {"id": fac_id, "status": "ok", "city": city, "country": country, "latitude": lat, "longitude": lon}


def _tables(*, include_facilities: bool = True) -> dict[str, list[dict]]:
    """Exchange 1: two Frankfurt facilities (metro). Exchange 2: administrative DE, facilities in DE and NL."""
    return {
        "ix": [
            {"id": 1, "status": "ok", "name": "Metro-IX", "city": "Frankfurt", "country": "DE"},
            {"id": 2, "status": "ok", "name": "Euro-IX", "city": "Frankfurt", "country": "DE"},
        ],
        "ixlan": [{"id": 10, "status": "ok", "ix_id": 1}, {"id": 20, "status": "ok", "ix_id": 2}],
        "ixpfx": [
            {"id": 100, "status": "ok", "ixlan_id": 10, "protocol": "IPv4", "prefix": "80.81.192.0/22"},
            {"id": 101, "status": "ok", "ixlan_id": 10, "protocol": "IPv6", "prefix": "2001:7f8::/64"},
            {"id": 200, "status": "ok", "ixlan_id": 20, "protocol": "IPv4", "prefix": "193.0.0.0/21"},
        ],
        "ixfac": [
            {"id": 1, "status": "ok", "ix_id": 1, "fac_id": 1000},
            {"id": 2, "status": "ok", "ix_id": 1, "fac_id": 1001},
            {"id": 3, "status": "ok", "ix_id": 2, "fac_id": 1000},
            {"id": 4, "status": "ok", "ix_id": 2, "fac_id": 1002},
        ],
        "fac": [
            _fac(1000, "Frankfurt", "DE", 50.11, 8.68),
            _fac(1001, "Frankfurt", "DE", 50.12, 8.70),
            _fac(1002, "Amsterdam", "NL", 52.37, 4.90),
        ]
        if include_facilities
        else [],
    }


def _build(tables: dict[str, list[dict]] | None = None) -> ixp_import.IxpBuildResult:
    t = tables or _tables()
    return ixp_import.build_snapshot(t["ix"], t["ixlan"], t["ixpfx"], t["ixfac"], t["fac"])


def _index(result: ixp_import.IxpBuildResult | None = None) -> ixp_index.IxpIndex:
    snapshot = (result or _build()).snapshot
    return ixp_index.IxpIndex(snapshot.exchanges, snapshot.prefixes)


@pytest.fixture(autouse=True)
def _clean_index():
    ixp_index.reset_for_tests()
    yield
    ixp_index.reset_for_tests()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    sqlite_db.init_schema(c)
    yield c
    c.close()


# Import: administrative seat vs. evidenced region (P2.4)


def test_metro_exchange_gets_a_region_from_facilities() -> None:
    evidence = _index().lookup(ipaddress.ip_address(METRO_IP))
    assert evidence is not None and evidence.location_class == ixp_prefixes.LOC_METRO
    assert (evidence.city, evidence.country) == ("Frankfurt", "DE")
    assert evidence.position() is not None


def test_multi_country_exchange_keeps_its_administrative_seat_but_evidences_no_country() -> None:
    result = _build()
    exchange = next(e for e in result.snapshot.exchanges if e.ix_id == 2)
    assert exchange.location_class == ixp_prefixes.LOC_DISTRIBUTED
    assert exchange.country == "DE"  # administrative record, stored for reference only
    assert exchange.ref_country is None
    evidence = _index(result).lookup(ipaddress.ip_address(DISTRIBUTED_IP))
    assert evidence is not None and evidence.country is None and evidence.city is None
    assert evidence.position() is None


def test_malformed_prefix_protocol_is_excluded_instead_of_aborting_the_import() -> None:
    tables = _tables()
    tables["ixpfx"].append({"id": 300, "status": "ok", "ixlan_id": 10, "protocol": ["IPv4"], "prefix": "80.82.0.0/22"})
    result = _build(tables)
    assert result.excluded["pfx_inconsistent"] == 1
    assert len(result.snapshot.prefixes) == 3


def test_ipv6_and_mapped_addresses_match_their_prefix() -> None:
    ixp_index.install(_index())
    assert ixp_index.lookup(V6_IP).name == "Metro-IX"
    assert ixp_index.lookup(f"::ffff:{METRO_IP}").name == "Metro-IX"
    assert ixp_index.lookup("8.8.8.8") is None and ixp_index.lookup("not-an-ip") is None


# Hop presentation: table, route model, map points and PDF read one decision (P2.4, P2.5)


def _hop(n: int, ip: str) -> dict:
    return {"hop": n, "ip": ip, "hostname": None, "status": "ok", "latency_ms": {"avg": n}}


@pytest.fixture
def ixp_installed(monkeypatch) -> None:
    ixp_index.install(_index())
    monkeypatch.setattr(traceroute_geo, "geolocate_ip", lambda ip: None)


def test_distributed_exchange_names_no_country_or_city_as_hop_position(ixp_installed) -> None:
    loc = traceroute_geo.geolocate_with_hostname_hint(DISTRIBUTED_IP)
    assert loc is not None and loc["ixp"]["name"] == "Euro-IX"  # still recognised as an IXP
    assert loc["lat"] is None and loc["lon"] is None and loc["country"] is None and loc["city"] is None
    view = traceroute_geo.location_view(loc)
    assert (view["city"], view["country"], view["ixp"]) == (None, None, "Euro-IX")
    hops = [_hop(1, DISTRIBUTED_IP)]
    assert traceroute_geo.route_hops_to_geo_points(hops, destination_ip=DISTRIBUTED_IP) == []
    node = traceroute_geo.build_route_model(hops, destination_ip=DISTRIBUTED_IP)[0]
    assert node[traceroute_geo.RM_LOCATION]["country"] is None
    assert node[traceroute_geo.RM_GEO_SOURCE] == str(traceroute_geo.GeoSource.IXP)
    assert node[traceroute_geo.RM_MAP_ROLE] == traceroute_geo.MAP_ROLE_NONE


@pytest.mark.parametrize("destination_ip", [METRO_IP_2, "203.0.113.77"])
def test_ixp_region_is_labelled_estimated_for_intermediate_and_final_hop(ixp_installed, destination_ip) -> None:
    hops = [_hop(1, METRO_IP), _hop(2, METRO_IP_2)]
    if destination_ip != METRO_IP_2:
        hops = [_hop(1, METRO_IP)]  # destination is not on the map; the intermediate IXP hop must stay an estimate
    points = traceroute_geo.route_hops_to_geo_points(hops, destination_ip=destination_ip)
    first = points[0]
    assert first[traceroute_geo.META_MAP_ROLE] == traceroute_geo.MAP_ROLE_ESTIMATE
    for point in points:
        assert point["_approximate"] is True and is_approximate(point)
        assert point["loc_precision"] == traceroute_geo.LOC_APPROXIMATE
        assert gr.IXP_SOURCE_LABEL in point[traceroute_geo.META_LOC_NOTE]
        assert "Metro-IX" in point[traceroute_geo.META_LOC_NOTE]
        assert (point["city"], point["country"]) == ("Frankfurt", "DE")
    if destination_ip == METRO_IP_2:
        last = points[-1]
        # The destination keeps its special marker role; its uncertainty is carried by the same flags.
        assert last[traceroute_geo.META_DESTINATION] is True
        assert last[traceroute_geo.META_MAP_ROLE] == traceroute_geo.MAP_ROLE_ROUTE
        assert first["segment_to_next"] == traceroute_geo.SEGMENT_DASHED
        nodes = traceroute_geo.build_route_model(hops, destination_ip=destination_ip)
        assert {n[traceroute_geo.RM_ACCURACY] for n in nodes} == {str(traceroute_geo.GeoAccuracy.APPROXIMATE)}
        assert {n[traceroute_geo.RM_GEO_SOURCE] for n in nodes} == {str(traceroute_geo.GeoSource.IXP)}
        assert [n[traceroute_geo.RM_MAP_ROLE] for n in nodes] == [p[traceroute_geo.META_MAP_ROLE] for p in points]


def test_ixp_source_label_is_one_display_contract() -> None:
    assert gr.IXP_SOURCE_LABEL == "PeeringDB IXP prefix"
    assert provider_lookup._source_label(gr.SRC_IXP) == "PeeringDB IXP prefix"
    note = traceroute_geo.location_note({traceroute_geo.META_CONFIDENCE: traceroute_geo.CONFIDENCE_IXP, "ixp": {}})
    assert note is not None and note.startswith("IXP, source PeeringDB IXP prefix")


# Index: older loaders never overwrite a newer index (P2.2)


@pytest.fixture
def lazy_db(monkeypatch):
    """Lazy loads open a sentinel connection; ``load_from_connection`` is replaced per test."""
    monkeypatch.setattr(pop_codes, "open_pop_connection", lambda: "lazy-connection")
    monkeypatch.setattr(ixp_index.sqlite_db, "close_connection", lambda c: True)


def _old_and_new() -> tuple[ixp_index.IxpIndex, ixp_index.IxpIndex]:
    old_tables = _tables()
    old_tables["ix"][0]["name"] = "Old-IX"
    return _index(_build(old_tables)), _index()


def test_explicit_reload_wins_over_a_lazy_loader_that_read_the_old_snapshot(lazy_db, monkeypatch) -> None:
    old, new = _old_and_new()
    started, release = threading.Event(), threading.Event()

    def load(conn):
        if conn == "lazy-connection":
            started.set()
            assert release.wait(5)
            return old
        return new

    monkeypatch.setattr(ixp_index, "load_from_connection", load)
    worker = threading.Thread(target=lambda: ixp_index.lookup(METRO_IP))
    worker.start()
    try:
        assert started.wait(5)
        assert ixp_index.reload_from_connection("explicit-connection") is True  # refresh installs the new snapshot
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert ixp_index.lookup(METRO_IP).name == "Metro-IX"  # the late old loader did not overwrite it
    assert ixp_index._dirty is False


def test_invalidation_during_a_lazy_load_discards_its_result_and_reloads(lazy_db, monkeypatch) -> None:
    old, new = _old_and_new()
    started, release = threading.Event(), threading.Event()
    calls = []

    def load(conn):
        calls.append(conn)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            return old
        return new

    monkeypatch.setattr(ixp_index, "load_from_connection", load)
    worker = threading.Thread(target=lambda: ixp_index.lookup(METRO_IP))
    worker.start()
    try:
        assert started.wait(5)
        ixp_index.invalidate()
    finally:
        release.set()
        worker.join(5)
    assert ixp_index._dirty is True  # the stale result was not installed as current
    assert ixp_index.lookup(METRO_IP).name == "Metro-IX"  # next lookup reloads the fresh data
    assert len(calls) == 2


def test_older_explicit_reload_never_overwrites_a_newer_one(monkeypatch) -> None:
    old, new = _old_and_new()
    started, release = threading.Event(), threading.Event()

    def load(conn):
        if conn == "older":
            started.set()
            assert release.wait(5)
            return old
        return new

    monkeypatch.setattr(ixp_index, "load_from_connection", load)
    results: list[bool] = []
    worker = threading.Thread(target=lambda: results.append(ixp_index.reload_from_connection("older")))
    worker.start()
    try:
        assert started.wait(5)
        assert ixp_index.reload_from_connection("newer") is True
    finally:
        release.set()
        worker.join(5)
    assert results == [False]
    assert ixp_index.lookup(METRO_IP).name == "Metro-IX"


def test_failed_reload_keeps_the_active_index_and_queues_a_retry(monkeypatch) -> None:
    ixp_index.install(_index())

    def boom(conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ixp_index, "load_from_connection", boom)
    assert ixp_index.reload_from_connection(object()) is False
    assert ixp_index._dirty is True
    assert ixp_index._index.lookup(ipaddress.ip_address(METRO_IP)).name == "Metro-IX"


# Refresh: fetch completeness and publication after commit (P2.1, P2.3)


@pytest.fixture
def refresh_db(tmp_path, monkeypatch):
    """File-backed database so a second connection can prove what is committed."""
    path = tmp_path / "ixp.db"
    c = sqlite3.connect(path, check_same_thread=False)
    sqlite_db.init_schema(c)
    c.commit()

    @contextmanager
    def _same(_get_conn):
        yield c

    monkeypatch.setattr(pop_refresh, "_db_connection", _same)
    maintenance.end_maintenance()
    yield path, c
    c.close()


def _serve(monkeypatch, tables: dict[str, list[dict]], *, page_size: int = 2, fail=None, no_data: str | None = None):
    """Fake PeeringDB: honours limit/skip; ``fail(table, skip)`` may raise, ``no_data`` drops ``data`` for a table."""
    monkeypatch.setattr(pop_refresh, "PEERINGDB_PAGE_SIZE", page_size)
    seen: list[tuple[str, int]] = []

    def fetch(url, **_kwargs):
        parsed = urlparse(url)
        table = parsed.path.rsplit("/", 1)[-1]
        query = parse_qs(parsed.query)
        limit, skip = int(query["limit"][0]), int(query["skip"][0])
        assert limit == page_size
        seen.append((table, skip))
        if fail is not None:
            fail(table, skip)
        if table == no_data:
            return {"meta": {}}
        return {"data": tables[table][skip : skip + limit]}

    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", fetch)
    return seen


def _stored_prefixes(c: sqlite3.Connection) -> int:
    return c.execute("SELECT COUNT(*) FROM ixp_prefixes").fetchone()[0]


def test_fac_is_paginated_completely_and_feeds_the_exchange_regions(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    tables = _tables()
    seen = _serve(monkeypatch, tables)
    result = pop_refresh._do_peeringdb_refresh(lambda: c)
    assert result.error is None
    assert ("fac", 0) in seen and ("fac", 2) in seen  # the second fac page was requested (3 rows, page size 2)
    assert ixp_prefixes.snapshot_status(c)["prefix_count"] == 3
    assert ixp_index.lookup(METRO_IP).position() is not None  # the third facility contributed to the exchanges


def test_fac_response_without_data_is_rejected_and_the_snapshot_stays(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _serve(monkeypatch, _tables())
    assert pop_refresh._do_peeringdb_refresh(lambda: c).error is None
    before = (ixp_prefixes.snapshot_status(c), _stored_prefixes(c), ixp_index._index)
    _serve(monkeypatch, _tables(), no_data="fac")
    result = pop_refresh._do_peeringdb_refresh(lambda: c)
    assert result.error and "phase=fetch_facilities" in result.error and "no data list" in result.error
    assert (ixp_prefixes.snapshot_status(c), _stored_prefixes(c), ixp_index._index) == before


def test_interrupted_fac_pagination_keeps_the_previous_snapshot(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _serve(monkeypatch, _tables())
    assert pop_refresh._do_peeringdb_refresh(lambda: c).error is None
    before = (ixp_prefixes.snapshot_status(c), _stored_prefixes(c), ixp_index._index)

    def cut(table, skip):
        if table == "fac" and skip == 2:
            raise httpx_error()

    _serve(monkeypatch, _tables(), fail=cut)
    result = pop_refresh._do_peeringdb_refresh(lambda: c)
    assert result.error and "phase=fetch_facilities" in result.error
    assert (ixp_prefixes.snapshot_status(c), _stored_prefixes(c), ixp_index._index) == before


def httpx_error() -> Exception:
    return RuntimeError("connection reset while paging")


def test_snapshot_without_any_evidenced_region_does_not_replace_a_complete_one(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _serve(monkeypatch, _tables())
    assert pop_refresh._do_peeringdb_refresh(lambda: c).error is None
    status = ixp_prefixes.snapshot_status(c)
    assert c.execute("SELECT COUNT(*) FROM ixp_exchanges WHERE location_class='metro'").fetchone()[0] == 1
    unusable = _tables()
    unusable["fac"] = [_fac(1000, "", "DE", 50.11, 8.68)]  # a facility without a city proves nothing
    _serve(monkeypatch, unusable)
    result = pop_refresh._do_peeringdb_refresh(lambda: c)
    assert result.error and "phase=fetch_ixp" in result.error and "no exchange region" in result.error
    assert ixp_prefixes.snapshot_status(c) == status
    assert ixp_index.lookup(METRO_IP).position() is not None  # still serving the complete snapshot


def test_empty_fac_table_never_reaches_the_snapshot_builder(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _serve(monkeypatch, _tables(include_facilities=False))
    result = pop_refresh._do_peeringdb_refresh(lambda: c)
    assert result.error and "fac table is empty" in result.error
    assert ixp_prefixes.snapshot_status(c) is None


def test_region_shrink_against_the_stored_snapshot_is_rejected() -> None:
    def exchange(i: int, located: bool) -> ixp_prefixes.IxpExchange:
        cls = ixp_prefixes.LOC_METRO if located else ixp_prefixes.LOC_UNKNOWN
        return ixp_prefixes.IxpExchange(i, f"IX{i}", None, None, cls)

    prefixes = tuple(ixp_prefixes.IxpPrefix(f"198.51.{i}.0/24", 4, 24, i, i) for i in range(1, 11))
    snapshot = ixp_prefixes.IxpSnapshot(tuple(exchange(i, i <= 2) for i in range(1, 11)), prefixes)
    with pytest.raises(ixp_prefixes.IxpSnapshotRejectedError, match="regions shrank"):
        ixp_prefixes.check_plausible(snapshot, 0, previous_located_count=200)
    ixp_prefixes.check_plausible(snapshot, 0, previous_located_count=3)  # small stores are not guarded


def test_index_is_published_only_after_the_commit_is_visible_to_other_connections(refresh_db, monkeypatch) -> None:
    path, c = refresh_db
    _serve(monkeypatch, _tables())
    observed: list[object] = []
    real_reload = ixp_index.reload_from_connection

    def spy(conn):
        other = sqlite3.connect(path)
        try:
            observed.append(ixp_prefixes.snapshot_status(other))  # what a second reader sees at publication time
        finally:
            other.close()
        return real_reload(conn)

    monkeypatch.setattr(ixp_index, "reload_from_connection", spy)
    assert pop_refresh._do_peeringdb_refresh(lambda: c).error is None
    assert observed and observed[0] is not None and observed[0]["prefix_count"] == 3


def test_failed_commit_leaves_database_and_index_untouched(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _serve(monkeypatch, _tables())
    assert pop_refresh._do_peeringdb_refresh(lambda: c).error is None
    active = ixp_index._index
    before = (ixp_prefixes.snapshot_status(c), _stored_prefixes(c))
    c.execute("CREATE TRIGGER boom BEFORE UPDATE ON ixp_snapshot BEGIN SELECT RAISE(ABORT, 'boom'); END")
    c.commit()
    changed = _tables()
    extra_prefix = {"id": 300, "status": "ok", "ixlan_id": 10, "protocol": "IPv4", "prefix": "80.81.200.0/24"}
    changed["ixpfx"] = [*changed["ixpfx"][:2], extra_prefix]
    _serve(monkeypatch, changed)
    result = pop_refresh._do_peeringdb_refresh(lambda: c)
    assert result.error and "phase=apply_ixp" in result.error
    assert not c.in_transaction
    assert (ixp_prefixes.snapshot_status(c), _stored_prefixes(c)) == before
    assert ixp_index._index is active  # the runtime index never saw the rolled-back data


# Upgrade of an existing installation: the first IXP import is not held back by a fresh facility import


V4_DECIX = "80.81.192.39"
V6_DECIX = "2001:7f8::10"


def _decix_tables() -> dict[str, list[dict]]:
    tables = _tables()
    tables["ix"][0] = {"id": 1, "status": "ok", "name": "DE-CIX Frankfurt", "city": "Frankfurt", "country": "DE"}
    tables["ixpfx"][0] = {"id": 100, "status": "ok", "ixlan_id": 10, "protocol": "IPv4", "prefix": "80.81.192.0/21"}
    return tables


def _old_facility_import(c: sqlite3.Connection, *, days: int = 1) -> None:
    """The state of an installation before the IXP tables existed: a successful PeeringDB run, no snapshot."""
    stamp = (utcnow() - timedelta(days=days)).isoformat()
    c.execute(
        "INSERT INTO pop_import_runs (run_id, source, started_at, finished_at, status)"
        " VALUES (?, 'peeringdb', ?, ?, 'completed')",
        ("old-facility-run", stamp, stamp),
    )
    c.commit()


def test_upgrade_installation_imports_the_ixp_snapshot_on_its_own(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _old_facility_import(c)
    assert ixp_prefixes.snapshot_status(c) is None
    assert ixp_index.lookup(V4_DECIX) is None  # no snapshot: no IXP match, the GeoIP answer would stand

    plan = pop_refresh.plan_next_refresh(c, include_jitter=False)
    assert plan.delay_seconds <= pop_refresh.POP_STARTUP_JITTER_MAX_SECONDS and "IXP" in plan.reason

    _serve(monkeypatch, _decix_tables())
    results = pop_refresh.refresh_external_sources(lambda: c)  # exactly what the scheduler loop calls
    assert [r.error for r in results] == [None]

    assert ixp_prefixes.snapshot_status(c)["prefix_count"] == 3
    for ip in (V4_DECIX, V6_DECIX):
        evidence = ixp_index.lookup(ip)
        assert evidence is not None
        assert (evidence.name, evidence.location_class, evidence.city, evidence.country) == (
            "DE-CIX Frankfurt",
            ixp_prefixes.LOC_METRO,
            "Frankfurt",
            "DE",
        )

    later = pop_refresh.plan_next_refresh(c, include_jitter=False)
    assert later.delay_seconds > 20 * 24 * 3600 and "IXP" not in later.reason  # no refresh loop afterwards


def test_ixp_failure_after_the_upgrade_backs_off_and_keeps_the_facility_import(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _old_facility_import(c)

    def rate_limited(table, skip):
        if table == "ixpfx":
            raise RuntimeError("[ratelimit] Rate limited after 3 retries")

    _serve(monkeypatch, _decix_tables(), fail=rate_limited)
    result = pop_refresh.refresh_external_sources(lambda: c)[0]
    assert result.error and "phase=fetch_ixp" in result.error
    assert ixp_prefixes.snapshot_status(c) is None
    # facility import still applied: the fixture has facilities in two cities (Frankfurt, Amsterdam)
    assert c.execute("SELECT COUNT(*) FROM pop_city_evidence").fetchone()[0] == 2

    plan = pop_refresh.plan_next_refresh(c, include_jitter=False)
    assert "backoff" in plan.reason
    assert plan.delay_seconds > pop_refresh.POP_BACKOFF_BASE_SECONDS - 120  # no busy loop against PeeringDB


def test_ixp_failure_keeps_the_previous_snapshot_and_retries_with_growing_backoff(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    _serve(monkeypatch, _decix_tables())
    assert pop_refresh.refresh_external_sources(lambda: c)[0].error is None
    before = (ixp_prefixes.snapshot_status(c), _stored_prefixes(c), ixp_index._index)

    def down(table, skip):
        if table == "ix":
            raise RuntimeError("connection reset")

    _serve(monkeypatch, _decix_tables(), fail=down)
    delays = []
    for _ in range(2):
        assert pop_refresh.refresh_external_sources(lambda: c)[0].error
        assert (ixp_prefixes.snapshot_status(c), _stored_prefixes(c), ixp_index._index) == before
        delays.append(pop_refresh.plan_next_refresh(c, include_jitter=False).delay_seconds)
    assert 0 < delays[0] <= pop_refresh.POP_BACKOFF_BASE_SECONDS
    assert delays[1] > delays[0]
    assert ixp_index.lookup(V4_DECIX).name == "DE-CIX Frankfurt"


def test_fresh_ixp_snapshot_triggers_no_new_import_before_the_interval(refresh_db, monkeypatch) -> None:
    _path, c = refresh_db
    seen = _serve(monkeypatch, _decix_tables())
    assert pop_refresh.refresh_external_sources(lambda: c)[0].error is None
    requests = len(seen)
    assert requests > 0
    assert pop_refresh.plan_next_refresh(c, include_jitter=False).delay_seconds > 20 * 24 * 3600
    assert len(seen) == requests  # planning alone never talks to PeeringDB


# Geo version: the identity browser and server caches use to notice changed IXP data


def test_geo_version_follows_the_active_ixp_data_not_the_reload_count() -> None:
    assert ixp_index.geo_version() == ixp_index.geo_version() == "none"
    ixp_index.install(_index())
    first = ixp_index.geo_version()
    assert first != "none"
    ixp_index.install(_index())  # identical data published again (a periodic re-import)
    assert ixp_index.geo_version() == first
    changed = _decix_tables()
    ixp_index.install(_index(_build(changed)))
    assert ixp_index.geo_version() not in {first, "none"}
    ixp_index.install(None)
    assert ixp_index.geo_version() == "none"


# A hop whose GeoIP answer says IL is replaced by the IXP region once the snapshot exists


def _israeli_geoip(ip: str):
    return {
        "lat": 32.08,
        "lon": 34.78,
        "city": "Tel Aviv",
        "country": "IL",
        "accuracy_radius_km": 50.0,
        "city_is_region": False,
    }


@pytest.mark.parametrize("ip", [V4_DECIX, V6_DECIX])
def test_conflicting_geoip_country_is_replaced_by_the_ixp_region(monkeypatch, ip) -> None:
    monkeypatch.setattr(traceroute_geo, "geolocate_ip", _israeli_geoip)
    monkeypatch.setattr(traceroute_geo, "_online_fallback_enabled", lambda: False)
    before = traceroute_geo.location_view(traceroute_geo.geolocate_with_hostname_hint(ip))
    assert before["country"] == "IL"  # no snapshot: only GeoIP speaks
    ixp_index.install(_index(_build(_decix_tables())))
    after = traceroute_geo.location_view(traceroute_geo.geolocate_with_hostname_hint(ip))
    assert (after["city"], after["country"], after["ixp"]) == ("Frankfurt", "DE", "DE-CIX Frankfurt")
