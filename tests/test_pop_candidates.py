#!/usr/bin/env python3
#
# tests/test_pop_candidates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Unknown PoP observation and explicit-review safety contracts."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from app.db import pop_candidates, pop_lookup
from app.db import sqlite as sqlite_db
from app.services import pop_codes, traceroute_geo
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    sqlite_db.init_schema(db)
    db.row_factory = sqlite3.Row
    return db


def _key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _observe(conn: sqlite3.Connection, ip: str, measurement: str, *, country: str = "DE", lat: float = 50.1) -> bool:
    return pop_candidates.observe(
        conn,
        code="xyz",
        hostname="core.xyz.example.net",
        public_ip=ip,
        provider="generic",
        measurement_key=_key(measurement),
        proposal={"city": "Frankfurt", "country": country, "lat": lat, "lon": 8.6},
    )


def test_unconfirmed_candidate_is_inert_and_repeated_measurement_is_deduplicated(conn) -> None:
    assert _observe(conn, "8.8.8.8", "route-a")
    assert not _observe(conn, "8.8.8.8", "route-a")
    assert _observe(conn, "8.8.8.9", "route-b")
    assert pop_candidates.observe(
        conn,
        code="xyz",
        hostname="core.xyz.example.net",
        public_ip="8.8.8.10",
        provider="other",
        measurement_key=_key("route-b2"),
        proposal={"city": "Frankfurt", "country": "DE", "lat": 50.1, "lon": 8.6},
    )
    row = conn.execute("SELECT seen_count, independent_count, quality_score FROM pop_alias_candidates").fetchone()
    assert tuple(row) == (3, 1, 40)
    assert pop_lookup.lkp_t(conn, ["xyz"]) is None
    with pytest.raises(ValueError, match="two independent"):
        pop_candidates.review(
            conn,
            "xyz",
            action="approve",
            actor="reviewer",
            evidence="manual check",
            city="Frankfurt",
            country="DE",
            lat=50.1,
            lon=8.6,
        )
    assert _observe(conn, "1.1.1.1", "route-c")
    row = conn.execute("SELECT seen_count, independent_count, quality_score FROM pop_alias_candidates").fetchone()
    assert tuple(row) == (4, 2, 70)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(True, 8.6), (50.1, False), ("50.1", 8.6), (float("nan"), 8.6), (50.1, float("inf")), (90.5, 8.6), (50.1, -181)],
)
def test_invalid_coordinates_are_dropped_on_observe_and_refused_on_review(conn, lat, lon) -> None:
    assert pop_candidates.observe(
        conn,
        code="bad",
        hostname="core.bad.example.net",
        public_ip="8.8.4.4",
        provider="generic",
        measurement_key=_key("route-invalid"),
        proposal={"city": "Frankfurt", "country": "DE", "lat": lat, "lon": lon},
    )
    row = conn.execute(
        "SELECT proposal_lat, proposal_lon, proposal_source FROM pop_alias_candidates WHERE alias_code = 'bad'"
    ).fetchone()
    assert tuple(row) == (None, None, None)
    _observe(conn, "8.8.8.8", "route-a")
    _observe(conn, "1.1.1.1", "route-b")
    with pytest.raises(ValueError, match="observed country or coordinates"):
        pop_candidates.review(
            conn, "xyz", action="approve", actor="a", evidence="e", city="Frankfurt", country="DE", lat=lat, lon=lon
        )


def test_approval_revocation_and_review_audit(conn) -> None:
    _observe(conn, "8.8.8.8", "route-a")
    _observe(conn, "1.1.1.1", "route-b")
    pop_candidates.review(
        conn,
        "xyz",
        action="approve",
        actor="alice",
        evidence="ticket 123 and route evidence",
        city="Frankfurt",
        country="DE",
        lat=50.1,
        lon=8.6,
    )
    result = pop_lookup.lkp_t(conn, ["xyz"])
    assert result is not None and result["source"] == "learned"
    assert result["confidence"] == "medium"
    assert tuple(conn.execute("SELECT confidence, priority FROM pop_locations WHERE alias_code='xyz'").fetchone()) == (
        70,
        10,
    )
    pop_candidates.review(conn, "xyz", action="revoke", actor="bob", evidence="contradicting route")
    assert pop_lookup.lkp_t(conn, ["xyz"]) is None
    assert [
        (r[0], r[1], r[2])
        for r in conn.execute("SELECT action, actor, evidence FROM pop_candidate_reviews ORDER BY id")
    ] == [
        ("approve", "alice", "ticket 123 and route evidence"),
        ("revoke", "bob", "contradicting route"),
    ]


