#!/usr/bin/env python3
#
# tests/test_pdf_route_map.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Offline PDF route map: shared plotted-hop decision, projection, clustering, dataset integrity."""

from __future__ import annotations

import hashlib
import io

import pytest
from reportlab.pdfgen import canvas as rl_canvas

from app.services import pdf_report
from app.services import pdf_route_map as prm
from app.services.route_timeline import logical_hop_count, mapped_hop_numbers
from tests import test_pdf_report_contracts as pdf_contracts

FRANKFURT = (50.11, 8.68)
AMSTERDAM = (52.37, 4.90)
DUESSELDORF = (51.23, 6.78)
RATINGEN = (51.30, 6.85)


def _pt(hop, loc, city, **extra):
    return {
        "hop": hop,
        "ip": f"198.51.100.{hop}",
        "lat": loc[0],
        "lon": loc[1],
        "city": city,
        "country": "DE",
        "segment_to_next": "solid",
        "_is_public": True,
        **extra,
    }


def sample_hops(n=13):
    hops = []
    for i in range(1, n + 1):
        h = {
            "hop": i,
            "ip": f"198.51.100.{i}",
            "hostname": f"r{i}.example.net",
            "status": "ok",
            "latency_ms": {"avg": 1.5 * i},
            "loss_pct": 0.0,
            "city": None,
            "country": None,
        }
        if i in (9, 12):
            h.update(ip="*", status="timeout", hostname=None, latency_ms=None)
        if i in (1, 2):
            h["ip"] = f"10.0.0.{i}"
        hops.append(h)
    hops[-1].update(ip="203.0.113.5", hostname="www.example.com")
    return hops


def sample_geo_points():
    """Sample-like route: hops 3-4 Frankfurt, 6-8 Amsterdam, 10-11 Düsseldorf, destination Ratingen."""
    return [
        _pt(3, FRANKFURT, "Frankfurt", _hop_range="3-4"),
        _pt(6, AMSTERDAM, "Amsterdam", _hop_range="6-8", segment_to_next="dashed"),
        _pt(10, DUESSELDORF, "Düsseldorf", _hop_range="10-11"),
        _pt(
            13,
            RATINGEN,
            "Ratingen",
            _destination=True,
            _is_terminal=True,
            _approximate=True,
            accuracy_radius_km=25.0,
            segment_to_next=None,
        ),
    ]


@pytest.fixture(scope="module")
def model():
    return prm.build_route_map_model(sample_geo_points(), sample_hops())


# ─── shared decision with the web map ────────────────────────────────────────────


def test_plotted_hops_destination_and_counts_match_web_map(model):
    geo = sample_geo_points()
    hops = sample_hops()
    # The web fragment derives these four values from the same geo_points with the same helpers.
    assert model.plotted_hops == len(mapped_hop_numbers(geo)) == 8
    assert model.total_hops == logical_hop_count(hops) == 13
    web_destination = next(p for p in reversed(geo) if p.get("_destination"))
    assert model.destination == web_destination
    assert (model.destination["lat"], model.destination["lon"]) == RATINGEN
    assert model.subtitle == "Geolocated route for 8 of 13 hops"


def test_subtitle_says_when_destination_is_not_on_map():
    geo = [p for p in sample_geo_points() if not p.get("_destination")]
    m = prm.build_route_map_model(geo, sample_hops())
    assert m.destination is None
    assert m.subtitle.endswith("· Destination not on map")


def test_unlocated_hops_are_never_plotted():
    geo = [
        *sample_geo_points(),
        {"hop": 5, "ip": "10.0.0.5", "lat": None, "lon": None},
        {"hop": 2, "lat": float("nan"), "lon": 1.0},
        {"hop": 4, "lat": 99, "lon": 1.0},
        {"hop": 9, "lat": 50.0, "lon": 8.0, "_interpolated": True},
        {"hop": 1, "lat": 1.0, "lon": 1.0, "_is_public": False},
    ]
    assert [p["hop"] for p in prm.plotted_points(geo)] == [3, 6, 10, 13]


def test_no_plotted_hop_gives_no_model():
    assert prm.build_route_map_model([], sample_hops()) is None
    assert prm.build_route_map_model([{"hop": 1, "lat": None, "lon": None}], sample_hops()) is None


# ─── projection and fit ──────────────────────────────────────────────────────────


