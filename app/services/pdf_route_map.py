#!/usr/bin/env python3
#
# app/services/pdf_route_map.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Offline geographic route map for the PDF report.

The map draws vector geometry only: the Natural Earth country outlines the web map already ships
(app/static/vendor/geo, public domain) and the SAME ``geo_points`` the web map plots, so both views
agree on the plotted hops, the clusters and the destination. No tile server, no network access.

Projection: equirectangular, longitudes scaled by cos(latitude of the frame centre). One scale
applies to the whole frame, so distances are true only near that latitude (the scale bar says so).
The antimeridian is not handled: a route that crosses it is drawn the long way round.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import FILL_EVEN_ODD
from reportlab.platypus import Flowable

from app.services.route_timeline import logical_hop_count, mapped_hop_numbers, route_start_is_mapped

GEO_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor" / "geo"
BASEMAP_FILE = GEO_DIR / "world-110m.geojson"
LABELS_FILE = GEO_DIR / "labels.json"

MIN_EXTENT_DEG = 3.0  # smallest visible span, so a single region stays recognisable
KM_PER_DEG_LAT = 111.19
MAX_RADIUS_KM = 250.0  # larger accuracy radii are not drawn: they would cover the map
GROUP_RADIUS = 7.5 * mm  # consecutive hops closer than this share one marker
FONT_SIZE = 8.0
ATTRIBUTION = "Basemap: Natural Earth (public domain)"
NO_MAP_NOTE = "No hop could be placed on a map."

_INK = colors.HexColor("#12294a")
_START = colors.HexColor("#146c43")
_DEST = colors.HexColor("#a14d00")
_LAND = colors.HexColor("#eceff3")
_LAND_LINE = colors.HexColor("#aab3c0")
_SEA = colors.HexColor("#f8fafc")
_MUTED = colors.HexColor("#5c6670")

Rect = tuple[float, float, float, float]


# ─── Basemap data ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Basemap:
    """Country polygons as (bbox, rings of (lon, lat)) plus label points."""

    polygons: tuple[tuple[tuple[float, float, float, float], tuple[tuple[tuple[float, float], ...], ...]], ...]
    countries: tuple[tuple[str, float, float, float], ...]  # name, lat, lon, minZoom


_basemap: Basemap | None = None
_basemap_lock = threading.Lock()


def _polygon_parts(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    kind, coords = geometry.get("type"), geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coords]
    if kind == "MultiPolygon":
        return list(coords)
    return []


def _parse_basemap(world: dict[str, Any], labels: dict[str, Any]) -> Basemap:
    polygons = []
    for feature in world.get("features", []):
        for rings in _polygon_parts(feature.get("geometry") or {}):
            parsed = tuple(tuple((float(p[0]), float(p[1])) for p in ring) for ring in rings if len(ring) >= 3)
            if not parsed:
                continue
            xs = [p[0] for p in parsed[0]]
            ys = [p[1] for p in parsed[0]]
            polygons.append(((min(xs), min(ys), max(xs), max(ys)), parsed))
    countries = tuple((str(c[0]), float(c[1]), float(c[2]), float(c[3])) for c in labels.get("countries", []))
    return Basemap(tuple(polygons), countries)


def load_basemap() -> Basemap:
    """Parse the shipped dataset once per process (thread-safe, lazy)."""
    global _basemap
    if _basemap is None:
        with _basemap_lock:
            if _basemap is None:
                world = json.loads(BASEMAP_FILE.read_text(encoding="utf-8"))
                labels = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
                _basemap = _parse_basemap(world, labels)
    return _basemap