def test_explicit_curated_canonical_mapping_requires_matching_anchor(conn) -> None:
    for ip, measurement in (("8.8.8.8", "a"), ("1.1.1.1", "b")):
        pop_candidates.observe(
            conn,
            code="xyz",
            hostname="xyz.example.net",
            public_ip=ip,
            provider="generic",
            measurement_key=_key(measurement),
            proposal={"city": "Frankfurt", "country": "DE", "lat": 50.1, "lon": 8.6, "canonical_guess": "fra"},
        )
    with pytest.raises(ValueError, match="curated anchor"):
        pop_candidates.review(
            conn,
            "xyz",
            action="approve",
            actor="alice",
            evidence="route review",
            canonical="fra",
            city="Frankfurt",
            country="DE",
            lat=50.1,
            lon=8.6,
        )
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence, "
        "priority, created_at, updated_at) VALUES ('fra','fra','Frankfurt','DE',50.1,8.6,'seed',100,0,'x','x')"
    )
    conn.commit()
    pop_candidates.review(
        conn,
        "xyz",
        action="approve",
        actor="alice",
        evidence="route review",
        canonical="fra",
        city="Frankfurt",
        country="DE",
        lat=50.1,
        lon=8.6,
    )
    assert pop_lookup.lkp_t(conn, ["xyz"])["canonical_code"] == "fra"
    assert conn.execute("SELECT confidence FROM pop_locations WHERE alias_code='fra'").fetchone()[0] == 100


def test_conflicts_and_curated_alias_block_approval(conn) -> None:
    _observe(conn, "8.8.8.8", "route-a")
    _observe(conn, "1.1.1.1", "route-b", country="FR", lat=48.8)
    assert tuple(conn.execute("SELECT status, conflict_count FROM pop_alias_candidates").fetchone()) == ("conflict", 1)
    with pytest.raises(ValueError, match="non-conflicting"):
        pop_candidates.review(
            conn,
            "xyz",
            action="approve",
            actor="alice",
            evidence="review",
            city="Frankfurt",
            country="DE",
            lat=50.1,
            lon=8.6,
        )
    pop_candidates.review(conn, "xyz", action="reject", actor="alice", evidence="country conflict")
    assert conn.execute("SELECT status FROM pop_alias_candidates").fetchone()[0] == "rejected"

    for ip, route in (("8.8.4.4", "c"), ("1.0.0.1", "d")):
        pop_candidates.observe(
            conn,
            code="cur",
            hostname="cur.example.net",
            public_ip=ip,
            provider="generic",
            measurement_key=_key(route),
            proposal={"city": "Paris", "country": "FR", "lat": 48.85, "lon": 2.35},
        )
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence, priority, "
        "created_at, updated_at) VALUES ('cur','cur','Paris','FR',48.85,2.35,'seed',100,0,'x','x')"
    )
    conn.commit()
    with pytest.raises(ValueError, match="already owns"):
        pop_candidates.review(
            conn,
            "cur",
            action="approve",
            actor="alice",
            evidence="review",
            city="Paris",
            country="FR",
            lat=48.85,
            lon=2.35,
        )
    assert tuple(conn.execute("SELECT source, confidence FROM pop_locations WHERE alias_code='cur'").fetchone()) == (
        "seed",
        100,
    )