def test_single_point_gets_minimum_extent():
    view = prm.fit_view([FRANKFURT], 400, 200, 40, 30)
    _, lat0, _, lat1 = view.bounds()
    assert lat1 - lat0 >= prm.MIN_EXTENT_DEG
    assert view.project(*FRANKFURT) == pytest.approx((200, 100))


def test_two_points_fit_inside_margins():
    view = prm.fit_view([AMSTERDAM, FRANKFURT], 400, 200, 40, 30)
    for loc in (AMSTERDAM, FRANKFURT):
        x, y = view.project(*loc)
        assert 40 - 1e-6 <= x <= 360 + 1e-6
        assert 30 - 1e-6 <= y <= 170 + 1e-6


def test_continental_route_still_fits():
    view = prm.fit_view([(52.4, 4.9), (-33.9, 151.2)], 400, 200, 40, 30)
    for loc in ((52.4, 4.9), (-33.9, 151.2)):
        x, y = view.project(*loc)
        assert 0 < x < 400 and 0 < y < 200


# ─── clustering and labels ───────────────────────────────────────────────────────


def _groups(points, start_hop=None, start_mapped=False, size=(400, 200)):
    view = prm.fit_view([(p["lat"], p["lon"]) for p in points], *size, 40, 30)
    return prm.build_marker_groups(points, view, start_hop, start_mapped)


def test_labels_ranges_and_destination_marker(model):
    labels = [(g.kind, g.label) for g in _groups(list(model.points), size=(480, 220))]
    assert labels[0] == ("hop", "3–4")
    assert labels[1] == ("hop", "6–8")
    assert labels[-1][0] == "destination" and labels[-1][1].endswith("D")


def test_start_marker_only_when_first_hop_is_plotted():
    pts = sample_geo_points()
    assert all(g.kind != "start" for g in _groups(pts, start_hop=1, start_mapped=False))
    started = _groups(pts, start_hop=3, start_mapped=True)
    assert started[0].kind == "start" and started[0].label == "S"


def test_destination_never_replaces_last_hop_label():
    pts = [_pt(1, FRANKFURT, "Frankfurt"), _pt(2, AMSTERDAM, "Amsterdam")]
    assert [g.kind for g in _groups(pts)] == ["hop", "hop"]


def test_only_consecutive_visits_share_a_marker():
    a, b = _pt(1, FRANKFURT, "Frankfurt"), _pt(2, AMSTERDAM, "Amsterdam")
    c = _pt(3, FRANKFURT, "Frankfurt")
    groups = _groups([a, b, c])
    assert [g.members for g in groups] == [(0,), (1,), (2,)]


def test_close_points_share_label_and_overlapping_groups_fan_out():
    near = [_pt(1, FRANKFURT, "A"), _pt(2, (FRANKFURT[0] + 0.001, FRANKFURT[1]), "B")]
    far = _pt(3, (FRANKFURT[0] + 0.01, FRANKFURT[1]), "C")
    groups = _groups([*near, far, _pt(4, AMSTERDAM, "D")])
    assert groups[0].label == "1·2·3"  # all consecutive and within the cluster radius
    assert groups[0].display == pytest.approx(groups[0].center)


def test_chain_joins_every_plotted_point_and_reads_the_servers_dash_flag():
    pts = [
        _pt(7, FRANKFURT, "Frankfurt", segment_to_next="dashed"),
        _pt(8, (50.38, 7.33), "Kottenheim", map_role="estimate", segment_to_next="dashed"),
        _pt(10, FRANKFURT, "Frankfurt"),
        _pt(11, DUESSELDORF, "Wuppertal", segment_to_next="dashed"),
        _pt(13, RATINGEN, "Ratingen", _destination=True, _approximate=True),
    ]
    assert prm.route_segments(pts) == [(0, 1, True), (1, 2, True), (2, 3, False), (3, 4, True)]


def test_estimate_gets_its_own_marker_with_a_tilde_label():
    est = _pt(8, (50.38, 7.33), "Kottenheim", map_role="estimate", _approximate=True)
    groups = _groups([_pt(7, FRANKFURT, "Frankfurt"), est, _pt(10, FRANKFURT, "Frankfurt")])
    assert [(g.kind, g.label, g.members) for g in groups] == [
        ("hop", "7", (0,)),
        ("estimate", "~8", (1,)),
        ("hop", "10", (2,)),
    ]
    assert groups[1].approximate


