#!/usr/bin/env python3
#
# app/services/route_map_rules.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Legibility rules of the traceroute map, shared by the web map and the PDF report.

The route chain is the primary element and the basemap recedes. This module is the ONE place that
decides line width and colors, which countries and places get a name, the label limits and the zoom
levels at which names appear. The web map receives the result as ``map_style`` / ``map_labels``
(data attributes); the PDF imports the same functions. Nothing here invents a coordinate: every
label position is a hop position or a point from the vendored Natural Earth label file.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from app.services.traceroute_geo import MAP_ROLE_ESTIMATE, META_MAP_ROLE

GEO_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor" / "geo"
BASEMAP_FILE = GEO_DIR / "world-110m.geojson"
LABELS_FILE = GEO_DIR / "labels.json"

# Chain line, in CSS pixels (the PDF scales them to points). WCAG 1.4.11 asks 3:1 for graphics.
LINE_WEIGHT_PX = 3.0
LINE_HALO_PX = 3.0  # extra width of the contrast outline under the line
DASH_ARRAY = "9 6"  # 3:2 stroke to gap, coarse enough to read; scales with the line weight
MIN_ARROW_SEGMENT_PX = 64.0  # PDF keeps arrows off short links; web uses routed detail views instead
MARKER_GAP_PX = 3.0  # the line stops this far before a marker edge
WEB_ARROW_SPACING_PX = 112.0  # repeated on long overview curves so direction remains visible

# Names: the cap counts every place label of the map; countries are limited to the path.
MAX_CITY_LABELS = 8
MAX_ORIENTATION_CITIES = 3
ORIENTATION_MAX_MIN_ZOOM = 3.7  # only the large places of the label file
ORIENTATION_SEPARATION_DEG = 2.0
ORIENTATION_NEAR_PATH_DEG = 2.5  # an orientation city must lie this close to a chain segment
PATH_CLEARANCE_DEG = 1.0  # an orientation city this close to a hop would only compete with its marker
COUNTRY_MAX_ZOOM = 5  # countries are named at overview zoom only
CITY_MIN_ZOOM = 4  # places are named once the map is zoomed in far enough to place them
FIT_MAX_ZOOM = 6  # the 1:110m outlines get coarse beyond this
MAP_MIN_ZOOM = 2
MAP_MAX_ZOOM = 8
SINGLE_POINT_ZOOM = 5

PALETTE: dict[str, dict[str, str]] = {
    "light": {
        "water": "#e4ecf4",
        "land": "#f4f6f9",
        "border": "#d3dce6",
        "route": "#0b4fc0",
        "halo": "#ffffff",
        "label": "#6b7785",
        "label_strong": "#1f2a37",
        "label_halo": "#f4f6f9",
    },
    "dark": {
        "water": "#0c1726",
        "land": "#152538",
        "border": "#233853",
        "route": "#7cc0ff",
        "halo": "#07101c",
        "label": "#7d90a8",
        "label_strong": "#d6e2f0",
        "label_halo": "#0c1726",
    },
}

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def map_style() -> dict[str, Any]:
    """Everything the web map needs to draw with the shared rules (JSON-serializable)."""
    return {
        "palette": PALETTE,
        "line": {
            "weight": LINE_WEIGHT_PX,
            "halo": LINE_HALO_PX,
            "dash": DASH_ARRAY,
            "minArrowSegment": MIN_ARROW_SEGMENT_PX,
            "markerGap": MARKER_GAP_PX,
        },
        "labels": {
            "maxCities": MAX_CITY_LABELS,
            "countryMaxZoom": COUNTRY_MAX_ZOOM,
            "cityMinZoom": CITY_MIN_ZOOM,
        },
        "view": {
            "minZoom": MAP_MIN_ZOOM,
            "maxZoom": MAP_MAX_ZOOM,
            "fitMaxZoom": FIT_MAX_ZOOM,
            "singlePointZoom": SINGLE_POINT_ZOOM,
        },
        # Browser layout rules: PDF arrow density and placement remain independent.
        "web": {
            "arrowSpacing": WEB_ARROW_SPACING_PX,
        },
    }


def _load(name: str, path: Path) -> Any:
    with _cache_lock:
        if name not in _cache:
            _cache[name] = json.loads(path.read_text(encoding="utf-8"))
        return _cache[name]


def polygon_parts(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    """Rings of every polygon of a GeoJSON Polygon/MultiPolygon geometry; other types yield none."""
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coords]
    return list(coords) if kind == "MultiPolygon" else []