def test_distance_conflict_and_input_limits(conn, monkeypatch) -> None:
    assert not _observe(conn, "10.0.0.1", "private")
    assert not pop_candidates.observe(
        conn,
        code="xyz",
        hostname="x" * 254,
        public_ip="8.8.8.8",
        provider="generic",
        measurement_key=_key("bad-host"),
    )
    _observe(conn, "8.8.8.8", "a")
    _observe(conn, "1.1.1.1", "b", lat=55.0)
    assert conn.execute("SELECT status FROM pop_alias_candidates").fetchone()[0] == "conflict"
    monkeypatch.setattr(pop_candidates, "MAX_CANDIDATES", 1)
    assert not pop_candidates.observe(
        conn,
        code="new",
        hostname="new.example.net",
        public_ip="8.8.4.4",
        provider="generic",
        measurement_key=_key("new"),
    )


def test_route_capture_is_inert_and_candidate_failures_do_not_break_monitoring(tmp_path, monkeypatch) -> None:
    path = tmp_path / "justup.db"
    db = sqlite_db.connect(path)
    sqlite_db.init_schema(db)
    sqlite_db.close_connection(db)
    monkeypatch.setattr(pop_codes, "_pop_get_conn", lambda: sqlite_db.connect(path))
    monkeypatch.setattr(traceroute_geo, "geolocate_with_hostname_hint", lambda *_args: None)
    monkeypatch.setattr(
        traceroute_geo,
        "geolocate_ip",
        lambda _ip: {
            "city": "Frankfurt",
            "country": "DE",
            "lat": 50.1,
            "lon": 8.6,
        },
    )
    hop = [{"hop": 1, "ip": "8.8.8.8", "hostname": "core.xyz.example.net", "status": "ok"}]
    assert traceroute_geo.route_hops_to_geo_points(hop) == []
    assert traceroute_geo.route_hops_to_geo_points(hop) == []
    db = sqlite_db.connect(path)
    assert db.execute("SELECT seen_count FROM pop_alias_candidates WHERE alias_code='xyz'").fetchone()[0] == 1
    assert tuple(
        db.execute(
            "SELECT canonical_guess, proposal_source FROM pop_alias_candidates WHERE alias_code='xyz'"
        ).fetchone()
    ) == ("fra", "geolite2-city+curated-code")
    assert pop_lookup.lkp_t(db, ["xyz"]) is None
    sqlite_db.close_connection(db)
    monkeypatch.setattr(
        traceroute_geo, "observe_unknown_pop", lambda *_args: (_ for _ in ()).throw(sqlite3.Error("disk"))
    )
    assert traceroute_geo.route_hops_to_geo_points(hop) == []


def test_import_cannot_overwrite_approved_learned_row_but_may_after_revocation(conn) -> None:
    from app.db import pop_locations

    _observe(conn, "8.8.8.8", "route-a")
    _observe(conn, "1.1.1.1", "route-b")
    pop_candidates.review(
        conn, "xyz", action="approve", actor="alice", evidence="route evidence", city="Frankfurt", country="DE",
        lat=50.1, lon=8.6,
    )  # fmt: skip

    def _import(lat: float) -> bool:
        return pop_locations.upsert_pop_location(
            conn, canonical_code="xyz", alias_code="xyz", city="Elsewhere", country="DE", lat=lat, lon=8.6,
            source=pop_locations.SOURCE_PEERINGDB, confidence=100, priority=0, import_run_id="run-1",
        )  # fmt: skip

    assert _import(40.0) is False
    row = conn.execute("SELECT source, lat, city, retired FROM pop_locations WHERE alias_code='xyz'").fetchone()
    assert (row[0], row[1], row[2], row[3]) == ("learned", 50.1, "Frankfurt", 0)

    conn.commit()  # review() opens its own immediate transaction
    pop_candidates.review(conn, "xyz", action="revoke", actor="bob", evidence="contradicting route")
    assert _import(40.0) is True
    row = conn.execute("SELECT source, lat, retired FROM pop_locations WHERE alias_code='xyz'").fetchone()
    assert (row[0], row[1], row[2]) == ("peeringdb", 40.0, 0)