# ─── Projection ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class View:
    """Equirectangular view: points per degree of latitude, centred on (lat_c, lon_c)."""

    lat_c: float
    lon_c: float
    scale: float
    width: float
    height: float

    @property
    def k(self) -> float:
        return math.cos(math.radians(max(-80.0, min(80.0, self.lat_c))))

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        return (
            self.width / 2 + (lon - self.lon_c) * self.k * self.scale,
            self.height / 2 + (lat - self.lat_c) * self.scale,
        )

    def bounds(self) -> tuple[float, float, float, float]:
        """Visible (lon_min, lat_min, lon_max, lat_max)."""
        half_lon = self.width / 2 / (self.k * self.scale)
        half_lat = self.height / 2 / self.scale
        return self.lon_c - half_lon, self.lat_c - half_lat, self.lon_c + half_lon, self.lat_c + half_lat


def fit_view(
    coords: Sequence[tuple[float, float]], width: float, height: float, margin_x: float, margin_y: float
) -> View:
    """Fit ``(lat, lon)`` pairs into the frame with margins and a minimum extent."""
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    lat_c = (min(lats) + max(lats)) / 2
    lon_c = (min(lons) + max(lons)) / 2
    k = math.cos(math.radians(max(-80.0, min(80.0, lat_c))))
    span_x = max((max(lons) - min(lons)) * k, MIN_EXTENT_DEG)
    span_y = max(max(lats) - min(lats), MIN_EXTENT_DEG)
    scale = min((width - 2 * margin_x) / span_x, (height - 2 * margin_y) / span_y)
    return View(lat_c, lon_c, scale, width, height)


# ─── Route model (shared decision with the web map) ──────────────────────────────