def _inside(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def country_names() -> dict[str, str]:
    """ISO 3166-1 alpha-2 code -> English name, from the vendored label points (no extra data file)."""
    with _cache_lock:
        cached = _cache.get("iso_names")
    if cached is not None:
        return cached
    world = _load("world", BASEMAP_FILE)
    labels = _load("labels", LABELS_FILE)
    shapes = []
    for feature in world.get("features", []):
        iso = str((feature.get("properties") or {}).get("iso") or "").upper()
        shapes.extend(
            (iso, rings[0]) for rings in polygon_parts(feature.get("geometry") or {}) if rings and len(rings[0]) >= 3
        )
    names: dict[str, str] = {}
    for name, lat, lon, *_ in labels.get("countries", []):
        for iso, ring in shapes:
            if iso and iso not in names and _inside(lon, lat, ring):
                names[iso] = str(name)
                break
    with _cache_lock:
        _cache["iso_names"] = names
    return names


def _valid(point: dict[str, Any]) -> bool:
    lat, lon = point.get("lat"), point.get("lon")
    return all(isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon))


def _is_estimate(point: dict[str, Any]) -> bool:
    return point.get(META_MAP_ROLE) == MAP_ROLE_ESTIMATE


def chain_points(points: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The nodes of the route chain, in hop order: every located hop, estimates and destination included."""
    return [p for p in points if p and _valid(p)]


def _near_path(lat: float, lon: float, chain: Sequence[dict[str, Any]]) -> bool:
    """True when (lat, lon) is within ``ORIENTATION_NEAR_PATH_DEG`` of a chain segment (planar degrees)."""
    k = math.cos(math.radians(lat))
    px, py = lon * k, lat
    nodes = [(p["lon"] * k, p["lat"]) for p in chain]
    for (ax, ay), (bx, by) in zip(nodes, nodes[1:] or nodes, strict=False):
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
        if math.hypot(px - (ax + t * dx), py - (ay + t * dy)) <= ORIENTATION_NEAR_PATH_DEG:
            return True
    return False


def _spread(items: list[Any], count: int) -> list[Any]:
    """``count`` items spread over the list, keeping first and last."""
    if len(items) <= count:
        return items
    if count == 1:
        return [items[-1]]
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]


def select_labels(points: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Countries and places that get a name: those of the path, plus a few orientation cities.

    Countries: only those a chain hop lies in. Cities: the hop cities of the chain (an approximate
    position is not named as a place), at most ``MAX_CITY_LABELS`` including at most
    ``MAX_ORIENTATION_CITIES`` large cities close to a chain segment. A far-away city is never labelled.
    """
    chain = chain_points(points)
    names = country_names()
    label_by_name = {str(c[0]): c for c in _load("labels", LABELS_FILE).get("countries", [])}
    countries, seen_iso = [], set()
    for p in chain:
        iso = str(p.get("country") or "").upper()
        name = names.get(iso)
        if iso in seen_iso or not name:
            continue
        seen_iso.add(iso)
        _, lat, lon, *_rest = label_by_name[name]
        countries.append({"name": name, "lat": lat, "lon": lon})

    path, seen_city = [], set()
    for p in chain:
        city = str(p.get("city") or "")
        if _is_estimate(p) or not city or city in seen_city:
            continue
        seen_city.add(city)
        path.append({"name": city, "lat": p["lat"], "lon": p["lon"], "role": "path"})
    cities = _spread(path, MAX_CITY_LABELS)

    room = min(MAX_ORIENTATION_CITIES, MAX_CITY_LABELS - len(cities))
    if room > 0 and chain:
        lats, lons = [p["lat"] for p in chain], [p["lon"] for p in chain]
        pad = max(2.0, min(4.0, 0.25 * max(max(lats) - min(lats), max(lons) - min(lons))))
        box = (min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad)
        hops = list(zip(lats, lons, strict=True))
        chosen: list[dict[str, Any]] = []
        for name, lat, lon, min_zoom, *_ in _load("labels", LABELS_FILE).get("cities", []):
            if min_zoom > ORIENTATION_MAX_MIN_ZOOM or name in seen_city:
                continue
            if not (box[0] <= lat <= box[1] and box[2] <= lon <= box[3]) or not _near_path(lat, lon, chain):
                continue
            if any(max(abs(lat - a), abs(lon - b)) < PATH_CLEARANCE_DEG for a, b in hops):
                continue
            if any(max(abs(lat - c["lat"]), abs(lon - c["lon"])) < ORIENTATION_SEPARATION_DEG for c in chosen):
                continue
            chosen.append({"name": name, "lat": lat, "lon": lon, "role": "orientation"})
            if len(chosen) >= room:
                break
        cities = [*cities, *chosen]
    return {"countries": countries, "cities": cities}
