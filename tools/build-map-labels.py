#!/usr/bin/env python3
#
# tools/build-map-labels.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Build app/static/vendor/geo/labels.json (country and city labels for the traceroute map).

Source: Natural Earth (public domain), downloaded once at build time:
  ne_110m_admin_0_countries and ne_50m_populated_places_simple.
The result is committed; the app never fetches map data at runtime.

Format (compact arrays):
  countries: [name, lat, lon, minZoom]
  cities:    [name, lat, lon, minZoom, isCapital]
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
OUT = Path(__file__).resolve().parent.parent / "app/static/vendor/geo/labels.json"
MAX_CITY_ZOOM = 6.1


def fetch(name: str) -> dict:
    with urllib.request.urlopen(BASE + name, timeout=60) as resp:  # noqa: S310 - fixed https URL
        return json.load(resp)


def main() -> None:
    countries = []
    for f in fetch("ne_110m_admin_0_countries.geojson")["features"]:
        p = f["properties"]
        if p.get("LABEL_X") is None or p.get("LABEL_Y") is None:
            continue
        countries.append([p["NAME_EN"] or p["NAME"], round(p["LABEL_Y"], 3), round(p["LABEL_X"], 3), p["MIN_LABEL"]])
    countries.sort(key=lambda c: (c[3], c[0]))

    cities = []
    for f in fetch("ne_50m_populated_places_simple.geojson")["features"]:
        p = f["properties"]
        if p["min_zoom"] > MAX_CITY_ZOOM:
            continue
        cities.append(
            [
                p["name"],
                round(p["latitude"], 3),
                round(p["longitude"], 3),
                p["min_zoom"],
                int(bool(p["adm0cap"])),
                p["pop_max"],
            ]
        )
    cities.sort(key=lambda c: (c[3], -c[5]))
    for c in cities:
        c.pop()  # population only used for ordering

    OUT.write_text(
        json.dumps({"countries": countries, "cities": cities}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"{OUT}: {len(countries)} countries, {len(cities)} cities, {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
