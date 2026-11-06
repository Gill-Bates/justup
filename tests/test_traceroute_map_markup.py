#!/usr/bin/env python3
#
# tests/test_traceroute_map_markup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Route map fragment: the shared legibility rules reach the page as data attributes, the legend names every kind."""

from __future__ import annotations

import html
import json
import re

from app.api.frontend import templates
from app.services import route_map_rules as rules
from tests.test_route_map_rules import CHAIN


def _render(**extra) -> str:
    points = [dict(point, segment_to_next="dashed") for point in CHAIN]
    context = {
        "request": None,
        "hops": [{"hop": 1}],
        "geo_points": points,
        "has_interpolation": False,
        "map_features": {"has_approximate": True, "has_unknown_link": True},
        "map_style": rules.map_style(),
        "map_labels": rules.select_labels(points),
        "mapped_hop_count": len(points),
        "total_hops": 13,
        "destination_on_map": True,
        "route_start_is_mapped": True,
        "route_start_hop": 7,
        "route_destination_host": "example.org",
        "unavailable_reason": None,
        "kpis": {},
        "route_timeline": None,
        "labels": {},
        **extra,
    }
    return templates.get_template("fragments/traceroute_map.html").render(**context)


def _attribute(markup: str, name: str) -> str:
    found = re.search(rf'{name}="([^"]*)"', markup)
    assert found is not None, f"{name} missing"
    return html.unescape(found.group(1))


def test_shared_rules_are_handed_to_the_page_unchanged() -> None:
    markup = _render()
    assert json.loads(_attribute(markup, "data-map-style")) == json.loads(json.dumps(rules.map_style()))
    labels = json.loads(_attribute(markup, "data-map-labels"))
    assert {"countries", "cities"} <= labels.keys()
    assert len(labels["cities"]) <= rules.MAX_CITY_LABELS


def test_legend_names_every_node_and_link_kind() -> None:
    legend = re.sub(r"<[^>]+>", " ", _render().split('class="tr-map-legend"', 1)[1].split("</ul>", 1)[0])
    for text in (
        "Hop (reliable)",
        "Hop (approximate)",
        "Destination",
        "Connection measured",
        "Connection estimated or unknown",
    ):
        assert text in legend
    assert "tr-legend-line--dashed" in _render()  # dashed swatch differs by pattern, not only color


def test_legend_hides_kinds_the_route_does_not_have() -> None:
    markup = _render(map_features={"has_approximate": False, "has_unknown_link": False}, destination_on_map=False)
    legend = markup.split('class="tr-map-legend"', 1)[1].split("</ul>", 1)[0]
    assert "Hop (approximate)" not in legend
    assert "Connection estimated or unknown" not in legend
    assert "Destination" not in legend