def test_accuracy_radius_is_the_one_the_server_allows_to_draw():
    assert prm.accuracy_radius_km({"map_radius_km": 25}) == 25.0
    assert prm.accuracy_radius_km({"accuracy_radius_km": 25}) is None  # no server decision, nothing drawn
    assert prm.accuracy_radius_km({"map_radius_km": None}) is None


# ─── dataset ─────────────────────────────────────────────────────────────────────

WORLD_SHA256 = "4f4dde9bff703123a31ce5095127f8f97021d72052999509a484b7ef9bdcf70d"
LABELS_SHA256 = "18262862bf527052bbf93820b2c4125967d55ce7f7298fc790f4c3e46bee1dda"


def test_dataset_integrity_and_size_cap():
    for path, digest, cap in ((prm.BASEMAP_FILE, WORLD_SHA256, 300_000), (prm.LABELS_FILE, LABELS_SHA256, 100_000)):
        data = path.read_bytes()
        assert len(data) <= cap
        assert hashlib.sha256(data).hexdigest() == digest, f"{path.name} changed: update PROVENANCE.md and the pin"
    assert (prm.GEO_DIR / "PROVENANCE.md").is_file()


def test_basemap_loads_once_with_sane_bounds():
    base = prm.load_basemap()
    assert prm.load_basemap() is base
    assert len(base.polygons) > 150
    assert all(-180 <= b[0] <= b[2] <= 180 and -90 <= b[1] <= b[3] <= 90 for b, _ in base.polygons)


# ─── geometry and drawing ────────────────────────────────────────────────────────


def test_clip_ring_stays_inside_rect():
    ring = [(-50, -50), (150, -50), (150, 150), (-50, 150)]
    clipped = prm.clip_ring(ring, (0, 0, 100, 100))
    assert clipped and all(0 <= x <= 100 and 0 <= y <= 100 for x, y in clipped)


def test_land_rings_are_clipped_to_frame(model):
    view = prm.fit_view([(p["lat"], p["lon"]) for p in model.points], 480, 220, 40, 30)
    rings = prm.land_rings(prm.load_basemap(), view)
    assert rings
    assert all(-2.001 <= x <= 482.001 and -2.001 <= y <= 222.001 for r in rings for ring in r for x, y in ring)


def _draw(model, width=480, height=280):
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(600, 400), pageCompression=0, invariant=1)
    flow = prm.RouteMap(model, "Helvetica", "Helvetica-Bold", width, height)
    flow.wrapOn(c, width, height)
    flow.drawOn(c, 20, 20)
    c.save()
    return buf.getvalue()


def test_builtin_font_glyph_check_uses_encoding_vector(model):
    route_map = prm.RouteMap(model, "Helvetica", "Helvetica-Bold", 480)

    assert route_map._has_glyphs("Düsseldorf")
    assert not route_map._has_glyphs("東京")


def _spy_drawn_text(monkeypatch) -> list[str]:
    """Record every string drawn on a reportlab canvas while still drawing it."""
    drawn: list[str] = []
    for name in ("drawString", "drawCentredString", "drawRightString"):
        original = getattr(rl_canvas.Canvas, name)

        def spy(self, x, y, text, *a, _orig=original, **k):
            drawn.append(str(text))
            return _orig(self, x, y, text, *a, **k)

        monkeypatch.setattr(rl_canvas.Canvas, name, spy)
    return drawn


def test_drawing_is_deterministic_and_compact(model):
    prm.load_basemap()
    first = _draw(model)
    assert first == _draw(model)
    assert len(first) < 300_000


def test_single_point_and_same_place_routes_draw():
    single = prm.build_route_map_model([_pt(3, FRANKFURT, "Frankfurt")], sample_hops())
    assert single.plotted_hops == 1
    assert _draw(single)
    same = prm.build_route_map_model(
        [_pt(3, FRANKFURT, "Frankfurt", _hop_range="3-6"), _pt(7, FRANKFURT, "Frankfurt", _destination=True)],
        sample_hops(),
    )
    assert _draw(same)


def test_intercontinental_route_draws():
    pts = [
        _pt(2, AMSTERDAM, "Amsterdam"),
        _pt(5, (40.7, -74.0), "New York"),
        _pt(9, (35.7, 139.7), "Tokyo", _destination=True),
    ]
    assert _draw(prm.build_route_map_model(pts, sample_hops(10)))


