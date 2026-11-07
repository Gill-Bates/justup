#!/usr/bin/env python3
#
# tests/test_route_map_rules.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Legibility rules of the traceroute map: line contrast, label selection and limits (web and PDF share them)."""

from __future__ import annotations

import pytest

from app.services import route_map_rules as rules
from tests._support import wcag_contrast


def _p(hop, lat, lon, city, country, **extra):
    return {"hop": hop, "lat": lat, "lon": lon, "city": city, "country": country, **extra}


# Fixture that mirrors the reported screenshot: Germany/Benelux/France chain, destination, one distant estimate.
CHAIN = [
    _p(7, 50.11, 8.68, "Frankfurt", "DE", map_role="route"),
    _p(8, 50.38, 7.33, "Kottenheim", "DE", map_role="estimate", _approximate=True),
    _p(9, 52.37, 4.90, "Amsterdam", "NL", map_role="estimate", _approximate=True),
    _p(10, 50.85, 4.35, "Brussels", "BE", map_role="estimate", _approximate=True),
    _p(11, 48.86, 2.35, "Paris", "FR", map_role="estimate", _approximate=True),
    _p(12, 32.08, 34.78, "Tel Aviv", "IL", map_role="estimate", _approximate=True),
    _p(13, 51.3, 6.85, "Ratingen", "DE", map_role="route", _destination=True),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_route_line_has_graphics_contrast_against_land_and_sea(theme):
    palette = rules.PALETTE[theme]
    for ground in ("land", "water"):
        assert wcag_contrast(palette["route"], palette[ground]) >= 3.0
    assert wcag_contrast(palette["route"], palette["halo"]) >= 3.0  # the outline separates the line from borders


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_basemap_recedes_borders_and_land_stay_calm(theme):
    palette = rules.PALETTE[theme]
    assert wcag_contrast(palette["border"], palette["land"]) < 1.6  # barely visible country borders
    assert wcag_contrast(palette["land"], palette["water"]) < 1.4  # two quiet area tones


def test_line_is_thick_enough_and_dashes_are_coarse():
    assert rules.LINE_WEIGHT_PX >= 2.5
    stroke, gap = (float(v) for v in rules.DASH_ARRAY.split())
    assert stroke >= rules.LINE_WEIGHT_PX and gap >= rules.LINE_WEIGHT_PX


def test_only_path_countries_are_named_and_all_chain_nodes_count():
    names = [c["name"] for c in rules.select_labels(CHAIN)["countries"]]
    assert names == ["Germany", "Netherlands", "Belgium", "France", "Israel"]


def test_city_labels_are_capped_name_no_estimates_and_skip_far_cities():
    cities = rules.select_labels(CHAIN)["cities"]
    names = {c["name"] for c in cities}
    assert len(cities) <= rules.MAX_CITY_LABELS
    assert {"Frankfurt", "Ratingen"} <= names  # reliable hops are named
    assert {"Kottenheim", "Amsterdam", "Tel Aviv"}.isdisjoint(names)  # an approximate position is not a place
    assert {"Oslo", "Moscow", "Casablanca", "Stockholm"}.isdisjoint(names)
    assert sum(c["role"] == "orientation" for c in cities) <= rules.MAX_ORIENTATION_CITIES


def test_many_path_cities_are_thinned_to_the_cap_keeping_first_and_last():
    chain = [_p(i, 40 + i * 0.5, 5 + i, f"City{i}", "DE", map_role="route") for i in range(20)]
    cities = rules.select_labels(chain)["cities"]
    path = [c["name"] for c in cities if c["role"] == "path"]
    assert len(path) == rules.MAX_CITY_LABELS and path[0] == "City0" and path[-1] == "City19"


def test_labels_use_only_known_positions():
    for city in rules.select_labels(CHAIN)["cities"]:
        if city["role"] == "path":
            assert any((p["lat"], p["lon"]) == (city["lat"], city["lon"]) for p in CHAIN)


def test_map_style_is_json_ready_and_carries_the_zoom_rule():
    import json

    style = json.loads(json.dumps(rules.map_style()))
    assert style["labels"]["countryMaxZoom"] >= style["labels"]["cityMinZoom"] - 1  # both name kinds meet at one zoom
    assert style["view"]["fitMaxZoom"] <= style["view"]["maxZoom"]
    assert style["web"]["arrowSpacing"] == rules.WEB_ARROW_SPACING_PX