# --- Shared coordinate / number / anchor rules ----------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon", "ok"),
    [
        (50.0, 8.0, True),
        (50, 8, True),
        (90, 180, True),
        (-90, -180, True),
        (90.1, 0, False),
        (0, 180.1, False),
        (True, 8.0, False),
        (50.0, False, False),
        (None, 8.0, False),
        (float("nan"), 8.0, False),
        (float("inf"), 8.0, False),
        (50.0, float("-inf"), False),
    ],
)
def test_valid_coordinates_and_has_coords_agree(lat, lon, ok):
    from app.db.pop_locations import valid_coordinates

    assert valid_coordinates(lat, lon) is ok
    assert traceroute_geo._has_coords({"lat": lat, "lon": lon}) is ok  # noqa: SLF001


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (1, True),
        (0, True),
        (-3.5, True),
        (50.0, True),
        (True, False),
        (False, False),
        (None, False),
        ("1", False),
        (float("nan"), False),
        (float("inf"), False),
        (float("-inf"), False),
    ],
)
def test_finite_number(value, ok):
    assert traceroute_geo._finite_number(value) is ok  # noqa: SLF001


@pytest.mark.parametrize(("offset_deg", "approved"), [(0.44, True), (0.46, False)], ids=["49km", "51km"])
def test_canonical_location_must_lie_within_the_curated_anchor_distance(conn, offset_deg, approved) -> None:
    lat = 48.85 + offset_deg  # 1 degree of latitude is ~111.2 km: 0.44 -> ~48.9 km, 0.46 -> ~51.2 km
    for ip, route in (("8.8.4.4", "c"), ("1.0.0.1", "d")):
        pop_candidates.observe(
            conn,
            code="xyz",
            hostname="xyz.example.net",
            public_ip=ip,
            provider="generic",
            measurement_key=_key(route),
            proposal={"city": "Paris", "country": "FR", "lat": lat, "lon": 2.35, "canonical_guess": "cur"},
        )
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence, priority, "
        "created_at, updated_at) VALUES ('cur','cur','Paris','FR',48.85,2.35,'seed',100,0,'x','x')"
    )
    conn.commit()
    kwargs = {
        "action": "approve", "actor": "alice", "evidence": "review", "canonical": "cur",
        "city": "Paris", "country": "FR", "lat": lat, "lon": 2.35,
    }  # fmt: skip
    if approved:
        pop_candidates.review(conn, "xyz", **kwargs)
        assert conn.execute("SELECT canonical_code FROM pop_locations WHERE alias_code='xyz'").fetchone()[0] == "cur"
    else:
        with pytest.raises(ValueError, match="Reviewed canonical location lacks a nearby curated anchor"):
            pop_candidates.review(conn, "xyz", **kwargs)


# --- Backend: the configured sender decides the profile account ----------------------------------


def _profile_backend(accounts: list[str]):
    import threading
    from types import SimpleNamespace

    from app.services.notifications.signal_cli_backend import SignalCliBackend

    backend = object.__new__(SignalCliBackend)
    backend._state_lock = threading.RLock()
    backend._sender = None  # production default
    backend.list_accounts = lambda: accounts  # type: ignore[method-assign]
    backend.rpc_calls = []
    backend._run_signal_rpc = lambda method, params, *_a, **_k: (
        backend.rpc_calls.append((method, params)) or SimpleNamespace()
    )
    backend._require_rpc_success = lambda *_a, **_k: None
    return backend


def test_update_profile_uses_the_passed_configured_sender() -> None:
    backend = _profile_backend(["+491", "+492"])
    backend.update_profile("justUp", avatar_path="", sender="+492")
    assert backend.rpc_calls[0][1]["account"] == "+492"


def test_update_profile_rejects_an_unregistered_configured_sender() -> None:
    from app.services.notifications.signal_errors import SignalNotRegistered

    backend = _profile_backend(["+491"])
    with pytest.raises(SignalNotRegistered):
        backend.update_profile("justUp", avatar_path="", sender="+499")
    assert backend.rpc_calls == []
