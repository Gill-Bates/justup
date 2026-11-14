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

import itertools
import json
import math
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import FILL_EVEN_ODD
from reportlab.platypus import Flowable

from app.services.geo_resolver import ACCURACY_APPROXIMATE
from app.services.route_map_rules import (
    BASEMAP_FILE,
    DASH_ARRAY,
    LABELS_FILE,
    LINE_HALO_PX,
    LINE_WEIGHT_PX,
    MARKER_GAP_PX,
    MAX_CITY_LABELS,
    MIN_ARROW_SEGMENT_PX,
    PALETTE,
    polygon_parts,
    select_labels,
)
from app.services.route_timeline import logical_hop_count, mapped_hop_numbers, route_start_is_mapped
from app.services.traceroute_geo import (
    LOC_APPROXIMATE,
    LOC_COUNTRY,
    MAP_ROLE_ESTIMATE,
    META_MAP_RADIUS_KM,
    META_MAP_ROLE,
    SEGMENT_DASHED,
    route_map_features,
)

GEO_DIR = BASEMAP_FILE.parent  # the dataset paths are owned by route_map_rules

MIN_EXTENT_DEG = 3.0  # smallest visible span, so a single region stays recognisable
KM_PER_DEG_LAT = 111.19
GROUP_RADIUS = 7.5 * mm  # consecutive hops closer than this share one marker
FONT_SIZE = 8.0
ATTRIBUTION = "Basemap: Natural Earth (public domain)"
NO_MAP_NOTE = "No hop could be placed on a map."

_PX = 0.75  # points per CSS pixel: the shared line rules are written in pixels
_THEME = PALETTE["light"]  # a printed report always uses the light palette
_ROUTE = colors.HexColor(_THEME["route"])
_HALO = colors.HexColor(_THEME["halo"])
_MARKERS = PALETTE["markers"]["print"]  # print marker colours, defined in route_map_rules.PALETTE
_INK = colors.HexColor(_MARKERS["hop"])
_START = colors.HexColor(_MARKERS["start"])
_DEST = colors.HexColor(_MARKERS["end"])
DESTINATION_COLOR = _DEST  # shared with the network path diagram so both show the same destination marker
_LAND = colors.HexColor(_THEME["land"])
_LAND_LINE = colors.HexColor(_THEME["border"])
_SEA = colors.HexColor(_THEME["water"])
_LABEL = colors.HexColor(_THEME["label"])
_MUTED = colors.HexColor("#5c6670")

Rect = tuple[float, float, float, float]


def draw_destination_diamond(
    c, cx: float, cy: float, half: float, fill, font_bold: str, font_size: float, glyph: str = "D"
) -> None:
    """Destination marker: filled diamond with a white glyph, as in the map legend and path diagram."""
    c.saveState()
    c.setDash()
    c.setLineWidth(0.8)
    c.setStrokeColor(colors.white)
    c.setFillColor(fill)
    path = c.beginPath()
    path.moveTo(cx, cy + half)
    path.lineTo(cx + half, cy)
    path.lineTo(cx, cy - half)
    path.lineTo(cx - half, cy)
    path.close()
    c.drawPath(path, fill=1, stroke=1)
    c.setFillColor(colors.white)
    c.setFont(font_bold, font_size)
    c.drawCentredString(cx, cy - font_size * 0.35, glyph)
    c.restoreState()


# ─── Basemap data ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Basemap:
    """Country polygons as (bbox, rings of (lon, lat)) plus label points."""

    polygons: tuple[tuple[tuple[float, float, float, float], tuple[tuple[tuple[float, float], ...], ...]], ...]
    countries: tuple[tuple[str, float, float, float], ...]  # name, lat, lon, minZoom


_basemap: Basemap | None = None
_basemap_lock = threading.Lock()


def _parse_basemap(world: dict[str, Any], labels: dict[str, Any]) -> Basemap:
    polygons = []
    for feature in world.get("features", []):
        for rings in polygon_parts(feature.get("geometry") or {}):
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


