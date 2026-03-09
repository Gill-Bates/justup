#!/usr/bin/env python3
#
# app/services/traceroute_worldmap.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Stable world-map renderer for traceroute paths.

Goal: reproducible, comparable output across time (reports/PDFs).
- Fixed projection (PlateCarree)
- Deterministic route-zoom (min-span + fixed padding factor)
- 600 DPI for sharp PDF/print output

Returns PNG bytes or None when rendering is unavailable.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from app.services.traceroute_geo import SEGMENT_DASHED

JUSTUP_GREEN = "#198754"
JUSTUP_BLUE = "#0d6efd"
JUSTUP_RED = "#dc3545"

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend, must be set before pyplot import
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
import matplotlib.font_manager as fm
from matplotlib.path import Path
from matplotlib.patches import FancyArrowPatch
from matplotlib.patches import Rectangle
from pathlib import Path as FilePath

# Register Roboto font for matplotlib (if available)
_FONT_DIR = FilePath(__file__).parent.parent / "static" / "fonts"
_ROBOTO_FONT = _FONT_DIR / "Roboto-Regular.ttf"
if _ROBOTO_FONT.exists():
    fm.fontManager.addfont(str(_ROBOTO_FONT))
    _ROBOTO_FAMILY = "Roboto"
else:
    _ROBOTO_FAMILY = "sans-serif"  # Fallback

try:
	from PIL import Image as PILImage
	_HAS_PIL = True
except Exception:
	_HAS_PIL = False

try:
	from adjustText import adjust_text
	_HAS_ADJUSTTEXT = True
except ImportError:
	_HAS_ADJUSTTEXT = False

_log = logging.getLogger(__name__)

try:
	import cartopy.crs as ccrs
	import cartopy.feature as cfeature
	from cartopy.io.shapereader import Reader
	import cartopy.io.shapereader as shpreader
	_HAS_CARTOPY = True
	
	# Pre-warm Natural Earth shapefiles (avoids download delay on first render)
	# These are cached in CARTOPY_USER_BACKGROUNDS or ~/.local/share/cartopy
	try:
		_ = cfeature.LAND.with_scale("10m")
		_ = cfeature.OCEAN.with_scale("10m")
		_ = cfeature.BORDERS.with_scale("10m")
		_ = cfeature.COASTLINE.with_scale("10m")
	except Exception:
		pass  # Pre-warming is optional, will download on first use
except Exception:
	_log.info("cartopy not installed, world map rendering disabled")
	_HAS_CARTOPY = False


