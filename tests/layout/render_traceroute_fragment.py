#!/usr/bin/env python3
#
# tests/layout/render_traceroute_fragment.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Render the real traceroute map fragment for the browser spec (stdout = HTML, stdin = geo points JSON)."""

from __future__ import annotations

import json
import sys

from app.api.frontend import templates
from app.services import route_map_rules as rules


def main() -> None:
    payload = json.load(sys.stdin)
    # A bare list is the geo points; an object may add hop rows and the KPI row.
    points = payload["points"] if isinstance(payload, dict) else payload
    extra = payload if isinstance(payload, dict) else {}
    html = templates.get_template("fragments/traceroute_map.html").render(
        request=None,
        hops=extra.get("hops", [{"hop": 1}]),
        geo_points=points,
        has_interpolation=False,
        map_features={"has_approximate": True, "has_unknown_link": True},
        map_style=rules.map_style(),
        map_labels=rules.select_labels(points),
        mapped_hop_count=len(points),
        total_hops=13,
        destination_on_map=True,
        route_start_is_mapped=True,
        route_start_hop=points[0]["hop"],
        route_destination_host="example.org",
        unavailable_reason=None,
        kpis=extra.get("kpis", {}),
        route_timeline=None,
        labels={},
    )
    sys.stdout.write(html)


if __name__ == "__main__":
    main()