def _lon_scale(lat: float) -> float:
    """Equirectangular longitude scale; fit and projection must use the same clamped latitude."""
    return math.cos(math.radians(max(-80.0, min(80.0, lat))))


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
        return _lon_scale(self.lat_c)

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
    k = _lon_scale(lat_c)
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
        or point.get("accuracy_class") == ACCURACY_APPROXIMATE
        or point.get("loc_precision") in (LOC_APPROXIMATE, LOC_COUNTRY)
    )


def is_estimate(point: dict[str, Any]) -> bool:
    """An approximate position the server keeps off the route line (``map_role``)."""
    return point.get(META_MAP_ROLE) == MAP_ROLE_ESTIMATE


def accuracy_radius_km(point: dict[str, Any]) -> float | None:
    """The radius the server allows to be drawn around an approximate position."""
    radius = point.get(META_MAP_RADIUS_KM)
    return float(radius) if isinstance(radius, (int, float)) and not isinstance(radius, bool) and radius > 0 else None


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
    # The destination and every estimate keep a marker of their own, like the web map.
    solo = [bool(p.get("_destination")) or is_estimate(p) for p in points]
    index_groups = _group_consecutive(xy, GROUP_RADIUS, solo)
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
        elif any(is_estimate(points[i]) for i in g):
            kind, label = "estimate", "~" + "·".join(labels)
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
    has_unknown_link: bool
    has_approximate: bool

    @property
    def subtitle(self) -> str:
        text = f"Geolocated route for {self.plotted_hops} of {self.total_hops} hops"
        return text if self.destination else f"{text} · Destination not on map"