def render_traceroute_worldmap(
	geo_points: list[dict[str, Any]],
	*,
	width_in: float = 6.5,
	height_in: float = 3.5,
	dpi: int = 600,
) -> bytes | None:
	"""Render traceroute on a stable world map.

	- Projection: PlateCarree
	- 600 DPI for sharp PDF/print output
	- NaturalEarth base layers (ocean, land, borders, states, rivers)
	- Dominant start/destination markers with labels
	"""
	if not _HAS_CARTOPY:
		return None
	if not geo_points or len(geo_points) < 2:
		return None

	def _pin_marker() -> Path:
		# Simple location-pin / teardrop shape, normalized around origin.
		verts = [
			(0.0, 1.15),
			(0.70, 1.10),
			(1.05, 0.45),
			(1.05, -0.20),
			(0.60, -0.85),
			(0.0, -1.45),
			(-0.60, -0.85),
			(-1.05, -0.20),
			(-1.05, 0.45),
			(-0.70, 1.10),
			(0.0, 1.15),
		]
		codes = [
			Path.MOVETO,
			Path.CURVE3,
			Path.CURVE3,
			Path.CURVE3,
			Path.CURVE3,
			Path.CURVE3,
			Path.CURVE3,
			Path.CURVE3,
			Path.CURVE3,
			Path.CURVE3,
			Path.CLOSEPOLY,
		]
		return Path(verts, codes)

	pin_marker = _pin_marker()

	valid_points: list[dict[str, Any]] = []
	for p in geo_points:
		if "lat" not in p or "lon" not in p:
			continue
		try:
			lat = float(p["lat"])
			lon = float(p["lon"])
		except Exception:
			continue
		if not (-90.0 <= lat <= 90.0):
			continue
		if not (-180.0 <= lon <= 180.0):
			continue
		pp = dict(p)
		pp["lat"] = lat
		pp["lon"] = lon
		valid_points.append(pp)
	if len(valid_points) < 2:
		return None

	lats = [p["lat"] for p in valid_points]
	lons = [p["lon"] for p in valid_points]

	try:
		fig = plt.figure(figsize=(width_in, height_in))
		# Full-bleed axes: the map can reach the image edge.
		ax = fig.add_axes([0.0, 0.0, 1.0, 1.0], projection=ccrs.PlateCarree())
		ax.set_position([0.0, 0.0, 1.0, 1.0])

		# Remove Cartopy's axes/background patches (they define hard clip edges for
		# geoms). Labels can draw outside; geoms typically cannot unless patches are hidden.
		try:
			if getattr(ax, "outline_patch", None) is not None:
				ax.outline_patch.set_visible(False)
		except Exception:
			pass
		try:
			if getattr(ax, "background_patch", None) is not None:
				ax.background_patch.set_visible(False)
		except Exception:
			pass
		try:
			ax.patch.set_visible(False)
		except Exception:
			pass

		# Zoom-to-fit with moderate padding (keeps detail, avoids "too zoomed out")
		min_lon, max_lon = min(lons), max(lons)
		min_lat, max_lat = min(lats), max(lats)

		lat_span = max((max_lat - min_lat), 4.0)
		lon_span = max((max_lon - min_lon), 4.0)
		pad_lat = max(lat_span * 0.20, 1.0)
		pad_lon = max(lon_span * 0.20, 1.0)

		bbox = [
			max(min_lon - pad_lon, -180),
			min(max_lon + pad_lon, 180),
			max(min_lat - pad_lat, -90),
			min(max_lat + pad_lat, 90),
		]

		# Reserve space for labels FIRST (degrees, deterministic)
		# This padding ensures city names fit within map bounds
		LABEL_PAD_LON = max(lon_span * 0.12, 1.2)
		LABEL_PAD_LAT = max(lat_span * 0.10, 1.0)
		lon0 = min_lon - pad_lon - LABEL_PAD_LON
		lon1 = max_lon + pad_lon + LABEL_PAD_LON
		lat0 = min_lat - pad_lat - LABEL_PAD_LAT
		lat1 = max_lat + pad_lat + LABEL_PAD_LAT

		# Force extent aspect ratio to match the figure (box) AFTER padding
		desired = width_in / height_in
		lon_span = lon1 - lon0
		lat_span = lat1 - lat0
		if lat_span <= 0:
			lat_span = 1e-6

		current = lon_span / lat_span

		if current > desired:
			target_lat_span = lon_span / desired
			extra = (target_lat_span - lat_span) / 2
			lat0 -= extra
			lat1 += extra
		else:
			target_lon_span = lat_span * desired
			extra = (target_lon_span - lon_span) / 2
			lon0 -= extra
			lon1 += extra

		bbox = [
			max(lon0, -180),
			min(lon1, 180),
			max(lat0, -90),
			min(lat1, 90),
		]

		ax.set_extent(bbox, crs=ccrs.PlateCarree())
		ax.set_axis_off()

		span = max(bbox[1] - bbox[0], bbox[3] - bbox[2])
		if span <= 15:
			ne_scale = "10m"
		elif span <= 60:
			ne_scale = "50m"
		else:
			ne_scale = "110m"

		ax.add_feature(cfeature.OCEAN.with_scale(ne_scale), facecolor="#eef2f5", zorder=0)
		ax.add_feature(cfeature.LAND.with_scale(ne_scale), facecolor="#f8f9fa", zorder=0)

		ax.add_feature(cfeature.COASTLINE.with_scale(ne_scale),
		               linewidth=0.6, edgecolor="#6c757d", zorder=1)
		ax.add_feature(cfeature.BORDERS.with_scale(ne_scale),
		               linewidth=0.6, edgecolor="#adb5bd", zorder=1)

		city_texts: list = []
		fixed_texts: list = []

		try:
			fname = shpreader.natural_earth(
				resolution=ne_scale, category="cultural", name="populated_places"
			)
			reader = shpreader.Reader(fname)

			min_pop = 150_000  # cities with 150k+ population
			candidates: list[tuple[int, str, float, float]] = []
			for rec in reader.records():
				lon_c, lat_c = rec.geometry.x, rec.geometry.y
				if not (bbox[0] <= lon_c <= bbox[1] and bbox[2] <= lat_c <= bbox[3]):
					continue
				pop = rec.attributes.get("POP_MAX", 0) or 0
				if pop < min_pop:
					continue
				candidates.append((int(pop), rec.attributes.get("NAME", ""), float(lon_c), float(lat_c)))

			candidates.sort(key=lambda x: x[0], reverse=True)
			area = (bbox[1] - bbox[0]) * (bbox[3] - bbox[2])
			if area < 150:
				max_labels = 35
			elif area < 400:
				max_labels = 55
			elif area < 900:
				max_labels = 80
			else:
				max_labels = 110

			for pop, name, lon_c, lat_c in candidates[:max_labels]:
				ax.plot(
					lon_c,
					lat_c,
					marker="o",
					markersize=2.6,
					color="#495057",
					transform=ccrs.PlateCarree(),
					zorder=3,
				)
				txt = ax.text(
					lon_c + 0.08,
					lat_c + 0.08,
					name,
					fontsize=7,
					fontfamily=_ROBOTO_FAMILY,
					color="#495057",
					transform=ccrs.PlateCarree(),
					zorder=3,
					clip_on=False,
					path_effects=[pe.withStroke(linewidth=2, foreground="white")],
				)
				city_texts.append(txt)
		except Exception as e:
			_log.debug(f"Could not load cities: {e}")

		for i in range(len(valid_points) - 1):
			p1 = valid_points[i]
			p2 = valid_points[i + 1]

			is_interpolated = p1.get("segment_to_next") == SEGMENT_DASHED

			linestyle = "--" if is_interpolated else "-"
			linecolor = "#9ca3af" if is_interpolated else JUSTUP_GREEN

			ax.plot(
				[p1["lon"], p2["lon"]],
				[p1["lat"], p2["lat"]],
				transform=ccrs.PlateCarree(),
				color=linecolor,
				linewidth=2.8 if not is_interpolated else 2.0,
				linestyle=linestyle,
				solid_capstyle="round",
				zorder=6,
			)

		try:
			# Find longest non-interpolated segment for arrow placement
			best_segment = None
			best_length = 0
			for i in range(len(valid_points) - 1):
				p1 = valid_points[i]
				p2 = valid_points[i + 1]
				if p1.get("segment_to_next") != SEGMENT_DASHED:
					# Calculate segment length (approximate, for comparison only)
					seg_len = ((p2["lon"] - p1["lon"])**2 + (p2["lat"] - p1["lat"])**2)**0.5
					if seg_len > best_length:
						best_length = seg_len
						best_segment = (p1, p2)
			
			if best_segment is None:
				best_segment = (valid_points[0], valid_points[-1])
			
			arrow_p1, arrow_p2 = best_segment
			
			# Place arrow in the middle of the segment (40% to 60%)
			start_lon = arrow_p1["lon"] + (arrow_p2["lon"] - arrow_p1["lon"]) * 0.40
			start_lat = arrow_p1["lat"] + (arrow_p2["lat"] - arrow_p1["lat"]) * 0.40
			end_lon = arrow_p1["lon"] + (arrow_p2["lon"] - arrow_p1["lon"]) * 0.60
			end_lat = arrow_p1["lat"] + (arrow_p2["lat"] - arrow_p1["lat"]) * 0.60

			arrow = FancyArrowPatch(
				(start_lon, start_lat),
				(end_lon, end_lat),
				arrowstyle="-|>",
				mutation_scale=14,
				linewidth=2.8,
				color=JUSTUP_GREEN,
				transform=ccrs.PlateCarree(),
				zorder=8,
			)
			arrow.set_path_effects([
				pe.Stroke(linewidth=5, foreground="white"),
				pe.Normal(),
			])
			ax.add_patch(arrow)
		except Exception:
			pass

		def label_offset(lon: float, lat: float, extent: list[float]):
			mid_lon = (extent[0] + extent[1]) / 2
			mid_lat = (extent[2] + extent[3]) / 2
			dx = 0.12 if lon < mid_lon else -0.12
			dy = 0.12 if lat < mid_lat else -0.12
			return dx, dy

		for i, p in enumerate(valid_points):
			if p.get("_interpolated"):
				continue

			if i == 0:
				ax.scatter(
					p["lon"], p["lat"],
					s=340,
					marker=pin_marker,
					color=JUSTUP_BLUE,
					edgecolors="white",
					linewidth=2,
					zorder=10,
					transform=ccrs.PlateCarree(),
				)
				ax.scatter(
					p["lon"], p["lat"],
					s=42,
					marker="o",
					color="white",
					edgecolors="none",
					zorder=11,
					transform=ccrs.PlateCarree(),
				)
				dx, dy = label_offset(p["lon"], p["lat"], bbox)
				city = p.get('city') or ''
				country = p.get('country') or ''
				label = f"{city} ({country})" if city and country else city or country or ''
				txt = ax.text(
					p["lon"] + dx, p["lat"] + dy,
					label,
					fontsize=10,
					fontfamily=_ROBOTO_FAMILY,
					fontweight="bold",
					color=JUSTUP_BLUE,
					ha="left",
					va="bottom",
					transform=ccrs.PlateCarree(),
					zorder=11,
					clip_on=False,
					path_effects=[pe.withStroke(linewidth=3, foreground="white")],
				)
				fixed_texts.append(txt)
			elif i == len(valid_points) - 1:
				ax.scatter(
					p["lon"], p["lat"],
					s=340,
					marker=pin_marker,
					color=JUSTUP_RED,
					edgecolors="white",
					linewidth=2,
					zorder=10,
					transform=ccrs.PlateCarree(),
				)
				ax.scatter(
					p["lon"], p["lat"],
					s=42,
					marker="o",
					color="white",
					edgecolors="none",
					zorder=11,
					transform=ccrs.PlateCarree(),
				)
				dx, dy = label_offset(p["lon"], p["lat"], bbox)
				city = p.get('city') or ''
				country = p.get('country') or ''
				label = f"{city} ({country})" if city and country else city or country or ''
				txt = ax.text(
					p["lon"] + dx, p["lat"] + dy,
					label,
					fontsize=10,
					fontfamily=_ROBOTO_FAMILY,
					fontweight="bold",
					color=JUSTUP_RED,
					ha="left",
					va="top",
					transform=ccrs.PlateCarree(),
					zorder=11,
					clip_on=False,
					path_effects=[pe.withStroke(linewidth=3, foreground="white")],
				)
				fixed_texts.append(txt)
			else:
				ax.scatter(
					p["lon"], p["lat"],
					s=16,
					color=JUSTUP_GREEN,
					alpha=0.9,
					zorder=7,
					transform=ccrs.PlateCarree(),
				)

		if _HAS_ADJUSTTEXT and city_texts:
			try:
				adjust_text(
					city_texts,
					ax=ax,
					objects=fixed_texts,
					expand_points=(1.8, 2.0),
					expand_text=(1.6, 1.8),
					force_text=0.9,
					force_points=0.6,
					lim=250,
					only_move={"points": "xy", "text": "xy"},
				)
			except Exception:
				pass

		buf = io.BytesIO()
		fig.savefig(
			buf,
			format="png",
			dpi=dpi,
			facecolor="white",
			edgecolor="none",
			bbox_inches=None,
			pad_inches=0,
		)
		plt.close(fig)
		buf.seek(0)
		png_bytes = buf.getvalue()

		# Optional explicit pixel border (kept at 0% for true full-bleed output).
		EXPLICIT_PNG_PAD_FRAC = 0.0
		if _HAS_PIL and EXPLICIT_PNG_PAD_FRAC > 0:
			try:
				im = PILImage.open(io.BytesIO(png_bytes))
				pad_x = max(int(im.width * EXPLICIT_PNG_PAD_FRAC), 1)
				pad_y = max(int(im.height * EXPLICIT_PNG_PAD_FRAC), 1)

				# Use opaque white border for maximum compatibility in PDF rendering.
				out = PILImage.new(
					"RGB",
					(im.width + 2 * pad_x, im.height + 2 * pad_y),
					(255, 255, 255),
				)
				if im.mode != "RGB":
					im = im.convert("RGB")
				out.paste(im, (pad_x, pad_y))

				out_buf = io.BytesIO()
				out.save(out_buf, format="PNG")
				out_buf.seek(0)
				return out_buf.getvalue()
			except Exception:
				pass

		return png_bytes
	except Exception:
		_log.exception("Failed to render traceroute world map")
		try:
			plt.close(fig)
		except Exception:
			pass
		return None