# ─── PDF level ───────────────────────────────────────────────────────────────────


def _build_with_map(monkeypatch, geo, hops, *, size="a4"):
    drawn = _spy_drawn_text(monkeypatch)
    b = pdf_contracts._full_builder(monkeypatch, size=size, hops=hops)
    monkeypatch.setattr(b, "_get_route_data", lambda **_k: (hops, geo, True))
    b.build()
    return b, " | ".join(drawn)


def test_pdf_contains_route_map_section(monkeypatch):
    b, text = _build_with_map(monkeypatch, sample_geo_points(), sample_hops())
    for expected in (
        "Route map",
        "Geolocated route for 8 of 13 hops",
        "Hop",
        "Destination",
        "Link estimated or unknown",
        "Link measured",
        "Approximate (~)",
        pdf_route_attribution(),
        "Frankfurt",
        "Ratingen",
    ):
        assert expected in text
    assert b.page_count >= 4


def test_pdf_without_plotted_hops_says_so(monkeypatch):
    b = pdf_contracts._full_builder(monkeypatch, hops=sample_hops())
    b.elements = []
    b._add_route_map(sample_hops(), [])
    assert prm.NO_MAP_NOTE in pdf_contracts._texts(b)


def test_pdf_letter_size_builds(monkeypatch):
    _, text = _build_with_map(monkeypatch, sample_geo_points(), sample_hops(), size="letter")
    assert "Route map" in text


def pdf_route_attribution():
    return prm.ATTRIBUTION


def test_report_module_uses_shared_helpers():
    assert pdf_report.build_route_map_model is prm.build_route_map_model


def test_pdf_and_web_read_the_same_segments_from_the_servers_points(monkeypatch):
    """Backend points of the Frankfurt/Kottenheim/Ratingen route give the same line in the PDF reader."""
    from app.services import traceroute_geo as tg
    from tests.test_traceroute_route_model import _owner_route

    _hops_in, _dest, points = _owner_route(monkeypatch)
    model = prm.build_route_map_model(points, sample_hops())
    segments = [
        (model.points[a]["hop"], model.points[b]["hop"], dashed) for a, b, dashed in prm.route_segments(model.points)
    ]
    # Same sequence the web map draws (tests/js/traceroute-map-groups.test.mjs pins it for the JS reader).
    assert segments == [(7, 8, True), (8, 10, True), (10, 11, False), (11, 13, True)]
    assert model.has_unknown_link and model.has_approximate
    assert tg.route_map_features(points) == {"has_unknown_link": True, "has_approximate": True}
    assert _draw(model)


def test_pdf_names_only_path_countries_and_a_capped_set_of_places_from_the_shared_rule(monkeypatch):
    """The labels the PDF draws come from route_map_rules.select_labels (the web map reads the same list)."""
    from app.services import route_map_rules as rules

    pts = [
        _pt(3, FRANKFURT, "Frankfurt"),
        _pt(5, AMSTERDAM, "Amsterdam", country="NL"),
        _pt(7, (48.86, 2.35), "Paris", country="FR"),
        _pt(9, (32.08, 34.78), "Tel Aviv", country="IL", map_role="estimate", _approximate=True),
        _pt(13, RATINGEN, "Ratingen", _destination=True, _is_terminal=True, segment_to_next=None),
    ]
    model_ = prm.build_route_map_model(pts, sample_hops())
    selected = rules.select_labels(model_.points)
    drawn = _spy_drawn_text(monkeypatch)
    _draw(model_, width=480, height=280)
    allowed_countries = {c["name"] for c in selected["countries"]}
    allowed_cities = {c["name"] for c in selected["cities"]}
    country_names = {c[0] for c in prm.load_basemap().countries}
    assert {d for d in drawn if d in country_names} <= allowed_countries
    assert allowed_countries == {"Germany", "Netherlands", "France", "Israel"}
    assert len(allowed_cities) <= rules.MAX_CITY_LABELS
    city_names = {c[0] for c in rules._load("labels", rules.LABELS_FILE)["cities"]}
    assert {d for d in drawn if d in city_names} <= allowed_cities
    assert {"Oslo", "Moscow", "Casablanca"}.isdisjoint(drawn)