def build_route_map_model(
    geo_points: Iterable[dict[str, Any]],
    display_hops: Sequence[dict[str, Any]],
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
    features = route_map_features(points)
    return RouteMapModel(
        points=tuple(points),
        plotted_hops=len(mapped_hop_numbers(points)),
        total_hops=logical_hop_count(display_hops),
        start_hop=start_hop,
        start_mapped=start_hop is not None and route_start_is_mapped(start_hop, points),
        destination=next((p for p in reversed(points) if p.get("_destination")), None),
        has_unknown_link=features["has_unknown_link"],
        has_approximate=features["has_approximate"],
    )


def route_segments(points: Sequence[dict[str, Any]]) -> list[tuple[int, int, bool]]:
    """Chain links as (from index, to index, dashed): every plotted point is a node, in hop order.

    The server decides which links are estimated or unknown (``segment_to_next``); this only reads it.
    """
    return [
        (a, b, points[a].get("segment_to_next") == SEGMENT_DASHED) for a, b in itertools.pairwise(range(len(points)))
    ]


def pdf_arrow_indices(
    chain: Sequence[tuple[tuple[float, float], tuple[float, float], bool]],
) -> list[int]:
    """PDF-only arrow policy: long solid links indicate direction; dashed links remain unarrowed."""
    return [
        index
        for index, (a, b, dashed) in enumerate(chain)
        if not dashed and math.hypot(b[0] - a[0], b[1] - a[1]) > MIN_ARROW_SEGMENT_PX * _PX
    ]


def dash_pattern(weight_pt: float) -> tuple[float, float]:
    """The shared dash rule (stroke, gap in px at the shared weight) scaled to this line weight."""
    stroke, gap = (float(v) for v in DASH_ARRAY.split())
    return stroke * _PX * weight_pt / (LINE_WEIGHT_PX * _PX), gap * _PX * weight_pt / (LINE_WEIGHT_PX * _PX)


def _seg_hits_rect(a: tuple[float, float], b: tuple[float, float], rect: Rect) -> bool:
    """Liang-Barsky: does the segment a-b cross the rectangle?"""
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - rect[0]), (dx, rect[2] - a[0]), (-dy, a[1] - rect[1]), (dy, rect[3] - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


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
        labels = select_labels(self.model.points)
        chain = self._chain(groups)
        self._draw_country_names(view, placed, chain, labels["countries"])
        self._draw_accuracy(view, groups)
        self._draw_route(view, groups, chain)
        for g in groups:
            self._draw_marker(g)
        self._draw_city_names(view, groups, placed, chain, labels["cities"])
        c.restoreState()
        c.setStrokeColor(_LAND_LINE)
        c.setLineWidth(0.6)
        c.rect(0, 0, self.width, self.frame_h, fill=0, stroke=1)
        self._draw_scale(view)
        c.translate(0, -self._FOOT_H)
        self._draw_footer()
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

    def _draw_country_names(
        self,
        view: View,
        placed: list[Rect],
        chain: Sequence[tuple[tuple[float, float], tuple[float, float], bool]],
        countries: Sequence[dict[str, Any]],
    ) -> None:
        """Name only the countries of the path (shared selection); a name never covers a marker or a link."""
        c = self.canv
        c.setFont(self.font, 7)
        c.setFillColor(_LABEL)
        for item in countries:
            name = item["name"]
            if not self._has_glyphs(name):
                continue
            x, y = view.project(item["lat"], item["lon"])
            w = stringWidth(name, self.font, 7)
            rect = (x - w / 2, y - 3, x + w / 2, y + 7)
            if rect[0] < 1 or rect[2] > view.width - 1 or rect[1] < 1 or rect[3] > view.height - 1:
                continue
            if any(_overlaps(rect, r) for r in placed) or any(_seg_hits_rect(a, b, rect) for a, b, _ in chain):
                continue
            c.drawCentredString(x, y, name.upper() if len(name) <= 3 else name)
            placed.append(rect)

    def _has_glyphs(self, text: str) -> bool:
        """False when the embedded font lacks a glyph; such a name would render as boxes."""
        font = pdfmetrics.getFont(self.font)
        glyphs = getattr(font.face, "charToGlyph", None)
        if glyphs is not None:
            return all(ord(ch) in glyphs for ch in text)

        # ReportLab's built-in Type 1 fonts use an encoding vector instead of a
        # Unicode charToGlyph map. Check that vector so fallback fonts work too.
        encoding = getattr(font, "encoding", None)
        vector = getattr(encoding, "vector", None)
        if vector is None:
            return False
        return all(ord(ch) < len(vector) and vector[ord(ch)] not in (None, ".notdef") for ch in text)

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

    def _chain(self, groups: Sequence[MarkerGroup]) -> list[tuple[tuple[float, float], tuple[float, float], bool]]:
        """Chain links between the displayed marker positions, cut off at the marker edges."""
        pts = self.model.points
        owner = {m: g for g in groups for m in g.members}
        links = []
        for first, second, dashed in route_segments(pts):
            ga, gb = owner[first], owner[second]
            if ga is gb:
                continue
            a, b = ga.display, gb.display
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length == 0:
                continue
            ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
            ta, tb = self._edge(ga, ux, uy), self._edge(gb, -ux, -uy)
            if length <= ta + tb:
                continue  # the markers touch: nothing to connect
            links.append(((a[0] + ux * ta, a[1] + uy * ta), (b[0] - ux * tb, b[1] - uy * tb), dashed))
        return links

    def _edge(self, group: MarkerGroup, ux: float, uy: float) -> float:
        """Distance from the marker centre to its edge along (ux, uy), plus the shared gap."""
        w, h = self._marker_size(group)
        t = min(w / 2 / abs(ux) if ux else math.inf, h / 2 / abs(uy) if uy else math.inf)
        return t + MARKER_GAP_PX * _PX

    def _draw_route(
        self,
        view: View,
        groups: Sequence[MarkerGroup],
        chain: Sequence[tuple[tuple[float, float], tuple[float, float], bool]],
    ) -> None:
        c = self.canv
        xy = [view.project(p["lat"], p["lon"]) for p in self.model.points]
        weight = LINE_WEIGHT_PX * _PX
        halo = (LINE_WEIGHT_PX + LINE_HALO_PX) * _PX
        c.setLineCap(0)
        for pass_color, width in ((_HALO, halo), (_ROUTE, weight)):  # contrast outline first, line on top
            c.setStrokeColor(pass_color)
            c.setLineWidth(width)
            for a, b, dashed in chain:
                c.setDash(*(dash_pattern(width) if dashed else ()))
                c.line(a[0], a[1], b[0], b[1])
        c.setDash()
        c.setFillColor(_ROUTE)
        for index in pdf_arrow_indices(chain):
            a, b, _dashed = chain[index]
            self._arrow((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, math.atan2(b[1] - a[1], b[0] - a[0]))
        c.setStrokeColor(_ROUTE)
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
        c.setFillColor(_ROUTE)
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

    def _draw_city_names(
        self,
        view: View,
        groups: Sequence[MarkerGroup],
        placed: list[Rect],
        chain: Sequence[tuple[tuple[float, float], tuple[float, float], bool]],
        cities: Sequence[dict[str, Any]],
    ) -> None:
        """Hop cities beside their marker, then a few orientation cities; one cap, no overlap with anything."""
        c = self.canv
        c.setFont(self.font, FONT_SIZE)
        by_position = {(self.model.points[m]["lat"], self.model.points[m]["lon"]): g for g in groups for m in g.members}
        drawn = 0
        for item in cities:
            if drawn >= MAX_CITY_LABELS:
                break
            name = item["name"]
            if not self._has_glyphs(name):
                continue
            tw = stringWidth(name, self.font, FONT_SIZE)
            group = by_position.get((item["lat"], item["lon"]))
            if group is not None:
                w, h = self._marker_size(group)
                x, y = group.display
                gap = 1.2 * mm
                spots = (
                    (x + w / 2 + gap, y - 3),
                    (x - w / 2 - gap - tw, y - 3),
                    (x - tw / 2, y + h / 2 + 1.5),
                    (x - tw / 2, y - h / 2 - FONT_SIZE),
                )
                c.setFillColor(_INK)
            else:
                x, y = view.project(item["lat"], item["lon"])
                spots = ((x + 2 * mm, y - 3),)
                c.setFillColor(_LABEL)
            for tx, ty in spots:
                rect = (tx - 1, ty - 1.5, tx + tw + 1, ty + FONT_SIZE)
                if rect[0] < 1 or rect[2] > self.width - 1 or rect[1] < 1 or rect[3] > self.frame_h - 1:
                    continue
                if any(_overlaps(rect, r) for r in placed) or any(_seg_hits_rect(a, b, rect) for a, b, _ in chain):
                    continue
                c.drawString(tx, ty, name)
                placed.append(rect)
                drawn += 1
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

    def _draw_footer(self) -> None:
        c = self.canv
        items = [("hop", "Hop")]
        if self.model.start_mapped:
            items.insert(0, ("start", "Start"))
        if self.model.destination:
            items.append(("destination", "Destination"))
        if self.model.has_approximate:
            items.append(("approx", "Approximate (~)"))
        items.append(("solid", "Link measured"))
        if self.model.has_unknown_link:
            items.append(("dashed", "Link estimated or unknown"))
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
        if kind in ("dashed", "solid"):
            weight = LINE_WEIGHT_PX * _PX
            c.setStrokeColor(_ROUTE)
            c.setLineWidth(weight)
            c.setDash(*(dash_pattern(weight) if kind == "dashed" else ()))
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
            draw_destination_diamond(c, cx, y, r * 1.6, fill, self.font_bold, 6.5)
        elif kind == "start":
            c.setFillColor(fill)
            c.roundRect(cx - r, y - r, 2 * r, 2 * r, 0.5 * mm, fill=1, stroke=0)
        else:
            c.setFillColor(fill)
            c.circle(cx, y, r, fill=1, stroke=0)
        c.restoreState()
        return x + 2 * r + 1.6 * mm