def _valid_coords(point: dict[str, Any]) -> bool:
    lat, lon = point.get("lat"), point.get("lon")
    return (
        all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon))
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def plotted_points(geo_points: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The points the map draws: located, public, not interpolated (same rule as the web map)."""
    return [
        p
        for p in geo_points
        if p and _valid_coords(p) and p.get("_is_public") is not False and not p.get("_interpolated")
    ]


def point_label(point: dict[str, Any]) -> str:
    """Hop number, or "a–b" for a merged run of consecutive hops."""
    hop_range = point.get("_hop_range")
    return str(hop_range).replace("-", "–") if hop_range else str(point.get("hop") or "?")


def _contains_hop(point: dict[str, Any], hop: int) -> bool:
    hop_range = point.get("_hop_range")
    if not hop_range:
        try:
            return int(point.get("hop")) == hop
        except (TypeError, ValueError):
            return False
    try:
        low, high = (int(v) for v in str(hop_range).split("-", maxsplit=1))
    except ValueError:
        return False
    return low <= hop <= high


def is_approximate(point: dict[str, Any]) -> bool:
    return bool(
        point.get("_approximate")
        or point.get("accuracy_class") == "approximate"
        or point.get("loc_precision") in ("approximate", "country")
    )


def accuracy_radius_km(point: dict[str, Any]) -> float | None:
    radius = point.get("accuracy_radius_km")
    if isinstance(radius, (int, float)) and not isinstance(radius, bool) and 0 < radius <= MAX_RADIUS_KM:
        return float(radius)
    return None


@dataclass(frozen=True)
class MarkerGroup:
    """One drawn marker standing for one or more consecutive co-located map points."""

    members: tuple[int, ...]  # indexes into the plotted points
    center: tuple[float, float]  # true projected position
    display: tuple[float, float]  # position after fan-out
    kind: str  # hop | start | destination | both
    label: str
    approximate: bool
    radius_km: float | None


def _group_consecutive(xy: Sequence[tuple[float, float]], radius: float, solo: Sequence[bool] = ()) -> list[list[int]]:
    """Only consecutive visits share a marker: a later return to a place stays a separate node.

    A ``solo`` point (the destination) never joins a group, so its "D" marker keeps its own position.
    """
    groups: list[list[int]] = []
    for index, (x, y) in enumerate(xy):
        alone = index < len(solo) and solo[index]
        prev_alone = bool(groups) and any(m < len(solo) and solo[m] for m in groups[-1])
        if (
            groups
            and not alone
            and not prev_alone
            and all(math.hypot(xy[m][0] - x, xy[m][1] - y) <= radius for m in groups[-1])
        ):
            groups[-1].append(index)
        else:
            groups.append([index])
    return groups


def _fan_out(centers: list[tuple[float, float]], radius: float) -> list[tuple[float, float]]:
    """Spread overlapping group markers on a small circle around their mean."""
    parent = list(range(len(centers)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(centers):
        for j in range(i + 1, len(centers)):
            if math.hypot(a[0] - centers[j][0], a[1] - centers[j][1]) <= radius:
                parent[root(j)] = root(i)
    clusters: dict[int, list[int]] = {}
    for i in range(len(centers)):
        clusters.setdefault(root(i), []).append(i)
    display = list(centers)
    for members in clusters.values():
        if len(members) < 2:
            continue
        cx = sum(centers[m][0] for m in members) / len(members)
        cy = sum(centers[m][1] for m in members) / len(members)
        offset = max(radius * 0.75, len(members) * radius * 0.28)
        for n, m in enumerate(sorted(members)):
            angle = 2 * math.pi * n / len(members)
            display[m] = (cx + math.cos(angle) * offset, cy + math.sin(angle) * offset)
    return display


def build_marker_groups(
    points: Sequence[dict[str, Any]], view: View, start_hop: int | None, start_mapped: bool
) -> list[MarkerGroup]:
    """Cluster, label and classify the markers; mirrors the web map's grouping and "S"/"D" rules."""
    xy = [view.project(p["lat"], p["lon"]) for p in points]
    index_groups = _group_consecutive(xy, GROUP_RADIUS, [bool(p.get("_destination")) for p in points])
    centers = [(sum(xy[i][0] for i in g) / len(g), sum(xy[i][1] for i in g) / len(g)) for g in index_groups]
    displays = _fan_out(centers, GROUP_RADIUS)
    groups: list[MarkerGroup] = []
    for g, center, display in zip(index_groups, centers, displays, strict=True):
        start = next(
            (i for i in g if start_mapped and start_hop is not None and _contains_hop(points[i], start_hop)), None
        )
        dest = next((i for i in g if points[i].get("_destination")), None)
        labels = list(dict.fromkeys(point_label(points[i]) for i in g))

        def others(skip: list[int], labels: list[str] = labels) -> list[str]:
            kept = []
            for label in labels:
                first = label.replace("–", "-").split("-", 1)[0]
                if first.isdigit() and any(_contains_hop(points[i], int(first)) for i in skip):
                    continue
                kept.append(label)
            return kept

        if start is not None and dest is not None:
            kind, label = "both", "S/D"
        elif start is not None:
            kind, label = "start", "·".join(["S", *others([start])])
        elif dest is not None:
            kind, label = "destination", "·".join([*others([dest]), "D"])
        else:
            kind, label = "hop", "·".join(labels)
        primary = points[dest if dest is not None else start if start is not None else g[0]]
        groups.append(
            MarkerGroup(
                members=tuple(g),
                center=center,
                display=display,
                kind=kind,
                label=label,
                approximate=is_approximate(primary),
                radius_km=accuracy_radius_km(primary),
            )
        )
    return groups


@dataclass(frozen=True)
class RouteMapModel:
    """Everything the drawing needs, decided without any drawing library."""

    points: tuple[dict[str, Any], ...]
    plotted_hops: int
    total_hops: int
    start_hop: int | None
    start_mapped: bool
    destination: dict[str, Any] | None
    has_interpolation: bool

    @property
    def subtitle(self) -> str:
        text = f"Geolocated route for {self.plotted_hops} of {self.total_hops} hops"
        return text if self.destination else f"{text} · Destination not on map"


def build_route_map_model(
    geo_points: Iterable[dict[str, Any]],
    display_hops: Sequence[dict[str, Any]],
    *,
    has_interpolation: bool = False,
) -> RouteMapModel | None:
    """Build the model from the web map's inputs; ``None`` when no hop can be placed."""
    points = plotted_points(geo_points)
    if not points:
        return None
    start_hop = None
    if display_hops:
        try:
            start_hop = int(display_hops[0].get("hop"))
        except (TypeError, ValueError):
            start_hop = None
    return RouteMapModel(
        points=tuple(points),
        plotted_hops=len(mapped_hop_numbers(points)),
        total_hops=logical_hop_count(display_hops),
        start_hop=start_hop,
        start_mapped=start_hop is not None and route_start_is_mapped(start_hop, points),
        destination=next((p for p in reversed(points) if p.get("_destination")), None),
        has_interpolation=has_interpolation,
    )


def segment_is_dashed(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (
        first.get("segment_to_next") == "dashed"
        or bool(second.get("_interpolated"))
        or second.get("segment_type") == "dashed"
    )


# ─── Geometry helpers ────────────────────────────────────────────────────────────


def clip_ring(ring: Sequence[tuple[float, float]], rect: Rect) -> list[tuple[float, float]]:
    """Sutherland–Hodgman clip of a ring to an axis-aligned rectangle."""
    x0, y0, x1, y1 = rect
    edges = (
        (lambda p: p[0] >= x0, lambda a, b: (x0, a[1] + (b[1] - a[1]) * (x0 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[0] <= x1, lambda a, b: (x1, a[1] + (b[1] - a[1]) * (x1 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[1] >= y0, lambda a, b: (a[0] + (b[0] - a[0]) * (y0 - a[1]) / (b[1] - a[1]), y0)),
        (lambda p: p[1] <= y1, lambda a, b: (a[0] + (b[0] - a[0]) * (y1 - a[1]) / (b[1] - a[1]), y1)),
    )
    out = list(ring)
    for inside, cross in edges:
        if not out:
            break
        src, out = out, []
        prev = src[-1]
        for cur in src:
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
            prev = cur
    return out


def land_rings(basemap: Basemap, view: View, pad: float = 2.0) -> list[list[list[tuple[float, float]]]]:
    """Projected, clipped and thinned rings of the polygons that touch the frame."""
    lon0, lat0, lon1, lat1 = view.bounds()
    rect = (-pad, -pad, view.width + pad, view.height + pad)
    result: list[list[list[tuple[float, float]]]] = []
    for (bx0, by0, bx1, by1), rings in basemap.polygons:
        if bx1 < lon0 or bx0 > lon1 or by1 < lat0 or by0 > lat1:
            continue
        kept = []
        for ring in rings:
            thinned: list[tuple[float, float]] = []
            for lon, lat in ring:
                p = view.project(lat, lon)
                if not thinned or math.hypot(p[0] - thinned[-1][0], p[1] - thinned[-1][1]) >= 0.35:
                    thinned.append(p)
            clipped = clip_ring(thinned, rect) if len(thinned) >= 3 else []
            if len(clipped) >= 3:
                kept.append(clipped)
        if kept:
            result.append(kept)
    return result


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _nice_km(target: float) -> float:
    """Largest 1/2/5 x 10^n km value not above ``target``."""
    if target <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(target))
    for step in (5, 2, 1):
        if step * exp <= target:
            return step * exp
    return exp


# ─── Flowable ────────────────────────────────────────────────────────────────────


class RouteMap(Flowable):
    """Title, framed map, legend and attribution as one unbreakable block."""

    _TITLE_H = 11 * mm
    _FOOT_H = 11 * mm
    _MARK_R = 2.7 * mm

    def __init__(
        self,
        model: RouteMapModel,
        font: str,
        font_bold: str,
        width: float,
        height: float = 90 * mm,
        basemap: Basemap | None = None,
    ):
        super().__init__()
        self.model, self.font, self.font_bold = model, font, font_bold
        self.width, self.height = width, height
        self._basemap = basemap
        self.hAlign = "LEFT"

    def wrap(self, _avail_w: float, _avail_h: float) -> tuple[float, float]:
        return self.width, self.height

    @property
    def frame_h(self) -> float:
        return self.height - self._TITLE_H - self._FOOT_H

    def view(self) -> View:
        coords = [(p["lat"], p["lon"]) for p in self.model.points]
        return fit_view(coords, self.width, self.frame_h, margin_x=13 * mm, margin_y=11 * mm)

    # -- drawing --------------------------------------------------------------------

    def draw(self) -> None:
        c = self.canv
        view = self.view()
        groups = build_marker_groups(self.model.points, view, self.model.start_hop, self.model.start_mapped)
        c.saveState()
        self._draw_header()
        c.translate(0, self._FOOT_H)
        frame = c.beginPath()
        frame.rect(0, 0, self.width, self.frame_h)
        c.setFillColor(_SEA)
        c.rect(0, 0, self.width, self.frame_h, fill=1, stroke=0)
        c.saveState()
        c.clipPath(frame, stroke=0, fill=0)
        self._draw_land(view)
        placed: list[Rect] = [self._marker_rect(g) for g in groups]
        self._draw_country_names(view, placed)
        self._draw_accuracy(view, groups)
        self._draw_route(view, groups)
        for g in groups:
            self._draw_marker(g)
        self._draw_city_names(groups, placed)
        c.restoreState()
        c.setStrokeColor(_LAND_LINE)
        c.setLineWidth(0.6)
        c.rect(0, 0, self.width, self.frame_h, fill=0, stroke=1)
        self._draw_scale(view)
        c.translate(0, -self._FOOT_H)
        self._draw_footer(groups)
        c.restoreState()

    def _draw_header(self) -> None:
        c = self.canv
        c.setFillColor(_INK)
        c.setFont(self.font_bold, 11)
        c.drawString(0, self.height - 4.5 * mm, "Route map")
        c.setFillColor(_MUTED)
        c.setFont(self.font, FONT_SIZE)
        c.drawString(0, self.height - 9 * mm, self.model.subtitle)

    def _draw_land(self, view: View) -> None:
        c = self.canv
        basemap = self._basemap or load_basemap()
        c.setFillColor(_LAND)
        c.setStrokeColor(_LAND_LINE)
        c.setLineWidth(0.4)
        for rings in land_rings(basemap, view):
            path = c.beginPath()
            for ring in rings:
                path.moveTo(*ring[0])
                for p in ring[1:]:
                    path.lineTo(*p)
                path.close()
            c.drawPath(path, fill=1, stroke=1, fillMode=FILL_EVEN_ODD)  # holes stay open

    def _marker_size(self, group: MarkerGroup) -> tuple[float, float]:
        w = stringWidth(group.label, self.font_bold, FONT_SIZE) + 2.6 * mm
        r = self._MARK_R
        if group.kind in ("destination", "both"):
            return max(2 * r, w) + 1.2 * mm, 2 * r + 1.2 * mm
        return max(2 * r, w), 2 * r

    def _marker_rect(self, group: MarkerGroup) -> Rect:
        w, h = self._marker_size(group)
        x, y = group.display
        return x - w / 2, y - h / 2, x + w / 2, y + h / 2

    def _draw_country_names(self, view: View, placed: list[Rect]) -> None:
        c = self.canv
        basemap = self._basemap or load_basemap()
        lon0, lat0, lon1, lat1 = view.bounds()
        c.setFont(self.font, 7)
        c.setFillColor(colors.HexColor("#8a94a1"))
        shown = 0
        for name, lat, lon, _zoom in sorted(basemap.countries, key=lambda item: (item[3], item[0])):
            if shown >= 8 or len(name) > 16 or not (lon0 < lon < lon1 and lat0 < lat < lat1):
                continue
            x, y = view.project(lat, lon)
            w = stringWidth(name, self.font, 7)
            rect = (x - w / 2, y - 3, x + w / 2, y + 7)
            if rect[0] < 1 or rect[2] > view.width - 1 or rect[1] < 1 or rect[3] > view.height - 1:
                continue
            if any(_overlaps(rect, r) for r in placed):
                continue
            c.drawCentredString(x, y, name.upper() if len(name) <= 3 else name)
            placed.append(rect)
            shown += 1

    def _draw_accuracy(self, view: View, groups: Sequence[MarkerGroup]) -> None:
        c = self.canv
        c.setStrokeColor(_MUTED)
        c.setLineWidth(0.7)
        c.setDash(2, 2)
        for g in groups:
            if g.approximate and g.radius_km:
                radius = g.radius_km / KM_PER_DEG_LAT * view.scale
                if radius > self._MARK_R:
                    c.circle(g.center[0], g.center[1], radius, fill=0, stroke=1)
        c.setDash()

    def _draw_route(self, view: View, groups: Sequence[MarkerGroup]) -> None:
        c = self.canv
        pts = self.model.points
        xy = [view.project(p["lat"], p["lon"]) for p in pts]
        c.setStrokeColor(_INK)
        c.setFillColor(_INK)
        c.setLineCap(1)
        for i in range(len(pts) - 1):
            a, b = xy[i], xy[i + 1]
            if a == b:
                continue
            dashed = segment_is_dashed(pts[i], pts[i + 1])
            c.setLineWidth(1.2 if dashed else 1.6)
            c.setDash(*((4, 3.5) if dashed else ()))
            c.line(a[0], a[1], b[0], b[1])
            c.setDash()
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length > 14 * mm:
                self._arrow((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, math.atan2(b[1] - a[1], b[0] - a[0]))
        c.setLineWidth(0.6)
        c.setDash(1, 2)
        for g in groups:
            for m in g.members:
                if math.hypot(xy[m][0] - g.display[0], xy[m][1] - g.display[1]) > 3:
                    c.line(xy[m][0], xy[m][1], g.display[0], g.display[1])
        c.setDash()

    def _arrow(self, x: float, y: float, angle: float) -> None:
        c = self.canv
        size = 1.6 * mm
        c.saveState()
        c.translate(x, y)
        c.rotate(math.degrees(angle))
        path = c.beginPath()
        path.moveTo(size, 0)
        path.lineTo(-size * 0.7, size * 0.7)
        path.lineTo(-size * 0.7, -size * 0.7)
        path.close()
        c.setFillColor(_INK)
        c.drawPath(path, fill=1, stroke=0)
        c.restoreState()

    def _draw_marker(self, g: MarkerGroup) -> None:
        c = self.canv
        w, h = self._marker_size(g)
        x, y = g.display
        fill = {"start": _START, "destination": _DEST, "both": _DEST}.get(g.kind, _INK)
        hollow = g.approximate
        c.setLineWidth(1.4 if hollow else 0.8)
        c.setDash(*((2.2, 1.6) if hollow else ()))
        c.setStrokeColor(fill if hollow else colors.white)
        c.setFillColor(colors.white if hollow else fill)
        if g.kind in ("destination", "both"):
            path = c.beginPath()
            path.moveTo(x, y + h / 2)
            path.lineTo(x + w / 2, y)
            path.lineTo(x, y - h / 2)
            path.lineTo(x - w / 2, y)
            path.close()
            c.drawPath(path, fill=1, stroke=1)
        elif g.kind == "start":
            c.roundRect(x - w / 2, y - h / 2, w, h, 0.6 * mm, fill=1, stroke=1)
        elif w > h + 0.1:
            c.roundRect(x - w / 2, y - h / 2, w, h, h / 2, fill=1, stroke=1)
        else:
            c.circle(x, y, h / 2, fill=1, stroke=1)
        c.setDash()
        c.setFillColor(fill if hollow else colors.white)
        c.setFont(self.font_bold, FONT_SIZE)
        c.drawCentredString(x, y - FONT_SIZE * 0.35, g.label)

    def _draw_city_names(self, groups: Sequence[MarkerGroup], placed: list[Rect]) -> None:
        c = self.canv
        c.setFont(self.font, FONT_SIZE)
        c.setFillColor(_INK)
        for g in groups:
            point = self.model.points[g.members[0]]
            name = str(point.get("city") or "")
            if not name:
                continue
            w, h = self._marker_size(g)
            tw = stringWidth(name, self.font, FONT_SIZE)
            x, y = g.display
            gap = 1.2 * mm
            for tx, ty in (
                (x + w / 2 + gap, y - 3),
                (x - w / 2 - gap - tw, y - 3),
                (x - tw / 2, y + h / 2 + 1.5),
                (x - tw / 2, y - h / 2 - FONT_SIZE),
            ):
                rect = (tx - 1, ty - 1.5, tx + tw + 1, ty + FONT_SIZE)
                if rect[0] < 1 or rect[2] > self.width - 1 or rect[1] < 1 or rect[3] > self.frame_h - 1:
                    continue
                if any(_overlaps(rect, r) for r in placed):
                    continue
                c.drawString(tx, ty, name)
                placed.append(rect)
                break

    def _draw_scale(self, view: View) -> None:
        c = self.canv
        km_per_pt = KM_PER_DEG_LAT / view.scale
        km = _nice_km(self.width * 0.22 * km_per_pt)
        length = km / km_per_pt
        x, y = self.width - 3 * mm - length, 3.2 * mm
        c.setStrokeColor(_INK)
        c.setLineWidth(1.0)
        c.line(x, y, x + length, y)
        c.line(x, y - 1.2, x, y + 1.2)
        c.line(x + length, y - 1.2, x + length, y + 1.2)
        c.setFillColor(_INK)
        c.setFont(self.font, 7)
        c.drawRightString(x + length, y + 1.8, f"{km:g} km")

    def _draw_footer(self, groups: Sequence[MarkerGroup]) -> None:
        c = self.canv
        items = [("hop", "Hop")]
        if self.model.start_mapped:
            items.insert(0, ("start", "Start"))
        if self.model.destination:
            items.append(("destination", "Destination"))
        if self.model.has_interpolation:
            items.append(("dashed", "Interpolated"))
        if any(g.approximate for g in groups):
            items.append(("approx", "Approximate"))
        x, y = 0.0, 5.6 * mm
        c.setFont(self.font, FONT_SIZE)
        for kind, text in items:
            x = self._legend_symbol(kind, x, y)
            c.setFillColor(_INK)
            c.setFont(self.font, FONT_SIZE)
            c.drawString(x, y - 2.6, text)
            x += stringWidth(text, self.font, FONT_SIZE) + 5 * mm
        c.setFillColor(_MUTED)
        c.setFont(self.font, 7)
        c.drawString(
            0,
            1.2 * mm,
            f"{ATTRIBUTION} · Equirectangular, true scale at {self.view().lat_c:.0f}° latitude · GeoIP estimates",
        )

    def _legend_symbol(self, kind: str, x: float, y: float) -> float:
        c = self.canv
        r = 1.5 * mm
        c.saveState()
        c.setLineWidth(1.2)
        if kind == "dashed":
            c.setStrokeColor(_INK)
            c.setDash(3, 2.5)
            c.line(x, y, x + 8 * mm, y)
            c.restoreState()
            return x + 9 * mm
        fill = {"start": _START, "destination": _DEST}.get(kind, _INK)
        cx = x + r
        if kind == "approx":
            c.setStrokeColor(_INK)
            c.setFillColor(colors.white)
            c.setDash(2, 1.5)
            c.circle(cx, y, r, fill=1, stroke=1)
        elif kind == "destination":
            c.setFillColor(fill)
            path = c.beginPath()
            path.moveTo(cx, y + r * 1.25)
            path.lineTo(cx + r * 1.25, y)
            path.lineTo(cx, y - r * 1.25)
            path.lineTo(cx - r * 1.25, y)
            path.close()
            c.drawPath(path, fill=1, stroke=0)
        elif kind == "start":
            c.setFillColor(fill)
            c.roundRect(cx - r, y - r, 2 * r, 2 * r, 0.5 * mm, fill=1, stroke=0)
        else:
            c.setFillColor(fill)
            c.circle(cx, y, r, fill=1, stroke=0)
        c.restoreState()
        return x + 2 * r + 1.6 * mm
