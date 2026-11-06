#!/usr/bin/env python3
#
# app/services/pdf_report.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server-side PDF report generation with high-quality vector charts."""

from __future__ import annotations

import dataclasses
import io
import itertools
import logging
import math
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from xml.sax.saxutils import escape as _xml_escape

import matplotlib as mpl

mpl.use("Agg")  # Non-interactive backend for server
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import ShapedStr, TTFont, shapeStr

# ReportLab embeds the IBM Plex Sans and Material Icons fonts in each PDF.
from reportlab.platypus import (
    Flowable,
    Image,
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.doctemplate import ActionFlowable
from reportlab.rl_config import defaultPageSize  # noqa: F401 - ensure config is loaded

from app.services import geo_resolver as gr
from app.services.flag_images import load_flags, normalize_country_code
from app.services.pdf_logo import LogoFlowable, draw_logo, logo_aspect
from app.services.pdf_route_map import (
    DESTINATION_COLOR,
    NO_MAP_NOTE,
    RouteMap,
    build_route_map_model,
    draw_destination_diamond,
)
from app.services.provider_lookup import lookup_provider_info
from app.services.route_interpolator import (
    collapse_interpolated_hops,
    ensure_destination_hop,
    interpolate_route,
)
from app.services.route_timeline import (
    build_route_timeline,
    expand_sequence_hops,
    group_timeline_nodes,
    mapped_hop_numbers,
    timeline_measurement,
)
from app.services.traceroute_geo import (
    geolocate_with_hostname_hint,
    location_view,
    route_hops_to_geo_points,
)
from app.services.traceroute_service import (
    NO_IP_MARKERS,
    TRACE_HOP_LIMIT,
    analyze_route,
    trace_sync,
)
from app.utils.availability_rules import MIN_DOWNTIME_SECONDS, measured_outages
from app.utils.cert_target import resolve_cert_target
from app.utils.downtime_view import display_seconds
from app.utils.ssrf import redact_url
from app.utils.status_palette import STATUS_PALETTE
from app.utils.tls_model import Check, ScanStatus, TlsScanResult, Verdict
from app.utils.tls_scanner import scan_tls
from app.utils.uptime_metric import availability_check_enabled, uptime_level
from app.utils.version import BUILD_INFO, VERSION
from app.utils.whatweb_parser import run_whatweb

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.db.tsdb import DowntimePeriod

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _esc(value: Any) -> str:
    """Escape external text for ReportLab Paragraph markup (strips control chars)."""
    text = _CONTROL_CHARS_RE.sub(" ", str(value if value is not None else ""))
    return _xml_escape(text)


_SITE_TITLE_MAX_LEN = 120
# Bidi controls, zero-width and invisible format characters
_INVISIBLE_CHARS_RE = re.compile(r"[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


def _clean_site_title(raw: Any, font_name: str) -> str:
    """Return a printable one-line website title, or "" when there is nothing to show.

    Characters the embedded font cannot draw are dropped instead of rendering as boxes.
    """
    if not isinstance(raw, str):
        return ""
    text = " ".join(_INVISIBLE_CHARS_RE.sub("", raw).split())
    glyphs = getattr(pdfmetrics.getFont(font_name).face, "charToGlyph", None)
    if glyphs is not None:
        text = " ".join("".join(ch for ch in text if ch == " " or ord(ch) in glyphs).split())
    if len(text) > _SITE_TITLE_MAX_LEN:
        text = text[: _SITE_TITLE_MAX_LEN - 1].rstrip() + "…"
    return text


# matplotlib is not thread-safe for parallel plot generation: serialize all PDF generation.
_PDF_LOCK = threading.Lock()

# Constants

FONT_DIR = Path(__file__).parent.parent / "static" / "fonts"
STATIC_DIR = Path(__file__).parent.parent / "static"
_PDF_TLS_SCAN_DEADLINE_SECONDS = 60.0

# Colors matching the justUp theme
COLORS = {
    "primary": colors.HexColor(STATUS_PALETTE["success"].background),
    "dark": colors.HexColor("#212529"),
    "muted": colors.HexColor(STATUS_PALETTE["neutral"].background),
    "neutral": colors.HexColor(STATUS_PALETTE["neutral"].background),
    "neutral_text": colors.HexColor(STATUS_PALETTE["neutral"].text),
    "light": colors.HexColor("#f8f9fa"),
    "white": colors.white,
    "divider": colors.HexColor("#e9ecef"),
    "danger": colors.HexColor(STATUS_PALETTE["danger"].background),
    "warning": colors.HexColor(STATUS_PALETTE["warning"].background),
    "success": colors.HexColor(STATUS_PALETTE["success"].background),
    "info": colors.HexColor(STATUS_PALETTE["info"].background),
    "info_text": colors.HexColor(STATUS_PALETTE["info"].text),
    # Print palette: dark blue structure, green/yellow/red for state
    "navy": colors.HexColor("#12294a"),
    "navy_mid": colors.HexColor("#2b4a7a"),
    "navy_soft": colors.HexColor("#e8eef7"),
    "surface": colors.HexColor("#f4f6fa"),
    "success_soft": colors.HexColor("#e6f4ec"),
    "success_text": colors.HexColor(STATUS_PALETTE["success"].text),
    "warning_soft": colors.HexColor("#fff4d6"),
    "warning_text": colors.HexColor(STATUS_PALETTE["warning"].text),
    "danger_soft": colors.HexColor("#fdeaec"),
    "danger_text": colors.HexColor(STATUS_PALETTE["danger"].text),
    "unknown_soft": colors.HexColor("#e3e7ec"),
}

# Chart colors
CHART_COLORS = {
    "response_time": "#2b5fa8",
    "uptime_up": "#198754",
    "uptime_down": "#dc3545",
    "ping": "#0f7f86",
    "tcp": "#6f42c1",
    "sla": "#c77700",
}

# Color palette for multi-port TCP chart (matches dashboard)
TCP_PORT_COLORS = [
    "#0d6efd",  # primary blue
    "#198754",  # success green
    "#dc3545",  # danger red
    "#fd7e14",  # orange
    "#6f42c1",  # purple
    "#20c997",  # teal
    "#d63384",  # pink
    "#ffc107",  # warning yellow
    "#6c757d",  # secondary gray
    "#0dcaf0",  # info cyan
]

# Spacing Design Tokens (consistent layout rhythm)
SPACE_XS = 1 * mm  # Minimal: dividers, tight padding
SPACE_SM = 2 * mm  # Small: inner cell padding
SPACE_MD = 4 * mm  # Medium: between elements
SPACE_LG = 8 * mm  # Large: section spacing
SPACE_XL = 12 * mm  # Extra large: page breaks, major sections

# Table cell padding presets
TABLE_PADDING_COMPACT = (SPACE_XS, SPACE_SM, SPACE_XS, SPACE_SM)  # top, right, bottom, left
TABLE_PADDING_NORMAL = (SPACE_SM, SPACE_MD, SPACE_SM, SPACE_MD)
TABLE_PADDING_BANNER = (SPACE_MD, SPACE_LG, SPACE_MD, SPACE_LG)

# DPI for all images embedded into PDFs (charts, maps, etc.)
PDF_DPI = 600


# Traceroute route data


def _run_traceroute_with_interpolation(
    host: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Trace through the shared service and interpolate silent segments.

    Returns:
        Tuple of (display_hops, geo_points, has_interpolation)
    """
    # The shared service resolves and traces ONE address; a failure is a controlled empty route.
    outcome = trace_sync(host)
    if not outcome.ok:
        return [], [], False
    target_ip = outcome.target_ip
    hops = outcome.hops

    # Analyze route for ICMP blocking patterns
    route_analysis = analyze_route(hops, target_ip)

    # Get destination geo data for interpolation.
    # Use the same hostname-hint path as the web UI (POP > GeoIP > online), not
    # plain GeoIP: GeoLite2 often has only coarse coordinates for the target, so
    # web and PDF must agree on the destination's location state.
    destination_geo = None
    if target_ip:
        destination_geo = geolocate_with_hostname_hint(target_ip, host)

    # Apply route interpolation for blocked segments
    interpolated_hops = interpolate_route(
        hops,
        destination_geo=destination_geo,
    )

    # Ensure destination is in hop list (synthetic if needed)
    if target_ip and not route_analysis["destination_reached"]:
        # Get final latency from last known hop if available
        final_latency = None
        if route_analysis["last_responding_hop"] > 0:
            for h in hops:
                if h.get("hop") == route_analysis["last_responding_hop"]:
                    final_latency = h.get("rtt_ms")
                    break

        interpolated_hops = ensure_destination_hop(
            interpolated_hops,
            destination_ip=target_ip,
            destination_hostname=host,
            destination_geo=destination_geo,
            final_rtt_hint_ms=final_latency,
        )

    geo_points = route_hops_to_geo_points(
        interpolated_hops,
        destination_ip=target_ip,
        destination_geo=destination_geo,
    )

    geo_by_ip: dict[str, dict] = {}
    for p in geo_points:
        geo_by_ip.setdefault(p.get("ip"), p)
    mapped_hops = mapped_hop_numbers(geo_points)
    view_by_ip: dict[str, dict] = {}

    enriched_hops = []
    for h in interpolated_hops:
        hop_data = dict(h)
        ip = h.get("ip")
        if (
            ip
            and ip not in NO_IP_MARKERS
            and h.get("status") not in ("timeout", "interpolated")
            and not h.get("_interpolated")
        ):
            if ip not in view_by_ip:
                geo = destination_geo if target_ip and ip == target_ip else geo_by_ip.get(ip)
                if not geo:
                    geo = geolocate_with_hostname_hint(ip, h.get("hostname"))
                view_by_ip[ip] = location_view(geo)
            view = view_by_ip[ip]
            hop_data["city"] = view["city"]
            hop_data["country"] = view["country"]
            hop_data["loc_precision"] = view["precision"]
        try:
            hop_data["on_map"] = int(h.get("hop")) in mapped_hops
        except (TypeError, ValueError):
            hop_data["on_map"] = False
        if target_ip and ip == target_ip:
            hop_data["_destination"] = True

        enriched_hops.append(hop_data)

    # Collapse consecutive interpolated hops for cleaner table
    display_hops = collapse_interpolated_hops(enriched_hops)

    has_interpolation = any(h.get("status") == "interpolated" or h.get("_interpolated") for h in interpolated_hops)

    return display_hops, geo_points, has_interpolation


# Data Structures


@dataclass
class ReportData:
    """Container for all data needed to generate the PDF report."""

    target: dict[str, Any]
    from_date: datetime
    to_date: datetime
    http_data: list[dict]
    uptime_data: list[dict]
    ping_data: list[dict]
    tcp_data: list[dict]
    cert_data: list[dict]
    status_code_data: list[dict]
    tcp_port_data: dict[int, list[dict]] | None = None  # Per-port TCP data: {port: [points]}
    display_data: dict[str, list[dict]] | None = None
    status_counts_exact: bool = False
    downtime_periods: list[DowntimePeriod] | None = None


@dataclass
class KPIMetrics:
    """Computed KPI values for the report."""

    uptime_percent: float | None  # None = no availability measurements
    avg_response_ms: float | None  # None = no response measurements
    max_response_ms: float | None
    downtime_minutes: int
    total_checks: int
    up_count: int
    down_count: int
    avg_ping_ms: float | None = None
    response_samples: int = 0
    coverage_pct: float | None = None  # share of the window covered by availability checks


@dataclass
class SLADeviation:
    """SLA target vs actual comparison."""

    metric_name: str
    sla_target: float
    actual_value: float
    deviation: float  # Positive = above target (bad for latency), negative = below (good)
    deviation_pct: float
    is_compliant: bool
    unit: str


@dataclass
class DowntimeEvent:
    """A single downtime period."""

    start: datetime
    end: datetime | None
    duration_minutes: int | None


# Font Registration

_fonts_registered = False
_fonts_lock = threading.Lock()


def _require_shaping(font_name: str) -> None:
    """Fail before PDF generation if the required font cannot be shaped."""
    font = pdfmetrics.getFont(font_name)
    if not font.shapable:
        msg = f"PDF font shaping is unavailable for {font_name}; install uharfbuzz and check the font file"
        raise RuntimeError(msg)


def _shaped_text(text: str, font_name: str, font_size: float) -> ShapedStr:
    _require_shaping(font_name)
    glyphs = pdfmetrics.getFont(font_name).face.charToGlyph
    missing = sorted({char for char in text if ord(char) not in glyphs})
    if missing:
        msg = f"PDF font {font_name} lacks glyphs for: {', '.join(f'U+{ord(char):04X}' for char in missing)}"
        raise ValueError(msg)
    shaped = shapeStr(text, font_name, font_size, force=True)
    if not isinstance(shaped, ShapedStr):
        msg = f"PDF font shaping failed for {font_name}"
        raise RuntimeError(msg)
    return shaped


def _text_width(text: str, font_name: str, font_size: float) -> float:
    if not text:
        return 0.0
    shaped = _shaped_text(text, font_name, font_size)
    return sum(glyph.x_advance for glyph in shaped.__shapeData__) * font_size / 1000


def _draw_text(canvas, x: float, y: float, text: str, *, align: str = "left") -> None:
    if not text:
        return
    font_name = canvas._fontname  # noqa: SLF001
    font_size = canvas._fontsize  # noqa: SLF001
    shaped = _shaped_text(text, font_name, font_size)
    width = sum(glyph.x_advance for glyph in shaped.__shapeData__) * font_size / 1000
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    canvas.drawString(x, y, shaped)


def _paragraph_style(
    name: str, *, font_name: str | None = None, parent: ParagraphStyle | None = None, **kwargs
) -> ParagraphStyle:
    font_name = font_name or (parent.fontName if parent else None)
    if font_name is None:
        msg = f"PDF paragraph style {name} has no font"
        raise RuntimeError(msg)
    _require_shaping(font_name)
    return ParagraphStyle(name, parent=parent, fontName=font_name, shaping=1, **kwargs)


def _table_style(commands: list[tuple]) -> TableStyle:
    return TableStyle([("SHAPING", (0, 0), (-1, -1), 1), *commands])


def _split_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if _text_width(candidate, font_name, font_size) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        for char in word:
            if current and _text_width(current + char, font_name, font_size) > max_width:
                lines.append(current)
                current = ""
            current += char
    if current:
        lines.append(current)
    return lines


def _register_fonts() -> None:
    """Register IBM Plex Sans fonts with ReportLab (thread-safe)."""
    global _fonts_registered
    if _fonts_registered:
        return

    with _fonts_lock:
        # Double-check after acquiring lock
        if _fonts_registered:
            return

        fonts = {
            "IBMPlexSans": "IBMPlexSans-Regular.ttf",
            "IBMPlexSans-Bold": "IBMPlexSans-Bold.ttf",
            "IBMPlexSans-Italic": "IBMPlexSans-Italic.ttf",
            "IBMPlexSans-BoldItalic": "IBMPlexSans-BoldItalic.ttf",
            "MaterialIcons": "MaterialIcons-Regular.ttf",
        }
        for name, filename in fonts.items():
            path = FONT_DIR / filename
            if not path.is_file():
                raise FileNotFoundError(f"Required PDF font is missing: {path}")
            font = TTFont(name, str(path))
            pdfmetrics.registerFont(font)
            if name != "MaterialIcons":
                _require_shaping(name)

        pdfmetrics.registerFontFamily(
            "IBMPlexSans",
            normal="IBMPlexSans",
            bold="IBMPlexSans-Bold",
            italic="IBMPlexSans-Italic",
            boldItalic="IBMPlexSans-BoldItalic",
        )
        _fonts_registered = True


def _get_font() -> str:
    """Return the regular IBM Plex Sans font name."""
    _register_fonts()
    return "IBMPlexSans"


def _get_font_bold() -> str:
    """Return the bold IBM Plex Sans font name."""
    _register_fonts()
    return "IBMPlexSans-Bold"


# Matplotlib Chart Generation

# Guard to prevent repeated matplotlib style setup (global state)
_mpl_initialized = False
_mpl_init_lock = threading.Lock()


def _setup_matplotlib_style() -> None:
    """
    Configure matplotlib for consistent, high-quality charts with IBM Plex Sans.

    Explicitly registers the local IBM Plex Sans TTF files with matplotlib's font
    manager to ensure chart labels match the rest of the PDF.

    Only runs once to avoid repeated global state modifications.
    """
    global _mpl_initialized
    if _mpl_initialized:
        return

    with _mpl_init_lock:
        if _mpl_initialized:
            return

        _register_fonts()

        from matplotlib import font_manager

        font_manager.fontManager.addfont(str(FONT_DIR / "IBMPlexSans-Regular.ttf"))
        font_manager.fontManager.addfont(str(FONT_DIR / "IBMPlexSans-Bold.ttf"))

        # Base style, with a fallback for matplotlib versions that renamed it.
        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except OSError:
            _log.debug("seaborn-v0_8-whitegrid not available, using default style")
            plt.style.use("default")

        # The style resets rcParams, so set them (incl. IBM Plex Sans) afterwards.
        font_config = {
            "font.family": "sans-serif",
            "font.sans-serif": ["IBM Plex Sans"],
            "font.weight": "regular",
            "mathtext.default": "regular",
        }

        plt.rcParams.update(
            {
                **font_config,
                "font.size": 9,
                "axes.titlesize": 11,
                "axes.labelsize": 9,
                "xtick.labelsize": 8,
                "ytick.labelsize": 8,
                "legend.fontsize": 11,
                "figure.dpi": PDF_DPI,
                "savefig.dpi": PDF_DPI,
                "axes.linewidth": 0.5,
                "grid.linewidth": 0.3,
                "lines.linewidth": 1.5,
                "axes.facecolor": "#ffffff",
                "figure.facecolor": "#ffffff",
                "axes.edgecolor": "#e9ecef",
                "grid.color": "#e9ecef",
            }
        )

        _mpl_initialized = True
        _log.debug("matplotlib configured with font.sans-serif=%s", plt.rcParams["font.sans-serif"])


@contextmanager
def _safe_figure(*args, **kwargs):
    """Context manager ensuring matplotlib figures are closed on exception.

    Prevents memory leaks from unclosed figures when chart rendering fails.
    """
    fig, ax = plt.subplots(*args, **kwargs)
    try:
        yield fig, ax
    finally:
        plt.close(fig)


# Shadow layer constants for soft shadow effect (CSS-like box-shadow blur)
_SHADOW_LAYERS = [(2.0, 0.015), (1.5, 0.03), (1.0, 0.05), (0.6, 0.07)]


def _draw_soft_shadow(canvas, w: float, h: float, radius: float) -> None:
    """Draw a soft shadow using multiple semi-transparent layers."""
    for offset, alpha in _SHADOW_LAYERS:
        canvas.setFillColor(colors.Color(0, 0, 0, alpha=alpha))
        canvas.roundRect(offset, -offset, w, h, radius, fill=1, stroke=0)


def _report_tz(from_date: datetime) -> tzinfo:
    """Time zone used for every timestamp in the report (that of the report start)."""
    return from_date.tzinfo or UTC


def _tz_label(from_date: datetime) -> str:
    return from_date.tzname() or "UTC"


def _to_tz(value: datetime, tz: tzinfo) -> datetime:
    """Convert a datetime to the report time zone; naive values are treated as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz)


def _num(raw: Any) -> float | None:
    """Return a finite float, or None for missing, non-numeric and NaN/inf values."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_series(data: list[dict], tz: tzinfo) -> tuple[list[datetime], list[float]]:
    """Parse {ts, value} points sorted by time; a missing value becomes NaN, never 0."""
    pairs: list[tuple[datetime, float]] = []
    for p in data:
        number = _num(p.get("value"))
        value = math.nan if number is None else number
        pairs.append((_to_tz(datetime.fromisoformat(p["ts"]), tz), value))
    pairs.sort(key=lambda item: item[0])
    return [t for t, _ in pairs], [v for _, v in pairs]


def _median_step(ts: list[datetime]) -> timedelta | None:
    diffs = sorted(b - a for a, b in itertools.pairwise(ts) if b > a)
    return diffs[len(diffs) // 2] if diffs else None


def _gap_threshold(ts: list[datetime]) -> timedelta | None:
    """Spacing above which two samples are not connected (3x the median spacing)."""
    step = _median_step(ts)
    return step * 3 if step else None


def _break_gaps(ts: list[datetime], vals: list[float]) -> tuple[list[datetime], list[float]]:
    """Insert NaN between samples that are further apart than the gap threshold."""
    limit = _gap_threshold(ts)
    if not limit:
        return ts, vals
    out_t, out_v = [ts[0]], [vals[0]]
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] > limit:
            out_t.append(ts[i - 1] + (ts[i] - ts[i - 1]) / 2)
            out_v.append(math.nan)
        out_t.append(ts[i])
        out_v.append(vals[i])
    return out_t, out_v


def _plot_series(
    ax, ts: list[datetime], vals: list[float], color: str, *, ymax: float | None = None, label: str = "Measured"
) -> Any:
    """Plot a measured series: gaps stay open, isolated samples get a marker, values above ymax sit on the edge."""
    bt, bv = _break_gaps(ts, vals)
    if ymax is not None:
        bv = [min(v, ymax) for v in bv]
    ax.fill_between(bt, bv, alpha=0.14, color=color, linewidth=0)
    (line,) = ax.plot(bt, bv, color=color, linewidth=1.1, label=label)
    iso = [
        i
        for i in range(len(bt))
        if not math.isnan(bv[i]) and (i == 0 or math.isnan(bv[i - 1])) and (i == len(bt) - 1 or math.isnan(bv[i + 1]))
    ]
    if iso:
        ax.plot([bt[i] for i in iso], [bv[i] for i in iso], linestyle="none", marker="o", markersize=2.5, color=color)
    return line


def _style_axes(ax, *, from_date: datetime, to_date: datetime) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, axis="y", alpha=0.6)
    ax.grid(False, axis="x")
    ax.tick_params(axis="both", length=2.5, width=0.5, labelsize=8, colors="#495057")
    _format_time_axis(ax, from_date=from_date, to_date=to_date)


# Latency scale and peaks

PEAK_COLOR = "#b02a5b"  # solid interval bars; the SLA line stays orange and dashed
_MAX_PEAK_MARKS = 6
_PEAK_LABEL = "Peak within interval (bucket maximum)"
_ABOVE_LABEL = "Above scale (value labelled)"
# An SLA line widens the Y axis only when it is within this factor of the plotted data; a distant
# threshold is marked at the top edge instead of compressing the normal latencies.
_SLA_SCALE_FACTOR = 2.5


@dataclass(frozen=True)
class _Peak:
    """Highest check inside one display interval; the exact moment inside it is unknown."""

    start: datetime
    end: datetime
    value: float
    median: float


@dataclass
class _LatencyView:
    ts: list[datetime]
    vals: list[float]
    peaks: list[_Peak]  # notable peaks, highest first
    highest_max: float | None  # highest interval maximum, notable or not
    intervals: int
    no_max_intervals: int  # intervals without a maximum (rolled-up history, plain samples)
    has_intervals: bool
    cap: float  # values above this never stretch the Y axis


def _percentile(sorted_values: list[float], q: float) -> float:
    return sorted_values[int((len(sorted_values) - 1) * q)]


def _build_view(data: list[dict], tz: tzinfo) -> _LatencyView:
    """Parse display points; select peaks by prominence instead of a fixed ratio to the median."""
    ts, vals = _parse_series(data, tz)
    valid = sorted(v for v in vals if not math.isnan(v))
    typical = _percentile(valid, 0.95) if valid else 1.0
    cap = max(typical * 3, 1.0)

    candidates: list[_Peak] = []
    maxes: list[float] = []
    no_max = 0
    for p in data:
        peak, median = _num(p.get("max")), _num(p.get("value"))
        end_raw = p.get("bucket_end")
        if peak is None or median is None or not end_raw:
            no_max += 1
            continue
        maxes.append(peak)
        if peak > median:
            start = _to_tz(datetime.fromisoformat(p["ts"]), tz)
            candidates.append(_Peak(start, _to_tz(datetime.fromisoformat(end_raw), tz), peak, median))

    notable: list[_Peak] = []
    if candidates:
        ordered = sorted(maxes)
        q1, q3 = _percentile(ordered, 0.25), _percentile(ordered, 0.75)
        fence = max(q3 + 1.5 * (q3 - q1), 1.5 * _percentile(valid, 0.5))
        notable = sorted(
            (c for c in candidates if c.value > fence and c.value >= c.median * 1.5 and c.value - c.median >= 1.0),
            key=lambda c: c.value,
            reverse=True,
        )
    return _LatencyView(
        ts=ts,
        vals=vals,
        peaks=notable,
        highest_max=max(maxes) if maxes else None,
        intervals=len(data),
        no_max_intervals=no_max,
        has_intervals=bool(maxes),
        cap=cap,
    )


def _nice_ticks(top: float, target: int = 5) -> list[float]:
    """Ticks 0..top on a 1/2/5 (or 2.5 above 10) step, so labels stay short integers where possible."""
    top = max(top, 1.0)
    raw = top / target
    mag = 10 ** math.floor(math.log10(raw))
    steps = [m * mag for m in (1, 2, 2.5, 5, 10) if m != 2.5 or mag >= 10]
    step = next(s for s in steps if s >= raw)
    count = math.ceil(top / step - 1e-9)
    return [round(i * step, 6) for i in range(count + 1)]


def _axis_scale(views: list[_LatencyView], sla_ms: float | None = None, target: int = 5) -> tuple[float, list[float]]:
    """Linear Y range for the normal latencies; extreme values are handled as overflow, not by a log scale."""
    visible = [
        v
        for view in views
        for v in (*(x for x in view.vals if not math.isnan(x)), *(p.value for p in view.peaks))
        if v <= view.cap
    ]
    top = max(visible, default=1.0)
    if sla_ms and sla_ms <= top * _SLA_SCALE_FACTOR:
        top = max(top, float(sla_ms))
    ticks = _nice_ticks(top * 1.08, target)
    return ticks[-1], ticks


def _fmt_tick(step: float):
    return (lambda v, _pos: f"{v:,.0f}") if step >= 1 else (lambda v, _pos: f"{v:g}")


def _ports_share_scale(views: list[_LatencyView]) -> bool:
    """One common Y range when the ports are comparable; otherwise a flat port would be unreadable."""
    tops = [_axis_scale([v])[0] for v in views]
    return max(tops) <= 8 * min(tops)


def _fmt_ms_value(value: float) -> str:
    return f"{value:,.0f} ms" if value >= 10 else f"{value:,.1f} ms"


def _fmt_interval(start: datetime, end: datetime) -> str:
    same_day = start.date() == end.date()
    return f"{start:%m-%d %H:%M}–{end:%H:%M}" if same_day else f"{start:%m-%d %H:%M}–{end:%m-%d %H:%M}"


def _peak_text(peak: _Peak, tz: tzinfo) -> str:
    return f"{_fmt_ms_value(peak.value)} ({_fmt_interval(peak.start.astimezone(tz), peak.end.astimezone(tz))})"


def _overflow_values(view: _LatencyView, top: float) -> list[float]:
    medians = [v for v in view.vals if not math.isnan(v) and v > top]
    return medians + [p.value for p in view.peaks if p.value > top]


def _draw_latency(
    ax,
    view: _LatencyView,
    color: str,
    *,
    top: float,
    from_date: datetime,
    to_date: datetime,
    max_labels: int = 3,
) -> list[Any]:
    """Draw median line, peak bars and above-scale markers on a prepared linear axis; returns legend handles."""
    span = to_date - from_date
    label = "Median per interval" if view.has_intervals else "Measured"
    handles = [_plot_series(ax, view.ts, view.vals, color, ymax=top, label=label)]
    marks: list[tuple[datetime, float, float]] = []  # x, drawn y, real value
    for p in view.peaks[:_MAX_PEAK_MARKS]:
        centre = p.start + (p.end - p.start) / 2
        y = min(p.value, top)
        # The bar spans only the real interval; a thin stem and a point marker keep it visible without
        # implying a longer event than the aggregation interval.
        ax.hlines(y, p.start, p.end, color=PEAK_COLOR, linewidth=1.6, zorder=4)
        ax.vlines(centre, min(p.median, y), y, color=PEAK_COLOR, linewidth=0.9, zorder=4)
        ax.plot([centre], [y], linestyle="none", marker="o", markersize=2.8, color=PEAK_COLOR, zorder=4)
        marks.append((centre, y, p.value))
    if view.peaks:
        handles.append(Line2D([], [], color=PEAK_COLOR, linewidth=0.9, marker="o", markersize=2.8, label=_PEAK_LABEL))
    median_over = [(t, top, v) for t, v in zip(view.ts, view.vals, strict=True) if not math.isnan(v) and v > top]
    over_marks = median_over + [m for m in marks if m[2] > top]
    if over_marks:
        ax.plot(
            [m[0] for m in over_marks],
            [top] * len(over_marks),
            linestyle="none",
            marker="^",
            markersize=4,
            color="#343a40",
            zorder=5,
            clip_on=False,
        )
        handles.append(Line2D([], [], linestyle="none", marker="^", markersize=4, color="#343a40", label=_ABOVE_LABEL))
    # Label the highest marks first and skip any that would sit on an already labelled one.
    labelled: list[datetime] = []
    for x, y, real in sorted([*marks, *median_over], key=lambda m: -m[2]):
        if len(labelled) >= max_labels or any(abs(x - other) < span * 0.12 for other in labelled):
            continue
        labelled.append(x)
        frac = (x - from_date) / span if span else 0.5
        high = y >= top * 0.88
        ax.annotate(
            _fmt_ms_value(real),
            (x, y),
            xytext=(-4 if frac > 0.5 else 4, -4 if high else 4),
            textcoords="offset points",
            ha="right" if frac > 0.5 else "left",  # beside the peak stem, never across it
            va="top" if high else "bottom",
            fontsize=7,
            color=PEAK_COLOR if real > view.cap else "#343a40",
            annotation_clip=False,
        )
    return handles


def _y_axis(ax, ticks: list[float], top: float, *, label: str | None = None) -> None:
    ax.set_ylim(0, top)
    ax.set_yticks(ticks)
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(_fmt_tick(step)))
    if label:
        ax.set_ylabel(label, fontsize=8, color="#495057")


def _left_margin(ticks: list[float], width: float, has_label: bool = True) -> float:
    """Figure fraction for the Y tick labels and optional side title, derived from the widest tick label."""
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    chars = max(len(_fmt_tick(step)(t, 0)) for t in ticks)
    return min(0.25, (0.14 + 0.075 * chars + (0.22 if has_label else 0)) / width)


def _latency_plot_left_in(data: list[dict], from_date: datetime, sla_threshold_ms: int | None, width: float) -> float:
    """Inches a latency chart reserves left of its plot area for the Y tick labels (0 without data)."""
    tz = _report_tz(from_date)
    if not data or all(math.isnan(v) for v in _parse_series(data, tz)[1]):
        return 0.0
    ticks = _axis_scale([_build_view(data, tz)], sla_threshold_ms)[1]
    return _left_margin(ticks, width, has_label=False) * width


def _keep_tick_labels_inside(fig) -> None:
    """Axes run edge to edge; a tick label at the border is aligned inward instead of being clipped."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    width = fig.bbox.width
    for ax in fig.axes:
        for label in ax.get_xticklabels():
            if not label.get_visible() or not label.get_text():
                continue
            box = label.get_window_extent(renderer)
            if box.x0 < 0:
                label.set_ha("left")
            elif box.x1 > width:
                label.set_ha("right")


def _fig_png(fig) -> bytes:
    _keep_tick_labels_inside(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=PDF_DPI, facecolor="white", edgecolor="none", bbox_inches=None, pad_inches=0)
    return buf.getvalue()


def _create_line_chart(
    data: list[dict],
    *,
    from_date: datetime,
    to_date: datetime,
    color: str,
    ylabel: str,
    width: float,
    height: float,
    sla_threshold_ms: int | None = None,
    threshold_label: str | None = None,
    legend_below: bool = False,
    plot_left_in: float | None = None,
) -> bytes | None:
    """Create a latency time-series chart; measurements and the SLA threshold are drawn distinctly.

    plot_left_in fixes the plot's left edge so the chapter's time axes line up.
    """
    if not data:
        return None
    tz = _report_tz(from_date)
    vals = _parse_series(data, tz)[1]
    valid = [v for v in vals if not math.isnan(v)]
    if not valid:
        return None

    _setup_matplotlib_style()
    view = _build_view(data, tz)
    top, ticks = _axis_scale([view], sla_threshold_ms)
    with _safe_figure(figsize=(width, height)) as (fig, ax):
        handles = _draw_latency(ax, view, color, top=top, from_date=from_date, to_date=to_date)
        if sla_threshold_ms:
            sla_label = threshold_label or f"SLA threshold ({sla_threshold_ms} ms)"
            if sla_threshold_ms <= top:
                handles.append(
                    ax.axhline(
                        sla_threshold_ms,
                        linestyle=(0, (5, 3)),
                        linewidth=1.1,
                        color=CHART_COLORS["sla"],
                        label=sla_label,
                    )
                )
            else:  # far above the data: keep the scale readable, mark the edge and say so in the legend
                handles.append(
                    Line2D([], [], linestyle=(0, (5, 3)), color=CHART_COLORS["sla"], label=f"{sla_label} – above scale")
                )
                ax.plot(
                    [1.0],
                    [1.0],
                    transform=ax.transAxes,
                    linestyle="none",
                    marker="^",
                    markersize=4,
                    color=CHART_COLORS["sla"],
                    clip_on=False,
                    zorder=5,
                )
                ax.annotate(
                    f"SLA {sla_threshold_ms:,} ms (above scale)",
                    xy=(1.0, 1.0),
                    xycoords="axes fraction",
                    xytext=(-7, -2),
                    textcoords="offset points",
                    ha="right",
                    va="top",
                    fontsize=7,
                    color="#8a5200",
                    annotation_clip=False,
                )
        _y_axis(ax, ticks, top, label=ylabel)
        ax.yaxis.label.set_rotation(0)
        ax.yaxis.label.set_horizontalalignment("left")
        ax.yaxis.label.set_verticalalignment("bottom")
        _style_axes(ax, from_date=from_date, to_date=to_date)
        legend_position = (
            {"loc": "upper center", "bbox_to_anchor": (0.5, -0.26)}
            if legend_below
            else {
                "loc": "lower right",
                "bbox_to_anchor": (1.0, 1.0),
            }
        )
        ax.legend(
            handles=handles,
            **legend_position,
            ncol=len(handles) if len(handles) <= 3 else 2,
            frameon=False,
            fontsize=8,
            borderaxespad=0.2,
        )
        top_edge = 0.98 if legend_below else 0.88 if len(handles) <= 3 else 0.82
        left = _left_margin(ticks, width, has_label=False) if plot_left_in is None else plot_left_in / width
        fig.subplots_adjust(left=left, right=1.0, top=top_edge, bottom=0.31)
        # Put the unit above the plot so the axis needs space only for its tick labels.
        ax.yaxis.set_label_coords(0.0, 1.02)
        return _fig_png(fig)


def _create_response_time_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float = 2.5,
    sla_threshold_ms: int | None = None,
    threshold_label: str | None = None,
    plot_left_in: float | None = None,
) -> bytes | None:
    """Create a response time chart as PNG bytes."""
    return _create_line_chart(
        data,
        from_date=from_date,
        to_date=to_date,
        color=CHART_COLORS["response_time"],
        ylabel="Response time (ms)",
        width=width,
        height=height,
        sla_threshold_ms=sla_threshold_ms,
        threshold_label=threshold_label,
        legend_below=True,
        plot_left_in=plot_left_in,
    )


def _create_ping_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float = 2.5,
    sla_threshold_ms: int | None = None,
    threshold_label: str | None = None,
    plot_left_in: float | None = None,
) -> bytes | None:
    """Create a ping latency chart as PNG bytes."""
    return _create_line_chart(
        data,
        from_date=from_date,
        to_date=to_date,
        color=CHART_COLORS["ping"],
        ylabel="Ping latency (ms)",
        width=width,
        height=height,
        sla_threshold_ms=sla_threshold_ms,
        threshold_label=threshold_label,
        plot_left_in=plot_left_in,
    )


def _create_tcp_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float = 2.5,
    plot_left_in: float | None = None,
) -> bytes | None:
    """Create a TCP latency chart as PNG bytes."""
    return _create_line_chart(
        data,
        from_date=from_date,
        to_date=to_date,
        color=CHART_COLORS["tcp"],
        ylabel="TCP latency (ms)",
        width=width,
        height=height,
        plot_left_in=plot_left_in,
    )


def _create_uptime_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float = 1.45,
    plot_left_in: float = 0.0,
) -> bytes | None:
    """Create an availability status strip as PNG bytes.

    Up and Down intervals are drawn as blocks; periods without samples stay empty.
    plot_left_in leaves the Y-axis gutter of the latency charts empty so the time axes align.
    """
    tz = _report_tz(from_date)
    points = [(t, v) for t, v in zip(*_parse_series(data, tz), strict=True) if not math.isnan(v)] if data else []
    if not points:
        return None

    _setup_matplotlib_style()
    ts = [t for t, _ in points]
    step = _median_step(ts) or timedelta(minutes=1)
    limit = step * 3
    span = to_date - from_date
    min_width = span * 0.004  # keep short outages visible in print
    blocks: dict[str, list[tuple[float, float]]] = {"up": [], "down": []}
    for i, (t, v) in enumerate(points):
        nxt = ts[i + 1] if i + 1 < len(ts) else None
        end = nxt if nxt is not None and nxt - t <= limit else t + step
        end = min(end, to_date)
        start = max(t, from_date)
        if end <= start:
            continue
        key = "up" if v == 1 else "down"
        if key == "down" and end - start < min_width:
            end = start + min_width
        blocks[key].append((mdates.date2num(start), mdates.date2num(end) - mdates.date2num(start)))

    with _safe_figure(figsize=(width, height)) as (fig, ax):
        ax.broken_barh(blocks["up"], (0, 1), facecolors=CHART_COLORS["uptime_up"], linewidth=0)
        ax.broken_barh(blocks["down"], (0, 1), facecolors=CHART_COLORS["uptime_down"], linewidth=0)
        ax.set_ylim(0, 1)
        ax.set_yticks([])
        _style_axes(ax, from_date=from_date, to_date=to_date)
        ax.spines["left"].set_visible(False)
        ax.grid(False)
        ax.legend(
            handles=[
                Patch(facecolor=CHART_COLORS["uptime_up"], label="Up"),
                Patch(facecolor=CHART_COLORS["uptime_down"], label="Down"),
                Patch(facecolor="#ffffff", edgecolor="#adb5bd", linewidth=0.6, label="No measurement"),
            ],
            loc="lower right",
            bbox_to_anchor=(1.0, 1.0),
            ncol=3,
            frameon=False,
            fontsize=8,
            borderaxespad=0.2,
        )
        fig.subplots_adjust(left=plot_left_in / width, right=1.0, top=0.80, bottom=0.42)
        return _fig_png(fig)


def _coverage_blocks(
    stamps: list[datetime], interval: timedelta, from_date: datetime, to_date: datetime
) -> list[tuple[float, float]]:
    """Observed spans: each check covers one configured interval, so the sum matches coverage_pct."""
    blocks: list[tuple[datetime, datetime]] = []
    for t in sorted(stamps):
        end = min(t + interval, to_date)
        if blocks and t <= blocks[-1][1]:
            blocks[-1] = (blocks[-1][0], max(blocks[-1][1], end))
        elif end > t:
            blocks.append((t, end))
    return [(mdates.date2num(a), mdates.date2num(b) - mdates.date2num(a)) for a, b in blocks if b > from_date]


def _create_coverage_bar(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float = 0.7,
    interval_seconds: float = 60,
    plot_left_in: float = 0.0,
) -> bytes | None:
    """Compact bar over the whole reporting period: where checks exist and where the period is empty."""
    tz = _report_tz(from_date)
    ts, vals = _parse_series(data, tz)
    stamps = [t for t, v in zip(ts, vals, strict=True) if not math.isnan(v)]
    if not stamps:
        return None
    _setup_matplotlib_style()
    blocks = _coverage_blocks(stamps, timedelta(seconds=interval_seconds), from_date, to_date)
    with _safe_figure(figsize=(width, height)) as (fig, ax):
        ax.broken_barh(
            [(mdates.date2num(from_date), mdates.date2num(to_date) - mdates.date2num(from_date))],
            (0, 1),
            facecolors="#ffffff",
            edgecolor="#adb5bd",
            hatch="////",
            linewidth=0.6,
        )
        ax.broken_barh(
            blocks,
            (0, 1),
            facecolors=CHART_COLORS["response_time"],
            linewidth=0.3,
            edgecolor=CHART_COLORS["response_time"],
        )
        ax.set_ylim(0, 1)
        ax.set_yticks([])
        _style_axes(ax, from_date=from_date, to_date=to_date)
        ax.spines["left"].set_visible(False)
        ax.set_xlabel("")  # the period is stated in the chapter subtitle
        ax.grid(False)
        ax.legend(
            handles=[
                Patch(facecolor=CHART_COLORS["response_time"], label="Checks recorded"),
                Patch(facecolor="#ffffff", edgecolor="#adb5bd", hatch="////", linewidth=0.6, label="No measurement"),
            ],
            loc="lower right",
            bbox_to_anchor=(1.0, 1.0),
            ncol=2,
            frameon=False,
            fontsize=8,
            borderaxespad=0.2,
        )
        fig.subplots_adjust(left=plot_left_in / width, right=1.0, top=0.62, bottom=0.42)
        return _fig_png(fig)


def _port_scales(port_data: dict[int, list[dict]], tz) -> tuple[dict, dict, list[float]]:
    """Per-port views and Y scales plus the tick list with the widest labels."""
    views = {port: _build_view(data, tz) for port, data in sorted(port_data.items())}
    shared = _ports_share_scale(list(views.values()))
    scales = {port: _axis_scale(list(views.values()) if shared else [views[port]], target=3) for port in views}
    widest = max(
        (s[1] for s in scales.values()), key=lambda t: len(_fmt_tick(1 if len(t) < 2 else t[1] - t[0])(t[-1], 0))
    )
    return views, scales, widest


def _create_multi_port_tcp_chart(
    port_data: dict[int, list[dict]],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float | None = None,  # Auto-calculated based on number of ports
    plot_left_in: float | None = None,
) -> bytes | None:
    """Create a small-multiples TCP latency chart with one panel per port.

    All panels share the time axis and the Y scale, so ports are directly comparable.
    """
    port_data = {port: data for port, data in (port_data or {}).items() if data}
    if not port_data:
        return None

    tz = _report_tz(from_date)
    series = {port: _parse_series(data, tz) for port, data in sorted(port_data.items())}
    if not any(not math.isnan(v) for _, vals in series.values() for v in vals):
        return None

    _setup_matplotlib_style()
    sorted_ports = list(series)
    num_ports = len(sorted_ports)
    if height is None:
        height = min(7.5, max(1.9, num_ports * 1.0 + 0.6))

    views, scales, widest = _port_scales(port_data, tz)

    with _safe_figure(nrows=num_ports, ncols=1, figsize=(width, height), sharex=True, squeeze=False) as (fig, axes):
        flat_axes = axes.flatten()
        handles: dict[str, Any] = {}
        for idx, port in enumerate(sorted_ports):
            ax = flat_axes[idx]
            color = TCP_PORT_COLORS[idx % len(TCP_PORT_COLORS)]
            top, ticks = scales[port]
            for h in _draw_latency(ax, views[port], color, top=top, from_date=from_date, to_date=to_date, max_labels=1):
                handles.setdefault(h.get_label(), h)
            ax.text(
                0.0,
                1.04,
                f"Port {port}",
                transform=ax.transAxes,
                fontsize=8,
                fontweight="bold",
                color=color,
                va="bottom",
            )
            _y_axis(ax, ticks, top)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(True, axis="y", alpha=0.6)
            ax.tick_params(axis="both", length=2.5, width=0.5, labelsize=8, colors="#495057")

        # Median entries differ in colour per port; the legend only explains peaks and overflow markers.
        legend_handles = [h for k, h in handles.items() if k in (_PEAK_LABEL, _ABOVE_LABEL)]
        if legend_handles:
            flat_axes[0].legend(
                handles=legend_handles,
                loc="lower right",
                bbox_to_anchor=(1.0, 1.0),
                ncol=len(legend_handles),
                frameon=False,
                fontsize=8,
                borderaxespad=0.2,
            )
        _format_time_axis(flat_axes[-1], from_date=from_date, to_date=to_date)
        left = _left_margin(widest, width, has_label=False) if plot_left_in is None else plot_left_in / width
        fig.text(0.0, 0.99, "TCP latency (ms)", ha="left", va="top", fontsize=8, color="#495057")
        fig.subplots_adjust(left=left, right=1.0, top=1 - 0.3 / height, bottom=min(0.34, 0.65 / height), hspace=0.6)
        return _fig_png(fig)


def _http_class_label(code: int) -> str:
    labels = {
        0: "No response",
        1: "1xx Informational",
        2: "2xx Success",
        3: "3xx Redirect",
        4: "4xx Client error",
        5: "5xx Server error",
    }
    return labels[code // 100]


_STATUS_CLASS_COLORS = {0: "#7f1d1d", 1: "#0369a1", 2: "#15803d", 3: "#15803d", 4: "#c2410c", 5: "#b91c1c"}


def _status_counts(data: list[dict]) -> dict[int, int]:
    """Count stored samples per HTTP status code; points without a valid code are skipped."""
    counts: dict[int, int] = {}
    for p in data or []:
        number = _num(p.get("value"))
        if number is None:
            continue
        code = int(number)
        if code == 0 or 100 <= code <= 599:
            n = _num(p.get("count"))
            counts[code] = counts.get(code, 0) + (int(n) if n is not None else 1)
    return counts


def _create_status_code_chart(
    data: list[dict], width: float = 6.7, height: float | None = None, *, exact: bool = False
) -> bytes | None:
    """Create an HTTP status code distribution as a labelled donut with a legend table.

    Counts are stored samples (history may be rolled up), not exact per-check counts.
    """
    status_counts = _status_counts(data)
    if not status_counts:
        return None

    _setup_matplotlib_style()
    codes = sorted(status_counts)
    counts = [status_counts[c] for c in codes]
    total = sum(counts)
    if height is None:
        height = max(1.9, 0.5 + 0.2 * len(codes))

    base = _STATUS_CLASS_COLORS
    seen: dict[int, int] = {}
    slice_colors = []
    for code in codes:
        shade = seen.get(code // 100, 0)
        seen[code // 100] = shade + 1
        slice_colors.append(_mix(base[code // 100], 0.28 * min(shade, 3)))

    fig = plt.figure(figsize=(width, height))
    try:
        side = min(height * 0.92, width * 0.36)
        ax = fig.add_axes([0.02, (1 - side / height) / 2, side / width, side / height])
        ax.pie(
            counts,
            colors=slice_colors,
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1.2},
        )
        ax.text(0, 0.08, f"{total:,}", ha="center", va="center", fontsize=13, fontweight="bold", color="#12294a")
        ax.text(0, -0.2, "checks" if exact else "samples", ha="center", va="center", fontsize=8, color="#6c757d")
        ax.axis("equal")

        x0 = side / width + 0.07
        row_h = min(0.2, 0.84 / max(len(codes), 1)) * 1.0
        y = 0.5 + row_h * (len(codes) - 1) / 2 + 0.04
        fig.text(x0, min(y + row_h, 0.97), "Status code", fontsize=8, color="#6c757d")
        fig.text(
            x0 + 0.40, min(y + row_h, 0.97), "Checks" if exact else "Samples", fontsize=8, color="#6c757d", ha="right"
        )
        fig.text(x0 + 0.52, min(y + row_h, 0.97), "Share", fontsize=8, color="#6c757d", ha="right")
        for code, count, color in zip(codes, counts, slice_colors, strict=True):
            fig.patches.append(
                Rectangle((x0, y - 0.012), 0.016, 0.045, transform=fig.transFigure, facecolor=color, edgecolor="none")
            )
            fig.text(
                x0 + 0.03,
                y,
                f"{('HTTP ' + str(code)) if code else 'Error'}  {_http_class_label(code)}",
                fontsize=8.5,
                color="#212529",
                va="baseline",
            )
            fig.text(x0 + 0.40, y, f"{count:,}", fontsize=8.5, color="#212529", ha="right")
            fig.text(x0 + 0.52, y, f"{count / total * 100:.1f} %", fontsize=8.5, color="#212529", ha="right")
            y -= row_h
        return _fig_png(fig)
    finally:
        plt.close(fig)


def _mix(hex_color: str, amount: float) -> str:
    """Blend a hex color toward white (amount 0..1) to tell codes of one class apart."""
    c = hex_color.lstrip("#")
    rgb = [int(c[i : i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(v + (255 - v) * amount):02x}" for v in rgb)


def _format_time_axis(ax, *, from_date: datetime, to_date: datetime) -> None:
    """Fixed x-limits from the report period, readable tick labels and an explicit time zone."""
    tz = _report_tz(from_date)
    ax.set_xlim(from_date, to_date)
    span = (to_date - from_date).total_seconds()
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=tz, minticks=4, maxticks=8))
    fmt = "%H:%M" if span <= 24 * 3600 else "%m-%d %H:%M" if span <= 3 * 24 * 3600 else "%m-%d"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=tz))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha="center")
    ax.set_xlabel(
        f"{from_date:%Y-%m-%d %H:%M} – {_to_tz(to_date, tz):%Y-%m-%d %H:%M} ({_tz_label(from_date)})",
        fontsize=8,
        color="#6c757d",
        labelpad=6,
    )


# KPI Calculation


def _valid_values(points: list[dict]) -> list[float]:
    return [v for v in (_num(p.get("value")) for p in points) if v is not None]


def _availability_pct(up: int, total: int) -> float | None:
    """Availability in percent; anything below a perfect run never rounds up to 100.

    The web UI (``format_percent``) deliberately rounds >= 99.95 to "100%" for compact tiles; a
    printed report is a record, so it keeps 99.99 as the ceiling for imperfect runs.
    """
    if not total:
        return None
    pct = round(up / total * 100, 2)
    return pct if up == total else min(pct, _IMPERFECT_CEILING_PCT)


# Printed ceiling for any period that had at least one failed check (never rounds up to 100 %).
_IMPERFECT_CEILING_PCT = 99.99

# Below this share of the period covered by checks, a green availability result is not credible:
# the status turns neutral ("Insufficient data"). 80 % leaves room for gaps (restarts, maintenance)
# without letting a short burst of checks stand for a whole reporting period.
LOW_COVERAGE_PCT = 80.0


def _coverage_pct(report_data: ReportData, total_checks: int, interval_seconds: float) -> float | None:
    window = (report_data.to_date - report_data.from_date).total_seconds()
    if window <= 0:
        return None
    return round(min(100.0, total_checks * interval_seconds / window * 100), 1)


def _latest_uptime_sample(uptime_data: list[dict], tz: tzinfo, fallback: datetime | None = None) -> datetime | None:
    stamps = []
    for point in uptime_data or []:
        try:
            stamps.append(_to_tz(datetime.fromisoformat(point["ts"]), tz))
        except (KeyError, TypeError, ValueError):
            continue
    if stamps:
        return max(stamps)
    return _to_tz(fallback, tz) if fallback is not None else None


def _downtime_minutes(
    uptime_data: list[dict],
    interval_seconds: float,
    events: list[DowntimeEvent] | None = None,
    *,
    tz: tzinfo = UTC,
    fallback: datetime | None = None,
) -> int:
    """Whole minutes of the downtime table; an ongoing outage counts up to its last sample."""
    last = _latest_uptime_sample(uptime_data, tz, fallback)
    total = 0
    for event in events if events is not None else _extract_downtime_events(uptime_data, interval_seconds):
        if event.end is not None:
            total += event.duration_minutes or 0
        elif last is not None:
            total += int(max(0.0, (last - _to_tz(event.start, tz)).total_seconds()) / 60)
    return total


def _compute_kpis(report_data: ReportData) -> KPIMetrics:
    """Compute KPI metrics; a metric without measurements stays None (never 0).

    downtime_minutes sums the measured outages of the downtime table (shared rule, gaps excluded).
    """
    interval_seconds = report_data.target.get("interval_seconds") or 60

    response = _valid_values(report_data.http_data)
    ping = _valid_values(report_data.ping_data)
    checks = _valid_values(report_data.uptime_data)

    up_count = sum(1 for v in checks if v == 1)
    total_checks = len(checks)
    down_count = total_checks - up_count
    return KPIMetrics(
        uptime_percent=_availability_pct(up_count, total_checks),
        avg_response_ms=round(sum(response) / len(response)) if response else None,
        max_response_ms=round(max(response)) if response else None,
        downtime_minutes=_downtime_minutes(
            report_data.uptime_data,
            interval_seconds,
            _report_downtime_events(report_data),
            tz=_report_tz(report_data.from_date),
            fallback=report_data.to_date,
        ),
        total_checks=total_checks,
        up_count=up_count,
        down_count=down_count,
        avg_ping_ms=round(sum(ping) / len(ping), 1) if ping else None,
        response_samples=len(response),
        coverage_pct=_coverage_pct(report_data, total_checks, interval_seconds),
    )


def _extract_downtime_events(
    uptime_data: list[dict], interval_seconds: float = 60, now: datetime | None = None
) -> list[DowntimeEvent]:
    """Extract downtime periods from uptime data by the shared rule in ``availability_rules``.

    Points without a value are UNKNOWN, and a gap between two points is neither downtime nor uptime.
    """
    points: list[tuple[datetime, int | None]] = []
    skipped_count = 0
    for p in sorted(uptime_data or [], key=lambda p: p.get("ts", "")):
        ts_raw = p.get("ts")
        if not ts_raw:
            skipped_count += 1
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except (ValueError, AttributeError):
            skipped_count += 1
            continue
        value = p.get("value")
        points.append((ts, value if value in (0, 1) else None))

    events = [
        DowntimeEvent(
            start=o.start,
            end=o.end,
            duration_minutes=None if o.end is None else display_seconds(o.start, o.end) // 60,
        )
        for o in measured_outages(points, interval_seconds=interval_seconds, now=now)
        if o.end is None or (o.seconds() or 0) >= MIN_DOWNTIME_SECONDS
    ]
    if skipped_count > 0:
        _log.warning("Downtime extraction: skipped %d datapoints with invalid/missing timestamps", skipped_count)
    return events


def _report_downtime_events(report_data: ReportData) -> list[DowntimeEvent]:
    if report_data.downtime_periods is not None:
        return [
            DowntimeEvent(
                start=period.start,
                end=period.end,
                duration_minutes=None if period.end is None else display_seconds(period.start, period.end) // 60,
            )
            for period in reversed(report_data.downtime_periods)
        ]
    return _extract_downtime_events(report_data.uptime_data, report_data.target.get("interval_seconds") or 60)


# Custom Flowables


class RoundedKPICard(Flowable):
    """A KPI card: muted label on top, large centered value, optional hint at the bottom."""

    def __init__(
        self,
        label: str,
        value: str,
        width: float,
        height: float,
        value_color,
        font: str,
        font_bold: str,
        bg_color=None,
        corner_radius: float = 3 * mm,
        hint: str = "",
        value_size: int = 17,
        label_size: int = 9,
        border_width: float = 0.5,
        accent=None,
    ):
        super().__init__()
        self.label = label
        self.value = value
        self.card_width = width
        self.card_height = height
        self.value_color = value_color
        self.font = font
        self.font_bold = font_bold
        self.bg_color = bg_color or COLORS["surface"]
        self.corner_radius = corner_radius
        self.hint = hint
        self.value_size = value_size
        self.label_size = label_size
        self.border_width = border_width
        self.accent = accent

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab wrap() callback signature
        return self.card_width, self.card_height

    def draw(self):
        canvas = self.canv
        w, h = self.card_width, self.card_height

        canvas.saveState()
        canvas.setFillColor(self.bg_color)
        canvas.setStrokeColor(COLORS["divider"])
        canvas.setLineWidth(self.border_width)
        canvas.roundRect(0, 0, w, h, self.corner_radius, fill=1, stroke=1)

        if self.accent is not None:
            canvas.setFillColor(self.accent)
            canvas.roundRect(w / 2 - 9 * mm, h - 1.6 * mm, 18 * mm, 1.6 * mm, 0.8 * mm, fill=1, stroke=0)

        canvas.setFont(self.font_bold, self.label_size)
        canvas.setFillColor(COLORS["muted"])
        _draw_text(canvas, w / 2, h - 7.5 * mm, self.label, align="center")

        size = self.value_size
        max_width = w - 8 * mm
        while size > 8 and _text_width(self.value, self.font_bold, size) > max_width:
            size -= 1
        canvas.setFont(self.font_bold, size)
        canvas.setFillColor(self.value_color)
        _draw_text(canvas, w / 2, h / 2 - size * 0.18 - (1.5 * mm if self.hint else 0), self.value, align="center")

        if self.hint:
            canvas.setFont(self.font, 8)
            canvas.setFillColor(COLORS["muted"])
            _draw_text(canvas, w / 2, 3.5 * mm, self.hint, align="center")

        canvas.restoreState()


class Badge(Flowable):
    """A small badge with rounded corners and colored background."""

    def __init__(
        self,
        text: str,
        font: str = "IBMPlexSans",
        padding_x: float = 3 * mm,
        padding_y: float = 1.5 * mm,
        font_size: int = 8,
        bg_color=None,
        text_color=None,
        corner_radius: float = 2 * mm,
        bold: bool = True,
        shadow: bool = True,
    ):
        super().__init__()
        self.shadow = shadow
        self.text = text.upper()
        self.base_font = font
        # Use bold variant for badge text
        self.font = f"{font}-Bold" if bold and not font.endswith("-Bold") else font
        self.padding_x = padding_x
        self.padding_y = padding_y
        self.font_size = font_size
        self.bg_color = bg_color or COLORS["primary"]
        self.text_color = text_color or colors.white
        self.corner_radius = corner_radius

        # Calculate dimensions based on text width (use bold font for measurement)
        text_width = _text_width(self.text, self.font, font_size)
        self.badge_width = text_width + 2 * padding_x
        self.badge_height = font_size + 2 * padding_y

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab wrap() callback signature
        return self.badge_width, self.badge_height

    def draw(self):
        canvas = self.canv
        canvas.saveState()

        if self.shadow:
            _draw_soft_shadow(canvas, self.badge_width, self.badge_height, self.corner_radius)

        canvas.setFillColor(self.bg_color)
        canvas.roundRect(0, 0, self.badge_width, self.badge_height, self.corner_radius, fill=1, stroke=0)

        # Draw centered text (vertically centered using font metrics)
        canvas.setFont(self.font, self.font_size)
        canvas.setFillColor(self.text_color)
        # Calculate vertical center: (badge_height - font_size) / 2, adjusted for baseline
        # Font descender is typically ~20% of font size
        descender = self.font_size * 0.2
        text_y = (self.badge_height - self.font_size) / 2 + descender
        _draw_text(canvas, self.badge_width / 2, text_y, self.text, align="center")

        canvas.restoreState()


class HorizontalLine(Flowable):
    """A simple horizontal line flowable."""

    def __init__(self, width_pct=1.0, color=colors.black, thickness=0.5, space_before=1, space_after=1):
        super().__init__()
        self.width_pct = width_pct
        self.color = color
        self.thickness = thickness
        self.space_before = space_before
        self.space_after = space_after

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab wrap() callback signature
        self.width = availWidth * self.width_pct
        return self.width, self.thickness + self.space_before + self.space_after

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        canvas.setStrokeColor(self.color)
        canvas.setLineWidth(self.thickness)

        y = self.space_after + (self.thickness / 2)
        canvas.line(0, y, self.width, y)

        canvas.restoreState()


class ShareBar(Flowable):
    """A thin full-width bar split into colored shares (e.g. HTTP status classes)."""

    def __init__(self, shares: list[tuple[Any, float]], width: float, height: float = 4.5 * mm):
        super().__init__()
        self.shares, self.width, self.height = shares, width, height

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab callback signature
        return self.width, self.height

    def draw(self):
        canvas = self.canv
        x = 0.0
        for color, fraction in self.shares:
            w = self.width * fraction
            canvas.setFillColor(color)
            canvas.rect(x, 0, w, self.height, fill=1, stroke=0)
            x += w
        canvas.setStrokeColor(COLORS["divider"])
        canvas.rect(0, 0, self.width, self.height, fill=0, stroke=1)


class SuccessCheckCircle(Flowable):
    """A green circle with a white checkmark inside."""

    def __init__(self, size: float = 12 * mm, bg_color=None, check_color=None):
        super().__init__()
        self.size = size
        self.bg_color = bg_color or COLORS["success"]
        self.check_color = check_color or colors.white

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab wrap() callback signature
        return self.size, self.size

    def draw(self):
        canvas = self.canv
        canvas.saveState()

        canvas.setFillColor(self.bg_color)
        canvas.circle(self.size / 2, self.size / 2, self.size / 2, fill=1, stroke=0)

        canvas.setStrokeColor(self.check_color)
        canvas.setLineWidth(self.size * 0.08)  # Proportional line width
        canvas.setLineCap(1)  # Round cap
        canvas.setLineJoin(1)  # Round join

        # Checkmark coordinates (scaled to circle size)
        cx, cy = self.size / 2, self.size / 2
        scale = self.size / 12

        # Checkmark path: start from left, go down-right, then up-right
        p = canvas.beginPath()
        p.moveTo(cx - 2.5 * scale, cy - 0.2 * scale)  # Left point
        p.lineTo(cx - 0.5 * scale, cy - 2.2 * scale)  # Bottom point
        p.lineTo(cx + 3 * scale, cy + 2.3 * scale)  # Top-right point
        canvas.drawPath(p, fill=0, stroke=1)

        canvas.restoreState()


# Formatting helpers


def _fmt_pct(value: float | None) -> str:
    """Format an availability percentage; only a perfect run reads 100 %."""
    if value is None:
        return "n/a"
    if value >= 100:
        return "100%"
    return f"{min(value, _IMPERFECT_CEILING_PCT):.2f}%"


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f} ms"


def _fmt_duration(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    if minutes < 1:
        return "< 1 min"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days}d"] if days else []
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


def _span_text(start: datetime, end: datetime) -> str:
    seconds = max(0, int((end - start).total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    if days and not hours:
        return f"{days} day{'s' if days != 1 else ''}"
    if days:
        return f"{days} d {hours} h"
    return f"{hours} hour{'s' if hours != 1 else ''}" if hours else f"{rest // 60} min"


def _fmt_dt(value: datetime, tz: tzinfo) -> str:
    return _to_tz(value, tz).strftime("%Y-%m-%d %H:%M")


# Status kinds -> (accent, soft background, text color)
_KIND_COLORS = {
    "success": ("success", "success_soft", "success_text"),
    "warning": ("warning", "warning_soft", "warning_text"),
    "danger": ("danger", "danger_soft", "danger_text"),
    "unknown": ("neutral", "unknown_soft", "neutral_text"),
}

_PDF_STATUS_VARIANTS = {
    "pass": "success",
    "ok": "success",
    "resolved": "success",
    "fail": "danger",
    "down": "danger",
    "warn": "warning",
    "partial": "warning",
    "info": "info",
}


def _pdf_status_badge_colors(kind: str):
    """Return the shared background and foreground for a PDF status badge."""
    palette = STATUS_PALETTE[_PDF_STATUS_VARIANTS.get(kind, "neutral")]
    return colors.HexColor(palette.background), colors.HexColor(palette.foreground)


# Highest TTL the worker probes; a route reaching it may be cut off.
_ROUTE_MAX_HOPS = TRACE_HOP_LIMIT

# Chart reference lines when SLA is enabled without an own threshold (not an agreed SLA)
_DEFAULT_RESPONSE_MS = 500
_DEFAULT_PING_MS = 100


ROUTE_INCOMPLETE = f"Route incomplete – {_ROUTE_MAX_HOPS}-hop limit reached"


_ROUTE_FONT_BODY = 8.5  # pt; smallest text in the route diagram, readable on a printed A4 page
_ROUTE_FONT_TITLE = 9.5
FLAG_W, FLAG_H = 3.6 * mm, 2.7 * mm  # 4:3 flag icon in front of place names
FLAG_GAP = 1.2 * mm
FLAG_BASELINE_DROP = 0.3 * mm  # flag bottom sits slightly below the text baseline

# Wording of the measurement state (what was observed) and the location state (where GeoIP places it)
# is kept apart; a cause such as ICMP filtering is never stated because it is not measured.
STATE_NO_REPLY = "NO REPLY"
STATE_NOT_REACHED = "NOT REACHED"
LOCATION_UNKNOWN = "Location unknown"
FIRST_HOP_LABEL = "First observed hop"
_HOP_ONE_LINE_MAX = 34  # characters of address plus host name that fit the Host / Address column
GEOIP_FOOTNOTE = "Locations are GeoIP estimates; schematic, not geographically scaled."


def _hop_number(hop: dict[str, Any]) -> int | None:
    try:
        return int(hop.get("hop"))
    except (TypeError, ValueError):
        return None


def _hop_latency(hop: dict[str, Any] | None) -> float | None:
    """The single measured RTT of an answering hop, or None."""
    value = (hop or {}).get("rtt_ms")
    return float(value) if isinstance(value, int | float) and value else None


def _place(city: str, country: str) -> str:
    if not city and country:  # a country alone must not look like a place
        return f"{country.upper()} (country only)"
    return ", ".join(part for part in (city, country.upper()) if part)


def _pdf_group_key(node: dict[str, Any]) -> str | None:
    """Group consecutive measured hops by place; replies are never merged with silent hops."""
    if node["kind"] in ("destination", "single") or node["measurement"] != "measured":
        return None
    if node["location"] == "unlocated":
        return "unlocated"
    return f"loc:{node['city']}|{node['country']}" if node["city"] or node["country"] else None


def _build_route_entries(
    display_hops: list[dict[str, Any]], target_ip: str | None, destination_place: str = ""
) -> dict[str, Any]:
    """Turn traced hops into the entries of the vertical route diagram.

    Node states and the consecutive-hop rule come from the shared route timeline model; only
    the wording and the grouping key (place instead of map state) are specific to the PDF.
    """
    seq = expand_sequence_hops(display_hops)
    by_hop = {n: hop for hop in seq if (n := _hop_number(hop)) is not None}
    # Country-only entries still have a known location. The PDF does not draw its derived center,
    # but must preserve the country in the timeline instead of labelling it "Location unknown".
    located = {
        n
        for n, hop in by_hop.items()
        if (hop.get("city") or hop.get("country")) and timeline_measurement(hop) == "measured"
    }
    model = build_route_timeline(seq, located, target_ip, None)
    flat = model["flat_nodes"]
    known_destination = bool(target_ip) or any(
        hop.get("_destination") or hop.get("_synthetic_destination") for hop in seq
    )
    if not known_destination:  # no address to compare against: the destination is unconfirmed, not unreached
        flat = [n for n in flat if n["measurement"] != "unreached"]
    dest = next((n for n in reversed(flat) if n["kind"] in ("destination", "single")), None)
    reached = "unconfirmed" if dest is None else "reached" if dest["measurement"] == "measured" else "unreached"

    entries: list[dict[str, Any]] = []
    for node in group_timeline_nodes(flat, key_fn=_pdf_group_key, min_run=2):
        if node["kind"] == "group":
            members = node["members"]
            latencies = [lat for m in members if (lat := _hop_latency(by_hop.get(m["hop"]))) is not None]
            span = f"{node['first_hop']}–{node['last_hop']}"
            first_note = f" · includes {FIRST_HOP_LABEL.lower()}" if members[0]["kind"] == "first" else ""
            if members[0]["location"] == "unlocated":
                title, detail = f"{node['count']} measured hops", [f"{LOCATION_UNKNOWN}{first_note}"]
            else:
                title = _place(members[0]["city"], members[0]["country"])
                detail = [f"{node['count']} hops measured{first_note}"]
            right = f"{min(latencies):.1f}–{max(latencies):.1f} ms" if latencies else ""
            flag = None if members[0]["location"] == "unlocated" else normalize_country_code(members[0]["country"])
            entries.append(
                {"state": "measured", "span": span, "title": title, "detail": detail, "right": right, "flag": flag}
            )
            continue
        hop = by_hop.get(node["hop"])
        span = node["label"]
        place = _place(node["city"], node["country"])
        lat = _hop_latency(hop)
        if node["kind"] in ("destination", "single"):
            ok = node["measurement"] == "measured"
            detail = [node["ip"] or node["host"] or "address unknown"]
            detail.append(destination_place or place or LOCATION_UNKNOWN)
            right = f"{lat:.1f} ms · measured" if ok and lat else STATE_NO_REPLY if not ok else "measured"
            title = "Destination" if ok else f"Destination · {STATE_NOT_REACHED}"
            entries.append(
                {
                    "state": "destination" if ok else "unreached",
                    "span": span,
                    "title": title,
                    "detail": detail,
                    "right": right,
                }
            )
        elif node["measurement"] != "measured":
            entries.append(
                {
                    "state": "noreply",
                    "span": span,
                    "title": STATE_NO_REPLY,
                    "detail": [f"{LOCATION_UNKNOWN}, no measured value"],
                    "right": "",
                }
            )
        else:
            first = node["kind"] == "first"
            title = FIRST_HOP_LABEL if first else place or LOCATION_UNKNOWN
            detail = [d for d in (node["ip"] or node["host"],) if d]
            if first:
                detail.append(place or LOCATION_UNKNOWN)
            flag = normalize_country_code(node["country"]) if place else None
            entries.append(
                {
                    "state": "measured",
                    "span": span,
                    "title": title,
                    "detail": detail,
                    "right": f"{lat:.1f} ms · measured" if lat else "measured",
                    # The flag precedes the place: the title, or the place line of the first hop.
                    "flag": None if first else flag,
                    "detail_flag": (len(detail) - 1, flag) if first and flag else None,
                }
            )

    replies = sum(n["measurement"] == "measured" for n in flat)
    traced = sum(n["measurement"] != "unreached" for n in flat)
    verdict = {
        "reached": "DESTINATION REACHED",
        "unreached": "DESTINATION NOT REACHED",
        "unconfirmed": "DESTINATION NOT CONFIRMED",
    }[reached]
    return {
        "entries": entries,
        "reached": reached,
        "header": f"NETWORK PATH · {traced} HOPS · {replies} REPLIES · {verdict}",
    }


class RouteDiagram(Flowable):
    """Directed vertical route: one continuous line with arrows, collapsed runs and clear states.

    State is carried by marker shape and line style as well as color, so the diagram survives
    greyscale printing: filled dot = measured, dashed hollow dot = no reply, diamond "D" =
    destination reached (as on the route map), diamond "×" = destination not reached.
    """

    _X_HOP = 11 * mm  # right edge of the hop-number column
    _X_LINE = 16 * mm
    _X_TEXT = 23 * mm
    _RIGHT_W = 36 * mm
    _LEAD = 4.0 * mm
    _PAD = 1.9 * mm
    _LEGEND = (("measured", "Measured"), ("noreply", "No reply"), ("destination", "Destination"))

    def __init__(
        self,
        header: str,
        entries: list[dict[str, Any]],
        font: str,
        font_bold: str,
        max_width: float,
        *,
        continued: bool = False,
        continues: str | None = None,
        legend_states: frozenset[str] | None = None,
        flags: dict[str, ImageReader] | None = None,
    ):
        super().__init__()
        self.flags = flags or {}
        self.header, self.entries = header, entries
        self.continued, self.continues = continued, continues
        self.legend_states = legend_states or frozenset(e["state"] for e in entries)
        self.font, self.font_bold = font, font_bold
        self.width = max_width
        text_w = max_width - self._X_TEXT - self._RIGHT_W - 2 * mm
        self._lines = []
        self._flag_lines: list[int | None] = []  # first rendered line of the detail that carries a flag
        for entry in entries:
            lines = []
            flag_line = None
            detail_flag = entry.get("detail_flag")
            for n, detail in enumerate(entry["detail"]):
                if detail_flag and detail_flag[0] == n:
                    flag_line = len(lines)
                lines.extend(_split_text(detail, font, _ROUTE_FONT_BODY, text_w) or [""])
            self._lines.append(lines)
            self._flag_lines.append(flag_line)
        self._heights = [self._PAD + self._LEAD * (1 + len(lines)) for lines in self._lines]
        self._head_h = 8 * mm
        self._legend_h = 8 * mm
        self.height = self._head_h + sum(self._heights) + self._legend_h

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab callback signature
        return self.width, self.height

    def split(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab callback signature
        """Break between hops when the route does not fit; a part needs at least three entries to stand alone."""
        if self.height <= availHeight:
            return []
        budget = availHeight - self._head_h - self._legend_h - 1  # 1 pt tolerance against float rounding
        count, used = 0, 0.0
        for height in self._heights:
            if used + height > budget:
                break
            used += height
            count += 1
        if count == 0 or count >= len(self.entries) or count < min(3, len(self.entries) - 1):
            return []
        rest = self.entries[count:]
        kwargs = {"legend_states": self.legend_states, "flags": self.flags}
        head = RouteDiagram(
            self.header,
            self.entries[:count],
            self.font,
            self.font_bold,
            self.width,
            continued=self.continued,
            continues=rest[0]["span"],
            **kwargs,
        )
        tail = RouteDiagram(
            self.header,
            rest,
            self.font,
            self.font_bold,
            self.width,
            continued=True,
            continues=self.continues,
            **kwargs,
        )
        return [head, tail]

    def _marker(self, canvas, state: str, cx: float, cy: float) -> None:
        navy, muted = COLORS["navy"], COLORS["muted"]
        canvas.setDash([])
        canvas.setLineWidth(1.2)
        if state == "noreply":
            canvas.setStrokeColor(muted)
            canvas.setFillColor(colors.white)
            canvas.setDash([1.6, 1.4])
            canvas.circle(cx, cy, 1.9 * mm, fill=1, stroke=1)
        elif state == "destination":  # same diamond "D" as on the route map
            draw_destination_diamond(canvas, cx, cy, 2.9 * mm, DESTINATION_COLOR, self.font_bold, 7.5)
        elif state == "unreached":
            draw_destination_diamond(canvas, cx, cy, 2.9 * mm, COLORS["danger"], self.font_bold, 7.5, glyph="×")
        else:
            canvas.setStrokeColor(navy)
            canvas.setFillColor(navy)
            canvas.circle(cx, cy, 1.9 * mm, fill=1, stroke=1)
        canvas.setDash([])

    def _flag(self, canvas, code: str | None, x: float, baseline: float) -> float:
        """Draw the flag left of a text baseline; returns the horizontal space taken (0 without flag)."""
        image = self.flags.get(code or "")
        if image is None:
            return 0.0
        canvas.drawImage(image, x, baseline - FLAG_BASELINE_DROP, FLAG_W, FLAG_H, mask="auto")
        return FLAG_W + FLAG_GAP

    def _arrow(self, canvas, x: float, y_from: float, y_to: float, dashed: bool) -> None:
        canvas.setStrokeColor(COLORS["muted"] if dashed else COLORS["navy_mid"])
        canvas.setFillColor(COLORS["muted"] if dashed else COLORS["navy_mid"])
        canvas.setLineWidth(1.1)
        canvas.setDash([2, 2] if dashed else [])
        tip = 1.6 * mm
        canvas.line(x, y_from, x, y_to + tip)
        canvas.setDash([])
        path = canvas.beginPath()
        path.moveTo(x, y_to)
        path.lineTo(x - 1.1 * mm, y_to + tip)
        path.lineTo(x + 1.1 * mm, y_to + tip)
        path.close()
        canvas.drawPath(path, fill=1, stroke=0)

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        y = self.height
        canvas.setFont(self.font_bold, 8.5)
        canvas.setFillColor(COLORS["navy"])
        title = (
            f"{self.header.split(' · ')[0]} · CONTINUED FROM HOP {self.entries[0]['span']}"
            if self.continued
            else self.header
        )
        _draw_text(canvas, 0, y - 5 * mm, title)
        y -= self._head_h
        if self.continued:
            self._arrow(canvas, self._X_LINE, y + 1.5 * mm, y - self._PAD / 2 - self._LEAD * 0.55 + 2.6 * mm, False)
        centers: list[float] = []
        for height in self._heights:
            centers.append(y - self._PAD / 2 - self._LEAD * 0.55)
            y -= height
        for i in range(len(self.entries) - 1):
            a, b = self.entries[i]["state"], self.entries[i + 1]["state"]
            gap_to = 3.4 * mm if b in ("destination", "unreached") else 2.6 * mm
            self._arrow(canvas, self._X_LINE, centers[i] - 2.8 * mm, centers[i + 1] + gap_to, "noreply" in (a, b))
        for entry, lines, cy, flag_line in zip(self.entries, self._lines, centers, self._flag_lines, strict=True):
            muted = entry["state"] == "noreply"
            self._marker(canvas, entry["state"], self._X_LINE, cy)
            canvas.setFillColor(COLORS["muted"] if muted else COLORS["navy"])
            canvas.setFont(self.font_bold, _ROUTE_FONT_BODY)
            _draw_text(canvas, self._X_HOP, cy - 1 * mm, entry["span"], align="right")
            canvas.setFont(self.font_bold, _ROUTE_FONT_TITLE)
            title_color = COLORS["danger_text"] if entry["state"] == "unreached" else COLORS["navy"]
            canvas.setFillColor(COLORS["muted"] if muted else title_color)
            title_x = self._X_TEXT + self._flag(canvas, entry.get("flag"), self._X_TEXT, cy - 1 * mm)
            _draw_text(canvas, title_x, cy - 1 * mm, entry["title"])
            if entry["right"]:
                canvas.setFont(self.font, _ROUTE_FONT_BODY)
                canvas.setFillColor(COLORS["dark"])
                _draw_text(canvas, self.width, cy - 1 * mm, entry["right"], align="right")
            canvas.setFont(self.font, _ROUTE_FONT_BODY)
            canvas.setFillColor(COLORS["muted"] if muted else COLORS["dark"])
            detail_flag = entry.get("detail_flag")
            for n, line in enumerate(lines, start=1):
                y_line = cy - 1 * mm - n * self._LEAD
                x_line = self._X_TEXT
                if detail_flag and n - 1 == flag_line:
                    x_line += self._flag(canvas, detail_flag[1], self._X_TEXT, y_line)
                _draw_text(canvas, x_line, y_line, line)
        if self.continues:
            self._arrow(canvas, self._X_LINE, centers[-1] - 2.8 * mm, 3 * mm - 1 * mm, False)
            canvas.setFont(self.font_bold, _ROUTE_FONT_BODY)
            canvas.setFillColor(COLORS["navy"])
            _draw_text(canvas, self._X_TEXT, 3 * mm, f"Route continues on the next page at hop {self.continues}")
            canvas.restoreState()
            return
        # Legend: the same marker shapes, named
        present = self.legend_states
        x, ly = 0.0, 3 * mm
        canvas.setFont(self.font, _ROUTE_FONT_BODY)
        for state, label in self._LEGEND:
            if state not in present and not (state == "destination" and "unreached" in present):
                continue
            shown = "unreached" if state == "destination" and state not in present else state
            self._marker(canvas, shown, x + 2.6 * mm, ly + 1 * mm)
            canvas.setFillColor(COLORS["dark"])
            text = "Destination not reached" if shown == "unreached" else label
            _draw_text(canvas, x + 6.5 * mm, ly, text)
            x += 8 * mm + _text_width(text, self.font, _ROUTE_FONT_BODY) + 6 * mm
        canvas.restoreState()


class FlagLabel(Flowable):
    """Table cell text with a small flag in front of the first line; the text stays a normal Paragraph."""

    def __init__(self, image: ImageReader, para: Paragraph):
        super().__init__()
        self.image, self.para = image, para
        self._offset = FLAG_W + FLAG_GAP

    def wrap(self, availWidth, availHeight):  # noqa: N803 - reportlab callback signature
        self.width, self.height = self.para.wrap(max(availWidth - self._offset, 1), availHeight)
        return availWidth, self.height

    def draw(self):
        self.para.drawOn(self.canv, self._offset, 0)
        style = self.para.style
        ascent = getattr(getattr(self.para, "blPara", None), "ascent", None) or style.fontSize
        baseline = self.height - ascent
        self.canv.drawImage(self.image, 0, baseline - FLAG_BASELINE_DROP, FLAG_W, FLAG_H, mask="auto")


class _ReportDocTemplate(SimpleDocTemplate):
    """Records chapter start pages (flowables carrying `chapter_name`) and pages that hold real content."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chapter_pages: dict[str, int] = {}
        self.content_pages: set[int] = set()

    def afterFlowable(self, flowable):  # noqa: N802 - reportlab callback name
        is_note = isinstance(flowable, Paragraph) and flowable.style.name == "Note"
        if not isinstance(flowable, ActionFlowable | PageBreak | Spacer) and not is_note:
            self.content_pages.add(self.page)
        name = getattr(flowable, "chapter_name", None)
        if name:
            self.chapter_pages.setdefault(name, self.page)


# PDF Document Generation


class PDFReportBuilder:
    """Builds the PDF report using ReportLab."""

    def __init__(self, report_data: ReportData, page_size: str = "a4"):
        # Normalize to one aware time zone so naive inputs cannot break charts or tables
        tz = _report_tz(report_data.from_date)
        report_data = dataclasses.replace(
            report_data, from_date=_to_tz(report_data.from_date, tz), to_date=_to_tz(report_data.to_date, tz)
        )
        self.report_data = report_data
        self.page_size = LETTER if page_size.lower() == "letter" else A4
        self.width, self.height = self.page_size
        self.margin = 20 * mm
        # SimpleDocTemplate frames carry 6 pt padding per side; full-width flowables must fit inside it
        self.content_width = self.width - 2 * self.margin - 12
        self.tz = _report_tz(report_data.from_date)
        self.font = _get_font()
        self.font_regular = self.font  # Alias for consistency
        self.font_bold = _get_font_bold()
        self.styles = self._create_styles()
        self.elements: list = []
        self.kpis = _compute_kpis(report_data)
        self.downtime_events = _report_downtime_events(report_data)
        self.chapter_pages: dict[str, int] = {}
        self.page_count = 0
        self.content_pages: set[int] = set()
        # Route cache: stores (display_hops, geo_points, has_interpolation)
        self._route_cache: dict[tuple[str, int], tuple[list, list, bool]] = {}
        # True when the trace ended in silent hops at the hop limit (destination never confirmed)
        self._route_silent_at_limit = False
        # WhatWeb cache: stores the tech stack dict
        self._whatweb_cache: dict | None = None
        self._whatweb_fetched: bool = False
        # TLS scan cache: normalized result (None = not applicable, e.g. non-HTTPS target)
        self._ssl_cache: TlsScanResult | None = None
        self._ssl_fetched: bool = False
        self._ssl_scanned_at: datetime | None = None
        # Decorative flag images by ISO code; filled by prefetch_external_data, empty means text only
        self._flags: dict[str, ImageReader] = {}

    def prefetch_external_data(self) -> None:
        """Fetch WhatWeb, TLS scan, traceroute and provider data (blocking I/O, call before build).

        This method should be called before acquiring the PDF generation lock
        to avoid blocking other PDF requests on slow external scans.
        """
        self._get_whatweb_data()
        self._get_ssl_data()
        display_hops, _geo, _interp = self._get_route_data()
        self._provider_info()
        self._prefetch_flags(display_hops)

    def _prefetch_flags(self, display_hops: list[dict[str, Any]]) -> None:
        """Load the flags of all shown countries once; failures leave the plain country code."""
        try:
            hops = expand_sequence_hops(display_hops)
            codes = [hop.get("country") for hop in hops if timeline_measurement(hop) == "measured"]
            self._flags = {c: ImageReader(io.BytesIO(png)) for c, png in load_flags(codes).items()}
        except Exception:  # noqa: BLE001 - flags are optional decoration
            _log.debug("Flag prefetch failed", exc_info=True)
            self._flags = {}

    def _create_styles(self) -> dict:
        """Create the paragraph styles used across all chapters."""

        def ps(name: str, *, bold: bool = False, **kw) -> ParagraphStyle:
            kw.setdefault("textColor", COLORS["dark"])
            return _paragraph_style(name, font_name=self.font_bold if bold else self.font, **kw)

        return {
            "chapter_title": ps("ChapterTitle", bold=True, fontSize=22, leading=26, textColor=COLORS["navy"]),
            "chapter_sub": ps("ChapterSub", fontSize=9.5, leading=13, textColor=COLORS["muted"], spaceAfter=2),
            "group": ps(
                "Group", bold=True, fontSize=11.5, leading=15, textColor=COLORS["navy"], spaceBefore=12, spaceAfter=4
            ),
            "chart_title": ps(
                "ChartTitle",
                bold=True,
                fontSize=11.5,
                leading=15,
                textColor=COLORS["navy"],
                spaceBefore=4,
                spaceAfter=2,
            ),
            "chart_note": ps("ChartNote", fontSize=8.5, leading=12, textColor=COLORS["muted"], spaceAfter=12),
            "label": ps("Label", fontSize=9, leading=12, textColor=COLORS["muted"]),
            "value": ps("Value", fontSize=9.5, leading=13),
            "token": ps("Token", fontSize=9, leading=12, wordWrap="CJK"),
            "token_small": ps("TokenSmall", fontSize=8, leading=10, textColor=COLORS["muted"], wordWrap="CJK"),
            "small": ps("Small", fontSize=8, leading=10, textColor=COLORS["muted"]),
            "note": ps("Note", fontSize=8.5, leading=12, textColor=COLORS["muted"], spaceBefore=6),
            "th": ps("TableHead", bold=True, fontSize=8.5, leading=11, textColor=COLORS["white"]),
            "th_right": ps(
                "TableHeadRight", bold=True, fontSize=8.5, leading=11, textColor=COLORS["white"], alignment=2
            ),
            "td": ps("TableCell", fontSize=9.5, leading=12),
            "td_right": ps("TableCellRight", fontSize=9.5, leading=12, alignment=2),
            "td_muted": ps("TableCellMuted", fontSize=9.5, leading=12, textColor=COLORS["muted"]),
            "td_muted_right": ps(
                "TableCellMutedRight", fontSize=9.5, leading=12, textColor=COLORS["muted"], alignment=2
            ),
            "hop_ip": ps("HopIp", fontSize=9.5, leading=12, wordWrap="CJK"),
            "hop_ip_muted": ps("HopIpMuted", fontSize=9.5, leading=12, textColor=COLORS["muted"], wordWrap="CJK"),
            "hop_host": ps("HopHost", fontSize=8, leading=10, textColor=COLORS["muted"], wordWrap="CJK"),
            "empty_title": ps(
                "EmptyTitle", bold=True, fontSize=16, leading=22, textColor=COLORS["success_text"], alignment=1
            ),
            "empty_text": ps("EmptyText", fontSize=10.5, leading=15, textColor=COLORS["muted"], alignment=1),
        }

    def _get_whatweb_data(self) -> dict | None:
        """Fetch and cache WhatWeb data for the target.

        Returns the cached result on subsequent calls to avoid duplicate scans.
        """
        if self._whatweb_fetched:
            return self._whatweb_cache

        self._whatweb_fetched = True
        target = self.report_data.target
        http_url = target.get("http_url", "")

        if not http_url:
            return None

        _log.debug("Fetching WhatWeb data for %s", redact_url(http_url))
        try:
            self._whatweb_cache = run_whatweb(http_url, aggression=1, timeout=20)
        except ValueError as e:
            _log.warning("Invalid URL for whatweb: %s", e)
            self._whatweb_cache = None

        return self._whatweb_cache

    def _assemble(self) -> None:
        """Compose all chapters into self.elements (each chapter starts on a new page)."""
        self.elements = []
        # Page 1: Cover (ends with a page break)
        self._build_cover_page()

        # Page 2: Technical Summary
        self._add_technical_summary()
        self.elements.append(PageBreak())

        # Page 3: Network Path
        self._build_network_path_page()
        self.elements.append(PageBreak())

        # Page 4: Charts ("Availability & Performance"). Without incidents the downtime summary
        # joins this page; with incidents the Downtime Log keeps its own page.
        merge_downtime = not self.downtime_events
        self._build_charts_page(page_break=not merge_downtime)
        if merge_downtime:
            self._add_downtime_summary()
        else:
            self._build_downtime_page()

    def build(self) -> bytes:
        """Build the complete PDF and return as bytes."""
        buffer = io.BytesIO()

        self._assemble()
        doc = _ReportDocTemplate(
            buffer,
            pagesize=self.page_size,
            leftMargin=self.margin,
            rightMargin=self.margin,
            topMargin=self.margin,
            bottomMargin=self.margin,
            title=f"justUp Monitoring Report - {self._cover_name()[0]}",
            lang="en",
            displayDocTitle=True,
            author="justUp!",
            subject="Uptime monitoring report",
            creator=f"justUp! {VERSION}",
            keywords=["justUp", "uptime", "monitoring"],
        )

        doc.build(self.elements, onFirstPage=self._add_header_footer, onLaterPages=self._add_header_footer)
        self.chapter_pages = dict(doc.chapter_pages)
        self.page_count = doc.page
        self.content_pages = set(doc.content_pages)

        buffer.seek(0)
        return buffer.getvalue()

    def _add_header_footer(self, canvas, doc):
        """Add header and footer to each page matching website style."""
        # Header bar: dark blue structure with a slim green accent
        canvas.saveState()
        canvas.setFillColor(COLORS["navy"])
        canvas.rect(0, self.height - 8 * mm, self.width, 8 * mm, fill=1, stroke=0)
        canvas.setFillColor(COLORS["success"])
        canvas.rect(0, self.height - 9 * mm, self.width, 1 * mm, fill=1, stroke=0)

        # Footer hairline
        canvas.setStrokeColor(COLORS["muted"])
        canvas.setLineWidth(0.25)
        canvas.line(self.margin, 18 * mm, self.width - self.margin, 18 * mm)

        target = self.report_data.target
        domain = ""
        http_url = target.get("http_url", "")
        if http_url:
            try:
                parsed = urlparse(http_url)
                domain = parsed.hostname or ""
            except ValueError:
                pass
        if not domain:
            domain = target.get("ping_host") or target.get("host") or target.get("name", "")

        # Footer layout (matching website):
        # Line 1: wordmark + v<VERSION> | © <YEAR> Gill-Bates | Lightweight Uptime Monitor
        # Line 2: Domain | Report generated at timestamp

        # justUp! wordmark (40% transparency), shared with the web app
        logo_x = self.margin
        logo_height = 4.5 * mm
        logo_width = logo_height / logo_aspect()
        draw_logo(canvas, logo_x, 10.0 * mm, logo_width, alpha=0.6)
        canvas.setFont(self.font, 8)
        canvas.setFillColor(COLORS["muted"])
        now = datetime.now(tz=UTC)
        build_suffix = f" ({BUILD_INFO[:7]})" if BUILD_INFO and BUILD_INFO != "dev" else ""
        footer_text = f"v{VERSION}{build_suffix} | © {now.year} Gill-Bates | Lightweight Uptime Monitor | "
        footer_text_x = logo_x + logo_width + 2 * mm
        _draw_text(canvas, footer_text_x, 11 * mm, footer_text)

        # GitHub link (after footer text)
        github_url = "https://github.com/Gill-Bates/justUp"
        footer_text_width = _text_width(footer_text, self.font, 8)
        github_x = footer_text_x + footer_text_width

        # GitHub icon with 40% transparency (alpha=0.6)
        github_icon_path = STATIC_DIR / "github.png"
        icon_size = (14 * 0.7 * 0.8) / 72 * 25.4 * mm
        if github_icon_path.exists():
            canvas.saveState()
            canvas.setFillAlpha(0.6)
            canvas.drawImage(
                str(github_icon_path),
                github_x,
                10.5 * mm,
                width=icon_size,
                height=icon_size,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",  # Handle PNG transparency correctly
            )
            canvas.restoreState()
        icon_width = icon_size + 0.8 * mm

        canvas.setFont(self.font, 8)
        canvas.setFillColor(COLORS["muted"])
        github_label = "GitHub"
        _draw_text(canvas, github_x + icon_width, 11 * mm, github_label)

        # Make it clickable (both icon and text)
        github_label_width = _text_width(github_label, self.font, 8)
        canvas.linkURL(github_url, (github_x, 9 * mm, github_x + icon_width + github_label_width, 14 * mm), relative=0)

        # Right side: page number
        _draw_text(canvas, self.width - self.margin, 11 * mm, f"Page {doc.page}", align="right")

        # Second line: Domain and timestamp
        timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
        canvas.setFont(self.font, 8)
        right_text = f"Report generated at {timestamp}"
        room = self.content_width - _text_width(right_text, self.font, 8) - 4 * mm
        while len(domain) > 4 and _text_width(domain, self.font, 8) > room:
            domain = domain[:-4] + "…"
        _draw_text(canvas, self.margin, 6 * mm, domain)
        _draw_text(canvas, self.width - self.margin, 6 * mm, right_text, align="right")

        canvas.restoreState()

    # Shared building blocks

    def _chapter_header(self, title: str, subtitle: str = "", *, chapter: str | None = None) -> None:
        """Chapter title, optional subtitle and a navy rule."""
        head = Paragraph(_esc(title), self.styles["chapter_title"])
        head.chapter_name = chapter or title
        self.elements.append(head)
        if subtitle:
            self.elements.append(Paragraph(subtitle, self.styles["chapter_sub"]))
        self.elements.append(
            HorizontalLine(color=COLORS["navy"], thickness=1.2, space_before=1 * mm, space_after=3 * mm)
        )

    def _state_badge(self, kind: str, text: str | None = None) -> Badge:
        """Small result badge. Unknown (not tested) is deliberately neutral, never green."""
        labels = {"pass": "PASS", "fail": "FAIL", "warn": "WARNING", "unknown": "NOT TESTED", "info": "INFO"}
        bg, fg = _pdf_status_badge_colors(kind)
        return self._small_badge(text or labels.get(kind, "UNKNOWN"), bg, fg)

    def _small_badge(self, text: str, bg_color, text_color=None) -> Badge:
        """The one compact badge style of the report tables."""
        return Badge(
            text=text,
            font=self.font,
            font_size=8,
            padding_x=2.2 * mm,
            padding_y=0.9 * mm,
            corner_radius=1.6 * mm,
            bg_color=bg_color,
            text_color=text_color,
            shadow=False,
        )

    def _kv_table(self, rows: list[tuple[Any, Any, Any]]) -> Table:
        """Label | value | state table. A state is a kind or a (kind, badge text) pair; rows wrap, never truncate."""
        label_w, state_w = 38 * mm, 28 * mm
        data = []
        for label, value, state in rows:
            label_cell = Paragraph(_esc(label), self.styles["label"]) if isinstance(label, str) else label
            value_cell = value if not isinstance(value, str) else Paragraph(value, self.styles["value"])
            if isinstance(state, tuple):
                state_cell = self._state_badge(*state)
            else:
                state_cell = self._state_badge(state) if state else ""
            data.append([label_cell, value_cell, state_cell])
        table = Table(data, colWidths=[label_w, self.content_width - label_w - state_w, state_w], splitByRow=1)
        table.setStyle(
            _table_style(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, COLORS["divider"]),
                    ("BACKGROUND", (0, 0), (0, -1), COLORS["surface"]),
                ]
            )
        )
        table.hAlign = "LEFT"
        return table

    def _group_title(self, text: str) -> None:
        para = Paragraph(_esc(text), self.styles["group"])
        para.keepWithNext = 1
        self.elements.append(para)

    # Cover

    def _is_low_coverage(self) -> bool:
        cov = self.kpis.coverage_pct
        return cov is not None and cov < LOW_COVERAGE_PCT

    def _uptime_level(self) -> str:
        """Availability level from the app-wide rule (SLA goal if enabled, else fixed limits)."""
        target = self.report_data.target
        return uptime_level(
            self.kpis.uptime_percent,
            sla_enabled=bool(target.get("sla_enabled")),
            sla_availability_pct=target.get("sla_availability_pct"),
        )

    def _effective_level(self) -> str:
        """Level shown in the report: low coverage turns "ok" into "none" (no sufficient evidence).

        Failed checks stay visible: sparse data can disprove availability but cannot prove it.
        """
        level = self._uptime_level()
        return "none" if level == "ok" and self._is_low_coverage() else level

    def _coverage_gap_text(self) -> str:
        cov = self.kpis.coverage_pct
        return f"Not enough measurement data for the remaining {100 - cov:.1f}% of the reporting period."

    def _coverage_hint(self) -> str:
        kpis = self.kpis
        return f"{kpis.up_count:,}/{kpis.total_checks:,} measurements · data coverage {kpis.coverage_pct:g}%"

    def _status_info(self) -> tuple[str, str, str]:
        """Return (label, detail, kind) from measured availability and data coverage."""
        kpis = self.kpis
        if kpis.uptime_percent is None:
            return "No data", "No availability measurements were recorded in this period.", "unknown"
        ongoing = any(e.end is None for e in self.downtime_events)
        tail = " · Last check: down (outage ongoing at end of period)" if ongoing else ""
        level = self._effective_level()
        if level == "none":
            return (
                "Insufficient data",
                f"{_fmt_pct(kpis.uptime_percent)} of recorded checks successful. {self._coverage_gap_text()}",
                "unknown",
            )
        # Sparse data cannot prove availability for the whole period: name it a success rate.
        noun = "of recorded checks successful" if self._is_low_coverage() else "availability"
        detail = (
            f"{_fmt_pct(kpis.uptime_percent)} {noun} · {kpis.down_count} of {kpis.total_checks} checks failed{tail}"
        )
        if self._is_low_coverage():
            detail += f" · Low data coverage ({kpis.coverage_pct:g}% of the period)"
        if level == "ok":
            return "Operational", detail, "success"
        if level == "warn":
            return "Degraded", detail, "warning"
        sla = self.report_data.target.get("sla_enabled") and self.report_data.target.get("sla_availability_pct")
        return ("Below SLA" if sla else "Critical"), detail, "danger"

    def _cover_name(self) -> tuple[str, str]:
        """Return (title, secondary line): the target name, plus its host when it differs."""
        target = self.report_data.target
        host = target.get("ping_host") or target.get("host") or ""
        if not host and target.get("http_url"):
            # Only the host name of the URL: userinfo, query and fragment may carry secrets.
            try:
                host = urlparse(str(target["http_url"])).hostname or ""
            except ValueError:
                host = ""
        host = str(host)
        name = str(target.get("name") or host or "Unknown target")
        return name, ("" if host.lower() == name.lower() else host)

    def _build_cover_page(self):
        """Build the stand-alone cover: target, period, status and headline KPIs only."""
        from_date, to_date = self.report_data.from_date, self.report_data.to_date

        self.elements.append(LogoFlowable(34 * mm))
        self.elements.append(Spacer(1, 8 * mm))

        name, host = self._cover_name()
        size = 32
        while size > 16 and _text_width(name, self.font_bold, size) > self.content_width:
            size -= 1
        title_style = _paragraph_style(
            "CoverTitle",
            font_name=self.font_bold,
            fontSize=size,
            leading=size + 6,
            textColor=COLORS["navy"],
            wordWrap="CJK",
        )
        title = Paragraph(_esc(name), title_style)
        title.chapter_name = "Cover"
        self.elements.append(title)
        # A wrapped name leaves no room for the website title on the one-page cover
        name_wraps = title.wrap(self.content_width, 1e4)[1] > title_style.leading + 1

        site_title = None if name_wraps else _clean_site_title((self._get_whatweb_data() or {}).get("title"), self.font)
        if site_title:
            self.elements.append(
                Paragraph(
                    _esc(site_title),
                    _paragraph_style(
                        "CoverSiteTitle",
                        font_name=self.font,
                        fontSize=12,
                        leading=16,
                        textColor=COLORS["muted"],
                        wordWrap="CJK",
                        maxLines=2,
                        splitLongWords=1,
                    ),
                )
            )
        if host:
            self.elements.append(
                Paragraph(
                    _esc(host),
                    _paragraph_style(
                        "CoverHost", font_name=self.font, fontSize=11, leading=15, textColor=COLORS["muted"]
                    ),
                )
            )
        self.elements.append(Spacer(1, 3 * mm))
        self.elements.append(
            Paragraph(
                "Uptime &amp; Performance Report",
                _paragraph_style(
                    "CoverSub", font_name=self.font_bold, fontSize=15, leading=20, textColor=COLORS["navy_mid"]
                ),
            )
        )
        self.elements.append(Spacer(1, 3 * mm))
        self.elements.append(
            HorizontalLine(width_pct=0.18, color=COLORS["success"], thickness=2.2, space_before=0, space_after=0)
        )
        self.elements.append(Spacer(1, 5 * mm))

        period_style = _paragraph_style(
            "CoverPeriod", font_name=self.font, fontSize=10.5, leading=15, textColor=COLORS["dark"]
        )
        period_label = _paragraph_style(
            "CoverPeriodLabel", font_name=self.font_bold, fontSize=8, leading=11, textColor=COLORS["muted"]
        )
        self.elements.append(Paragraph("REPORTING PERIOD", period_label))
        self.elements.append(
            Paragraph(
                f"{_fmt_dt(from_date, self.tz)} – {_fmt_dt(to_date, self.tz)} {_tz_label(from_date)} "
                f"({_span_text(from_date, to_date)})",
                period_style,
            )
        )
        self.elements.append(Spacer(1, 5 * mm))

        self._add_status_banner()
        # The one-page cover keeps priority over the extra hero gap when name or website title take space
        hero_gap = 8 * mm if name_wraps else 5 * mm if site_title else 16 * mm
        self.elements.append(Spacer(1, hero_gap))
        self._add_hero_kpi()
        self.elements.append(Spacer(1, 4 * mm))
        self._add_supporting_kpis()

        if self.report_data.target.get("sla_enabled"):
            self.elements.append(Spacer(1, 4 * mm))
            self._add_sla_compliance()

        self.elements.append(PageBreak())

    def _add_status_banner(self):
        """Add a status panel colored by the measured availability."""
        label, detail, kind = self._status_info()
        accent_key, soft_key, text_key = _KIND_COLORS[kind]
        text_color = COLORS[text_key]
        status_para = Paragraph(
            _esc(label),
            _paragraph_style("StatusBanner", font_name=self.font_bold, fontSize=17, leading=21, textColor=text_color),
        )
        detail_para = Paragraph(
            _esc(detail),
            _paragraph_style("StatusDetail", font_name=self.font, fontSize=10, leading=14, textColor=COLORS["dark"]),
        )
        table = Table([[status_para, detail_para]], colWidths=[54 * mm, self.content_width - 54 * mm])
        table.setStyle(
            _table_style(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), COLORS[soft_key]),
                    ("LINEBEFORE", (0, 0), (0, -1), 4, COLORS[accent_key]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 11),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        self.elements.append(table)

    def _add_hero_kpi(self):
        """Add availability as the central hero KPI; with low coverage it is a success rate, not availability."""
        kpis = self.kpis
        pct = kpis.uptime_percent
        label = "AVAILABILITY"
        if pct is None:
            color, hint = COLORS["muted"], "no measurements in this period"
        else:
            level = self._effective_level()
            color = {"ok": COLORS["success"], "warn": COLORS["warning_text"], "bad": COLORS["danger"]}.get(
                level, COLORS["muted"]
            )
            if kpis.coverage_pct is None:
                hint = f"{kpis.up_count:,} of {kpis.total_checks:,} checks successful"
            elif self._is_low_coverage():
                label, hint = "RECORDED CHECKS SUCCESSFUL", self._coverage_hint()
            else:
                hint = f"{kpis.up_count:,} of {kpis.total_checks:,} checks successful · {kpis.coverage_pct:g}% coverage"

        width, height = 100 * mm, 34 * mm
        hero_card = RoundedKPICard(
            label=label,
            value=_fmt_pct(pct),
            width=width,
            height=height,
            value_color=color,
            font=self.font,
            font_bold=self.font_bold,
            value_size=36,
            label_size=9,
            hint=hint,
            accent=color,
        )
        table = Table([[hero_card]], colWidths=[width])
        table.setStyle(_table_style([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        table.hAlign = "CENTER"
        self.elements.append(table)

    def _add_supporting_kpis(self):
        """Add the three equal KPI cards: average response, maximum response, estimated downtime."""
        kpis = self.kpis
        gap = 5 * mm
        card_width = (self.content_width - 2 * gap) / 3
        has_checks = kpis.total_checks > 0
        samples = f"{kpis.response_samples:,} samples" if kpis.response_samples else "no measurements"
        down_color = COLORS["danger"] if kpis.downtime_minutes > 0 else COLORS["dark"]
        quiet_low_coverage = self._is_low_coverage() and kpis.down_count == 0

        specs = [
            ("AVERAGE RESPONSE", _fmt_ms(kpis.avg_response_ms), COLORS["dark"], samples),
            (
                "MAXIMUM RESPONSE",
                _fmt_ms(kpis.max_response_ms),
                COLORS["dark"],
                "slowest sample" if kpis.max_response_ms is not None else samples,
            ),
            (
                "ESTIMATED DOWNTIME",
                "n/a" if quiet_low_coverage or not has_checks else f"≈ {kpis.downtime_minutes:,} min",
                COLORS["muted"] if quiet_low_coverage or not has_checks else down_color,
                "in observed checks"
                if quiet_low_coverage
                else "sum of measured outages"
                if has_checks
                else "no measurements",
            ),
        ]
        cards = [
            RoundedKPICard(
                label=label,
                value=value,
                width=card_width,
                height=24 * mm,
                value_color=color,
                font=self.font,
                font_bold=self.font_bold,
                value_size=19,
                label_size=8,
                hint=hint,
            )
            for label, value, color, hint in specs
        ]
        table = Table([cards], colWidths=[card_width + gap, card_width + gap, card_width])
        table.setStyle(
            _table_style(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        table.hAlign = "LEFT"
        self.elements.append(table)

    def _add_sla_compliance(self):
        """Add the SLA compliance table; metrics without measurements are not evaluated."""
        target = self.report_data.target
        kpis = self.kpis
        sla_availability = target.get("sla_availability_pct")
        sla_response = target.get("sla_response_time_ms")
        sla_ping = target.get("sla_ping_latency_ms")

        deviations: list[SLADeviation] = []
        if sla_availability is not None and kpis.uptime_percent is not None:
            actual = kpis.uptime_percent
            deviation = actual - sla_availability
            deviations.append(
                SLADeviation(
                    "Availability", sla_availability, actual, deviation, deviation, actual >= sla_availability, "%"
                )
            )
        if sla_response is not None and kpis.avg_response_ms is not None:
            actual = kpis.avg_response_ms
            deviation = actual - sla_response
            pct = (deviation / sla_response * 100) if sla_response > 0 else 0
            deviations.append(
                SLADeviation("Response Time", sla_response, actual, deviation, pct, actual <= sla_response, "ms")
            )
        if sla_ping is not None and kpis.avg_ping_ms is not None:
            actual = kpis.avg_ping_ms
            deviation = actual - sla_ping
            pct = (deviation / sla_ping * 100) if sla_ping > 0 else 0
            deviations.append(SLADeviation("Ping Latency", sla_ping, actual, deviation, pct, actual <= sla_ping, "ms"))

        if not deviations:
            return

        th, td = self.styles["th"], self.styles["td"]
        rows = [[Paragraph(h, th) for h in ("Metric", "SLA target", "Actual", "Deviation", "Status")]]
        for d in deviations:
            thin_availability = d.metric_name == "Availability" and self._is_low_coverage()
            metric_text = "Check success rate" if thin_availability else d.metric_name
            dev_text = (
                f"{d.deviation:+.2f} pp"
                if d.metric_name == "Availability"
                else f"{d.deviation:+.1f} {d.unit} ({d.deviation_pct:+.1f}%)"
            )
            if thin_availability and d.is_compliant:
                # Sparse data cannot prove compliance; a measured breach below keeps its number.
                dev_text = "n/a"
                status = f'<font name="{self.font_bold}" color="{COLORS["muted"].hexval()}">Not enough data</font>'
            else:
                status = (
                    f'<font name="{self.font_bold}" color="{COLORS["success_text"].hexval()}">Compliant</font>'
                    if d.is_compliant
                    else f'<font name="{self.font_bold}" color="{COLORS["danger_text"].hexval()}">Non-compliant</font>'
                )
            rows.append(
                [
                    Paragraph(metric_text, td),
                    Paragraph(f"{d.sla_target} {d.unit}", td),
                    Paragraph(f"{d.actual_value} {d.unit}", td),
                    Paragraph(dev_text, td),
                    Paragraph(status, td),
                ]
            )

        w = self.content_width
        table = Table(rows, colWidths=[w * 0.22, w * 0.18, w * 0.18, w * 0.24, w * 0.18], repeatRows=1)
        table.setStyle(
            _table_style(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), COLORS["navy"]),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [COLORS["white"], COLORS["surface"]]),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, COLORS["divider"]),
                ]
            )
        )
        self.elements.append(Paragraph("SLA Compliance", self.styles["group"]))
        self.elements.append(table)

    # Technical Summary

    def _add_technical_summary(self):
        """Technical Summary: Target Information, Certificate, TLS & Security, technology stack."""
        self._chapter_header("Technical Summary", self._period_text())

        self._group_title("Target Information")
        self._add_check_badges()
        self._add_provider_info()

        self._add_ssl_summary()
        if self._get_ssl_data() is None:
            # No TLS section for a target without a scanned HTTPS endpoint; plain HTTP is stated, not rated.
            transport = "HTTP, no TLS scan" if self._http_scheme() == "http" else "No TLS scan"
            self.elements.append(self._kv_table([("Transport", Paragraph(transport, self.styles["value"]), None)]))

        self._add_detected_stack()

    def _period_text(self) -> str:
        data = self.report_data
        return (
            f"Reporting period {_fmt_dt(data.from_date, self.tz)} – {_fmt_dt(data.to_date, self.tz)} "
            f"{_tz_label(data.from_date)}"
        )

    def _add_check_badges(self):
        """Show which checks are enabled."""
        target = self.report_data.target
        checks = [
            ("HTTP", target.get("enable_http_check"), COLORS["neutral"]),
            ("PING", target.get("enable_ping"), COLORS["neutral"]),
            ("Cert", target.get("enable_cert_expiration"), COLORS["neutral"]),
            ("TCP", target.get("enable_tcp_connect"), COLORS["neutral"]),
            ("UDP", availability_check_enabled(target, "udp", "enable_udp_scan"), COLORS["neutral"]),
        ]
        enabled = [
            Badge(text=name, font=self.font, bg_color=color, padding_y=1 * mm, shadow=False)
            for name, on, color in checks
            if on
        ]
        if enabled:
            inner = Table([enabled], colWidths=[b.badge_width + 3 * mm for b in enabled])
            inner.setStyle(
                _table_style(
                    [(a, (0, 0), (-1, -1), 0) for a in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")]
                )
            )
            inner.hAlign = "LEFT"
            value: Any = inner
        else:
            value = "—"
        self.elements.append(self._kv_table([("Checks", value, None)]))

    def _add_provider_info(self):
        """Add IP, ASN, provider and location of the target."""
        lookup_host = self._get_traceroute_host()
        if not lookup_host:
            return
        info = self._provider_info() or {}
        location = self._destination_place(with_accuracy=False)

        def cell(text: str, style: str = "token") -> Paragraph:
            return Paragraph(_esc(text if text and text != "-" else "—"), self.styles[style])

        rows = [
            ("IP Address", cell(info["ip"]), None),
            ("ASN", cell(info["asn"]), None),
            ("Provider", cell(info["provider"]), None),
            ("Location", cell(location), None),
        ]
        source = str(info.get("source") or "")
        if source:
            rows.append(("Data Source", cell({"ip-api-pro": "ip-api Pro"}.get(source, source)), None))
        accuracy = str(info.get("location_accuracy") or "")
        if accuracy:
            rows.append(("Location Accuracy", cell(accuracy), None))
        self.elements.append(self._kv_table(rows))

    def _add_detected_stack(self):
        """Add the technology stack detected by WhatWeb; missing headers are 'not tested', not 'No'."""
        stack = self._get_whatweb_data()
        if not stack:
            _log.debug("No tech stack detected for target")
            return

        def text(*keys: str) -> Paragraph:
            value = next((stack.get(k) for k in keys if stack.get(k)), None)
            return Paragraph(_esc(value or "—"), self.styles["token"])

        sec = stack.get("security") or {}

        def flag(key: str, yes: str, no: str, *, good_when: bool) -> tuple[str, str]:
            if key not in sec or sec[key] is None:
                return "Not tested", "unknown"
            present = bool(sec[key])
            return (yes if present else no), ("pass" if present == good_when else "fail")

        https, https_state = flag("https", "Yes", "No", good_when=True)
        hsts, hsts_state = flag("hsts", "Present", "Missing", good_when=True)
        xfo, xfo_state = flag("xfo", "Present", "Missing", good_when=True)
        debug, debug_state = flag("debug_headers", "Exposed", "None", good_when=False)

        rows = [
            ("Web Server", text("webserver"), None),
            ("CMS", text("cms"), None),
            ("Content Language", text("content_language", "language"), None),
            ("Country", text("country"), None),
            ("App / Runtime", text("programming_language"), None),
            ("IP", text("ip", "server_ip"), None),
            ("HTTPS", https, https_state),
            ("HSTS", hsts, hsts_state),
            ("X-Frame-Options", xfo, xfo_state),
            ("Debug Headers", debug, debug_state),
        ]
        self._group_title("Detected Technology Stack")
        self.elements.append(self._kv_table(rows))

    @staticmethod
    def _url_scheme(url: str) -> str:
        """Lower-case URL scheme, or "" when the URL cannot be parsed."""
        try:
            return urlparse(url).scheme.lower()
        except ValueError:
            return ""

    def _http_scheme(self) -> str:
        """Scheme of the configured HTTP URL ("http", "https" or "" when none or unusable)."""
        return self._url_scheme(str(self.report_data.target.get("http_url") or "").strip())

    def _get_ssl_data(self) -> TlsScanResult | None:
        """Run the TLS scan once per report and cache the outcome (also when it is None).

        The scan target comes from ``resolve_cert_target`` (cert_host/cert_port, else http_url, else
        http_port), like the live certificate check; without an http_url the target host is used over https.
        Returns None when nothing was scanned: the URL is not https and no cert_host is set, the target
        cannot be resolved, or there is no host. A scan that raised yields a ``FAILED`` result with the
        generic error "scan error" instead of None.
        """
        if self._ssl_fetched:
            return self._ssl_cache

        self._ssl_fetched = True

        target = self.report_data.target

        cert_host = target.get("cert_host")
        http_url = str(target.get("http_url") or target.get("url") or "").strip()
        if not http_url and target.get("host"):
            http_url = f"https://{target['host']}"
        if self._url_scheme(http_url) != "https":
            # Never upgrade a configured http:// to https://: only an explicit cert_host asks for a scan.
            if not cert_host:
                _log.debug("Skipping SSL scan for non-HTTPS target: %s", redact_url(http_url))
                return None
            http_url = ""  # an explicit cert_host is scanned even when the site itself is plain HTTP

        try:
            host, port = resolve_cert_target({**target, "http_url": http_url or None})
        except ValueError as exc:
            _log.warning("Skipping SSL scan: %s", exc)
            return None
        if not host:
            return None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        ssl_target = f"https://{host}" if port == 443 else f"https://{host}:{port}"
        _log.debug("TLS scan target resolved: %s", redact_url(ssl_target))

        _log.debug("Running TLS scan for %s", redact_url(ssl_target))
        try:
            self._ssl_scanned_at = datetime.now(UTC)
            self._ssl_cache = scan_tls(ssl_target, timeout=15, deadline=_PDF_TLS_SCAN_DEADLINE_SECONDS)
        except Exception as e:  # noqa: BLE001 - report generation resilience: a PDF render never aborts on an optional step
            _log.warning("TLS scan failed: %s", type(e).__name__)
            self._ssl_cache = TlsScanResult(
                host=str(urlparse(ssl_target).hostname or ""), port=0, status=ScanStatus.FAILED, error="scan error"
            )

        return self._ssl_cache

    @staticmethod
    def _verdict(check: Check, ok: str, bad: str) -> tuple[str, str]:
        """Map a check to (text, state). Unknown is never rendered as a pass."""
        if check.verdict is Verdict.SECURE:
            return ok, "pass"
        if check.verdict is Verdict.INSECURE:
            return bad, "fail"
        return "No result", "unknown"

    def _add_ssl_summary(self):
        """Add the Certificate and TLS & Security groups from the normalized scan result.

        A failed or partial scan is stated explicitly; unknown is never rendered as secure.
        """
        ssl = self._get_ssl_data()
        if ssl is None:
            _log.debug("TLS scan not applicable for target")
            return

        if ssl.status is ScanStatus.FAILED:
            self._group_title("Certificate & TLS")
            self.elements.append(
                self._kv_table(
                    [
                        (
                            "Scan",
                            Paragraph(
                                f"TLS scan failed: {_esc(ssl.error or 'unknown error')}. "
                                "No TLS security result is available; this is not a passed check.",
                                self.styles["value"],
                            ),
                            ("fail", "SCAN FAILED"),
                        )
                    ]
                )
            )
            return

        self._add_certificate_group(ssl)
        self._add_tls_group(ssl)

    def _add_certificate_group(self, ssl: TlsScanResult) -> None:
        token = self.styles["token"]
        self._group_title("Certificate")
        certificates = ssl.certificate_deployments or ((ssl.certificate,) if ssl.certificate else ())
        if not certificates:
            state = "fail" if ssl.certificate_check.verdict is Verdict.INSECURE else "unknown"
            self.elements.append(
                self._kv_table([("Certificate", Paragraph("Certificate details unavailable", token), state)])
            )
            return

        def verdict_row(label: str, check: Check, ok: str, bad: str) -> tuple[str, Paragraph, str]:
            text, state = self._verdict(check, ok, bad)
            return label, Paragraph(_esc(text), token), state

        for index, cert in enumerate(certificates, start=1):
            if len(certificates) > 1:
                self.elements.append(Paragraph(f"<b>Deployment {index}</b>", token))
            if cert.expiry.verdict is Verdict.INSECURE:
                days = ("Expired or not yet valid", "fail")
            elif cert.expiry.verdict is Verdict.UNKNOWN:
                days = ("No result", "unknown")
            else:
                days = (f"{cert.days_remaining} days", "pass" if cert.days_remaining > 30 else "warn")
            validity_state = {Verdict.SECURE: "pass", Verdict.INSECURE: "fail"}.get(cert.expiry.verdict, "unknown")
            key_text = f"{cert.key_type} {cert.key_size}-bit" if cert.key_size else cert.key_type
            sans = "<br/>".join(_esc(s) for s in cert.san) if cert.san else "none"

            rows: list[tuple[str, Any, str | None]] = [
                ("Subject", Paragraph(_esc(cert.subject), token), None),
                ("Issuer", Paragraph(_esc(cert.issuer), token), None),
                (
                    "Validity",
                    Paragraph(
                        f"{cert.valid_from:%Y-%m-%d} – {cert.valid_until:%Y-%m-%d} "
                        f"({cert.valid_from.tzname() or 'UTC'})",
                        token,
                    ),
                    validity_state,
                ),
                ("Days Remaining", Paragraph(_esc(days[0]), token), days[1]),
                verdict_row("Trusted", cert.trust, "Yes, chain validates", "No, chain does not validate"),
                verdict_row("Hostname Match", cert.hostname_match, "Matches", "Does not match"),
                verdict_row(
                    "Signature", cert.signature, cert.signature_algorithm, f"{cert.signature_algorithm} (weak hash)"
                ),
                verdict_row("Key", cert.key_strength, key_text, f"{key_text} (weak)"),
                (
                    "SANs",
                    Paragraph(
                        f"{len(cert.san)} name{'s' if len(cert.san) != 1 else ''}<br/>{sans}" if cert.san else "none",
                        token,
                    ),
                    None,
                ),
                verdict_row(
                    "Chain", cert.chain_order, f"{cert.chain_length} certificates, valid order", "Invalid order"
                ),
            ]
            self.elements.append(self._kv_table(rows))

    def _add_tls_group(self, ssl: TlsScanResult) -> None:
        """TLS result first (versions, findings, preferred cipher); the full cipher lists follow as detail."""
        token = self.styles["token"]
        self._group_title("TLS & Security")
        rows: list[tuple[str, Any, str | None]] = []

        if ssl.status is ScanStatus.PARTIAL:
            failed = ", ".join(sorted(ssl.command_errors)) or "some checks"
            rows.append(
                (
                    "Scan Status",
                    Paragraph(_esc(f"Incomplete: {failed} failed; those checks are not tested."), token),
                    ("warn", "INCOMPLETE"),
                )
            )
        elif any(check.verdict is Verdict.UNKNOWN for check in ssl.checks.values()):
            rows.append(
                (
                    "Scan Status",
                    Paragraph("Completed with inconclusive checks", token),
                    ("warn", "INCONCLUSIVE"),
                )
            )
        else:
            rows.append(
                ("Scan Status", Paragraph("All configured checks returned a result", token), ("info", "COMPLETE"))
            )
        scanned = getattr(self, "_ssl_scanned_at", None)
        when = (
            f"{_fmt_dt(_to_tz(scanned, self.tz), self.tz)} {_tz_label(self.report_data.from_date)}"
            if scanned
            else "not recorded"
        )
        rows.append(
            ("TLS Scan Time", Paragraph(_esc(f"{when} · snapshot, not part of the reporting period"), token), None)
        )

        offered = [p.name + (" (legacy)" if p.legacy else "") for p in ssl.protocols if p.supported]
        untested = [p.name for p in ssl.protocols if p.supported is None]
        versions = ", ".join(offered) or "None offered"
        if untested:
            versions += f"; not tested: {', '.join(untested)}"
        legacy_offered = any(p.legacy and p.supported for p in ssl.protocols)
        modern = ssl.modern_protocol.verdict
        state = (
            "fail"
            if legacy_offered or modern is Verdict.INSECURE
            else "pass"
            if modern is Verdict.SECURE
            else "unknown"
        )
        rows.append(("Supported Versions", Paragraph(_esc(versions), token), state))

        def check_row(label: str, check: Check, ok: str, bad: str):
            text, state = self._verdict(check, ok, bad)
            rows.append((label, Paragraph(_esc(text), token), state))

        check_row("Weak Protocols", ssl.weak_protocols, "None offered", ssl.weak_protocols.detail or "Offered")
        check_row("Heartbleed", ssl.heartbleed, "Not vulnerable", "Vulnerable")
        check_row("CCS Injection", ssl.ccs_injection, "Not vulnerable", "Vulnerable")
        check_row("ROBOT", ssl.robot, "Not vulnerable", "Vulnerable")
        check_row("TLS_FALLBACK_SCSV", ssl.fallback_scsv, "Supported", "Not supported")
        check_row("Extended Master Secret", ssl.extended_master_secret, "Supported", "Not supported")
        check_row("Secure Renegotiation", ssl.secure_renegotiation, "Supported", "Not supported")
        check_row("CRIME (compression)", ssl.compression, "Compression off", "Compression enabled")
        profile_text, profile_state = self._verdict(
            ssl.mozilla_tls_profile,
            "Compliant",
            ssl.mozilla_tls_profile.detail or "Not compliant",
        )
        if ssl.mozilla_tls_profile.verdict is Verdict.UNKNOWN and ssl.mozilla_tls_profile.detail:
            profile_text = f"No result: {ssl.mozilla_tls_profile.detail}"
        rows.append(
            (
                "Mozilla TLS Profile (Intermediate)",
                Paragraph(_esc(profile_text), token),
                profile_state,
            )
        )
        if ssl.tls_1_3_early_data is None:
            early_data = ("No result", "unknown")
        elif ssl.tls_1_3_early_data:
            early_data = ("Accepted; replay risk depends on application", "warn")
        else:
            early_data = ("Not accepted", "info")
        rows.append(("TLS 1.3 Early Data", Paragraph(_esc(early_data[0]), token), early_data[1]))
        rows.append(
            (
                "Session Resumption",
                Paragraph(_esc(ssl.session_resumption or "No result"), token),
                "info" if ssl.session_resumption else "unknown",
            )
        )
        rows.append(
            (
                "Supported Elliptic Curves",
                Paragraph(_esc(ssl.elliptic_curves or "No result"), token),
                "info" if ssl.elliptic_curves else "unknown",
            )
        )
        if ssl.http_headers is None:
            headers_state = "unknown"
        elif ssl.http_headers == "HSTS header not present" or "max-age missing" in ssl.http_headers:
            headers_state = "warn"
        elif ssl.http_headers == "No valid HTTP response":
            headers_state = "unknown"
        else:
            headers_state = "info"
        rows.append(
            (
                "HTTP Strict-Transport-Security",
                Paragraph(_esc(ssl.http_headers or "No result"), token),
                headers_state,
            )
        )
        rows.append(("Cipher Suite Example", Paragraph(_esc(ssl.cipher_suite_example or "—"), token), None))
        self.elements.append(self._kv_table(rows))

        detail: list[tuple[str, Any, str | None]] = []
        for proto in ssl.protocols:
            suites = "<br/>".join(_esc(s) for s in proto.cipher_suites)
            if proto.supported is None:
                label, state = "Not tested", "unknown"
            elif proto.supported:
                label, state = ("Offered (legacy)", "fail") if proto.legacy else ("Offered", "pass")
            else:
                label, state = ("Not offered", "pass") if proto.legacy else ("Not offered", "info")
            body = f"<b>{label}</b>" + (f"<br/>{suites}" if suites else "")
            detail.append((proto.name, Paragraph(body, token), state))
        if detail:
            self._group_title("Cipher Suites by Protocol (technical detail)")
            self.elements.append(self._kv_table(detail))

    # Network Path

    @staticmethod
    def _route_summary(display_hops: list[dict[str, Any]]) -> dict[str, Any]:
        """Count the traced hops (collapsed rows expand to their raw hops) and find the last reply."""
        traced = responding = 0
        last_hop_num = 0
        synthetic = None
        last_reply = None
        for hop in display_hops:
            if hop.get("_synthetic_destination"):
                synthetic = hop
                continue
            raw = hop.get("_raw_hops") if hop.get("_collapsed") else None
            n = len(raw) if raw else 1
            traced += n
            nums = [r.get("hop") for r in raw] if raw else [hop.get("hop")]
            last_hop_num = max([last_hop_num, *[x for x in nums if isinstance(x, int)]])
            if not hop.get("_interpolated") and hop.get("status") in ("ok", "partial"):
                responding += 1
                last_reply = hop
        return {
            "traced": traced,
            "responding": responding,
            "last_hop_num": last_hop_num,
            "synthetic": synthetic,
            "last_reply": last_reply,
        }

    def _route_incomplete(self, info: dict[str, Any]) -> bool:
        """True when the destination was not confirmed and the trace stopped at the hop limit."""
        if info["synthetic"]:
            return info["last_hop_num"] >= _ROUTE_MAX_HOPS
        return self._route_silent_at_limit

    def _build_network_path_page(self):
        """Build the Network Path chapter with the hop table."""
        display_hops, geo_points, _has_interpolation = self._get_route_data(max_hops=_ROUTE_MAX_HOPS)
        host = self._get_traceroute_host()
        if not display_hops:
            self._chapter_header("Network Path", "No traceroute data is available for this report.")
            return

        info = self._route_summary(display_hops)
        parts = []
        if host:
            parts.append(f"Destination {_esc(host)}")
        if info["synthetic"]:
            parts.append(f"address {_esc(info['synthetic'].get('ip'))}")
        parts.append(f"{info['traced']} hops traced")
        if self._route_incomplete(info):
            parts.append(ROUTE_INCOMPLETE)
        parts.append(f"{info['responding']} responding")
        reply = info["last_reply"]
        latency = _hop_latency(reply)
        if reply and latency:
            parts.append(f"RTT at last responding hop {latency:.1f} ms")
        self._chapter_header("Network Path", " · ".join(parts))
        self._add_route_map(display_hops, geo_points)
        self._add_route_schematic(display_hops)
        self._add_traceroute(display_hops, info)

    def _provider_info(self) -> dict[str, Any] | None:
        """Provider lookup of the target host, fetched once per report."""
        if not hasattr(self, "_provider_cache"):
            host = self._get_traceroute_host()
            self._provider_cache = lookup_provider_info(host) if host else None
        return self._provider_cache

    def _destination_place(self, *, with_accuracy: bool = True) -> str:
        """Destination location, always flagged as an approximate GeoIP position with its radius if known."""
        info = self._provider_info() or {}
        location = str(info.get("location") or "")
        if not location or location == "-":
            return ""
        text = gr.format_place(location, str(info.get("country_code") or ""))
        text += " · approximate GeoIP location"
        accuracy = str(info.get("location_accuracy") or "")
        return f"{text} · {accuracy}" if accuracy and with_accuracy else text

    def _route_model(self, display_hops: list[dict[str, Any]]) -> dict[str, Any]:
        info = self._provider_info() or {}
        synthetic = next((h.get("ip") for h in display_hops if h.get("_synthetic_destination")), None)
        ip = synthetic or info.get("ip")
        target_ip = ip if ip and ip != "-" else None
        return _build_route_entries(display_hops, target_ip, self._destination_place())

    def _add_route_map(self, display_hops: list[dict[str, Any]], geo_points: list[dict[str, Any]]):
        """Geographic route map above the diagram; it plots the same points as the web map."""
        model = build_route_map_model(geo_points, display_hops)
        if model is None:
            self.elements.extend([Paragraph(NO_MAP_NOTE, self.styles["note"]), Spacer(1, 2 * mm)])
            return
        self.elements.extend([RouteMap(model, self.font, self.font_bold, self.content_width), Spacer(1, 3 * mm)])

    def _add_route_schematic(self, display_hops: list[dict[str, Any]]):
        """Add the directed vertical route diagram before the full hop table."""
        model = self._route_model(display_hops)
        if not model["entries"]:
            return
        diagram = RouteDiagram(
            model["header"], model["entries"], self.font, self.font_bold, self.content_width, flags=self._flags
        )
        # No KeepTogether: a long route splits between hops and marks the continuation
        self.elements.extend([diagram, Spacer(1, 3 * mm)])

    def _status_badge(self, text: str, kind: str) -> Badge:
        return self._state_badge(kind, text)

    def _add_traceroute(
        self,
        display_hops: list[dict[str, Any]] | None = None,
        info: dict[str, Any] | None = None,
    ):
        """Add the hop table; the header repeats on every page."""
        if display_hops is None:
            display_hops, _geo, _interp = self._get_route_data(max_hops=_ROUTE_MAX_HOPS)
        if not display_hops:
            return
        info = info or self._route_summary(display_hops)
        st = self.styles
        hops = expand_sequence_hops(display_hops)
        td_muted_loc = st["td_muted"]

        rows: list[list[Any]] = [
            [
                Paragraph("Hop", st["th"]),
                Paragraph("Host / Address", st["th"]),
                Paragraph("Location", st["th"]),
                Paragraph("RTT", st["th_right"]),
                Paragraph("Status", st["th"]),
            ]
        ]
        dest_place = self._destination_place()
        reached = self._route_model(display_hops)["reached"] == "reached"
        last_index = len(hops) - 1
        for index, hop in enumerate(hops):
            state = timeline_measurement(hop)
            silent = state != "measured"  # timeout, interpolated or synthetic destination: nothing measured
            synthetic = state == "unreached"
            status = str(hop.get("status") or "timeout").lower()
            muted = silent
            td = st["td_muted"] if muted else st["td"]
            td_right = st["td_muted_right"] if muted else st["td_right"]

            hop_num = str(hop.get("hop", "?"))
            ip_raw = hop.get("ip_display") or hop.get("ip")
            ip = str(ip_raw) if ip_raw and ip_raw not in NO_IP_MARKERS else "—"
            hostname = hop.get("hostname_display") or hop.get("hostname")
            hostname = str(hostname) if hostname and hostname not in ("-", "—") and hostname != ip else ""
            if index == 0:
                hostname = f"{hostname} · {FIRST_HOP_LABEL.lower()}".strip(" ·")
            if synthetic:
                hostname = f"{hostname} · destination did not reply to probes".strip(" ·")
            path_html = _esc(ip)
            if hostname:
                # A short address and name share one line, which keeps a 13-hop table on one page.
                sep = " · " if len(ip) + len(hostname) <= _HOP_ONE_LINE_MAX else "<br/>"
                host_color = COLORS["muted"].hexval()
                path_html = f'{path_html}{sep}<font size="8" color="{host_color}">{_esc(hostname)}</font>'
            path = Paragraph(path_html, st["hop_ip_muted"] if muted else st["hop_ip"])

            city, country = hop.get("city") or "", hop.get("country") or ""
            approx_destination = index == last_index and bool(dest_place) and (reached or synthetic)
            if approx_destination:
                location = dest_place.split(" · ")[0] + " (approx.)"
            elif silent:
                location = "—"  # no reply: nothing to locate, same dash as host
            elif not (city or country):
                location = LOCATION_UNKNOWN
            else:
                location = f"{city} ({country})" if city and country else city or _place("", country)

            lat = _hop_latency(hop)
            latency = f"{lat:.1f} ms" if lat and not silent else "—"  # interpolated values are not measurements

            if synthetic:
                badge = self._status_badge(STATE_NOT_REACHED, "blocked")
            elif silent:
                badge = self._status_badge(STATE_NO_REPLY, "blocked")
            elif status in ("ok", "partial"):
                badge = self._status_badge(status.upper(), status)
            else:
                badge = self._status_badge(status.upper()[:10], "blocked")

            muted_location = location in (LOCATION_UNKNOWN, "—") or not (city or approx_destination)
            location_cell = Paragraph(_esc(location), td_muted_loc if muted_location else td)
            flag = self._flags.get(normalize_country_code(country) or "")
            if flag is not None and city and not silent and not approx_destination:
                location_cell = FlagLabel(flag, location_cell)
            rows.append(
                [
                    Paragraph(_esc(hop_num), td),
                    path,
                    location_cell,
                    Paragraph(latency, td_right),
                    badge,
                ]
            )

        note = GEOIP_FOOTNOTE
        if any(timeline_measurement(h) != "measured" for h in hops):
            note += f" {STATE_NO_REPLY}: no answer to the probes, so RTT and location are not available."
        last_hop_row = len(rows) - 1
        # The note is a table row bound to the last hop row, so it never ends up alone on a page.
        rows.append([Paragraph(_esc(note), st["note"]), *[""] * 4])

        w = self.content_width
        fixed = [11 * mm, 38 * mm, 17 * mm, 25 * mm]
        col_widths = [fixed[0], w - sum(fixed), *fixed[1:]]
        table = LongTable(rows, colWidths=col_widths, repeatRows=1, splitByRow=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), COLORS["navy"]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, 0), 4),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, last_hop_row), [COLORS["white"], COLORS["surface"]]),
            ("LINEBELOW", (0, 1), (-1, last_hop_row), 0.4, COLORS["divider"]),
            ("BOX", (0, 0), (-1, last_hop_row), 0.5, COLORS["divider"]),
            ("NOSPLIT", (0, last_hop_row), (-1, -1)),
        ]
        for note_row in range(last_hop_row + 1, len(rows)):
            style += [
                ("SPAN", (0, note_row), (-1, note_row)),
                ("LEFTPADDING", (0, note_row), (-1, note_row), 0),
                ("VALIGN", (0, note_row), (-1, note_row), "TOP"),
            ]
        table.setStyle(_table_style(style))
        self.elements.append(PageBreak())
        self.elements.append(table)

        if self._route_incomplete(info):
            self.elements.append(
                Paragraph(
                    f"<b>{ROUTE_INCOMPLETE}.</b> The destination was not reached within {_ROUTE_MAX_HOPS} hops; "
                    "the route may continue beyond this limit and is not shown completely.",
                    _paragraph_style(
                        "RouteIncomplete",
                        parent=st["note"],
                        textColor=COLORS["warning_text"],
                        backColor=COLORS["warning_soft"],
                        borderPadding=5,
                        spaceBefore=8,
                    ),
                )
            )

    def _get_traceroute_host(self) -> str | None:
        """Pick a stable traceroute host for this report."""
        target = self.report_data.target
        http_url = target.get("http_url", "")
        if http_url:
            try:
                parsed = urlparse(http_url)
                if parsed.hostname:
                    return parsed.hostname
            except ValueError:
                pass

        return target.get("ping_host") or target.get("host") or target.get("cert_host")

    def _get_route_data(self, *, max_hops: int = _ROUTE_MAX_HOPS) -> tuple[list[dict[str, Any]], list[dict], bool]:
        """Trace once per report build and cache the result.

        Returns:
            Tuple of (display_hops, geo_points, has_interpolation)
        """
        host = self._get_traceroute_host()
        if not host:
            return [], [], False

        cache_key = (host, max_hops)
        if cache_key in self._route_cache:
            return self._route_cache[cache_key]

        display_hops, geo_points, has_interpolation = _run_traceroute_with_interpolation(host)

        tail = display_hops[-1] if display_hops else {}
        self._route_silent_at_limit = (
            tail.get("status") == "timeout"
            and not tail.get("_interpolated")
            and self._route_summary(display_hops)["last_hop_num"] >= max_hops
        )

        # An empty route is cached too, so a silent target does not rerun the trace under the PDF lock.
        result = (display_hops, geo_points, has_interpolation) if display_hops else ([], [], False)
        self._route_cache[cache_key] = result
        return result

    # Availability & Performance

    def _chart_width_in(self) -> float:
        return self.content_width / 72.0

    def _build_charts_page(self, *, page_break: bool = True):
        """Build the Availability & Performance chapter (may span several pages)."""
        data = self.report_data
        target = data.target
        self._chapter_header(
            "Availability & Performance",
            f"{self._period_text()}. A gap in a chart means no measurement was recorded; it is not a zero value.",
        )

        response_label = ping_label = None
        if target.get("sla_enabled"):
            sla_response_ms = target.get("sla_response_time_ms")
            sla_ping_ms = target.get("sla_ping_latency_ms")
            # Unconfigured thresholds fall back to reference defaults that are not an agreed SLA
            if sla_response_ms is None:
                sla_response_ms, response_label = (
                    _DEFAULT_RESPONSE_MS,
                    f"Default threshold ({_DEFAULT_RESPONSE_MS} ms, no SLA set)",
                )
            if sla_ping_ms is None:
                sla_ping_ms, ping_label = _DEFAULT_PING_MS, f"Default threshold ({_DEFAULT_PING_MS} ms, no SLA set)"
        else:
            sla_response_ms = sla_ping_ms = None

        width = self._chart_width_in()
        kpis = self.kpis
        added = 0
        display = data.display_data or {}
        left_in = self._shared_plot_left_in(
            width, display, c_from_default=data.from_date, sla=(sla_response_ms, sla_ping_ms)
        )

        # Few samples in a long period: show the gap once on a coverage bar, then zoom into the measured window.
        window = self._detail_window()
        c_from, c_to = window or (data.from_date, data.to_date)
        zoom = " · measured window" if window else ""
        if window:
            bar = _create_coverage_bar(
                data.uptime_data or data.http_data,
                data.from_date,
                data.to_date,
                width=width,
                interval_seconds=data.target.get("interval_seconds") or 60,
                plot_left_in=left_in,
            )
            if bar:
                self._add_chart_section(
                    "Data Coverage",
                    bar,
                    f"Checks cover {kpis.coverage_pct:g}% of the reporting period. {self._coverage_gap_text()} "
                    f"The charts below show only the measured window "
                    f"{_fmt_dt(c_from, self.tz)} – {_fmt_dt(c_to, self.tz)}.",
                )
                added += 1

        uptime_chart = _create_uptime_chart(
            data.uptime_data,
            from_date=c_from,
            to_date=c_to,
            width=width,
            height=1.0 if window else 1.45,
            plot_left_in=left_in,
        )
        if uptime_chart:
            rate = _fmt_pct(kpis.uptime_percent)
            if self._is_low_coverage():
                rate += f" of recorded checks, {kpis.coverage_pct:g}% coverage"
            note = f"{kpis.up_count:,} of {kpis.total_checks:,} checks up ({rate})"
            note += f" · {len(self.downtime_events)} downtime event{'s' if len(self.downtime_events) != 1 else ''}"
            self._add_chart_section(
                "Availability Timeline" + zoom,
                uptime_chart,
                note + ". Short outages are drawn with a minimum visible width.",
            )
            added += 1

        line_height = 1.4 if window else 2.5
        response_chart = _create_response_time_chart(
            display.get("http_ms", data.http_data),
            from_date=c_from,
            to_date=c_to,
            width=width,
            height=line_height,
            sla_threshold_ms=int(sla_response_ms) if sla_response_ms else None,
            threshold_label=response_label,
            plot_left_in=left_in,
        )
        if response_chart:
            self._add_chart_section(
                "Response Time" + zoom,
                response_chart,
                self._latency_note(data.http_data, display.get("http_ms"), self.tz, sla_response_ms),
            )
            added += 1

        if data.status_code_data and self._add_status_codes(width):
            added += 1
        if target.get("enable_ping") and data.ping_data:
            ping_chart = _create_ping_chart(
                display.get("ping_ms", data.ping_data),
                from_date=c_from,
                to_date=c_to,
                width=width,
                height=line_height,
                sla_threshold_ms=int(sla_ping_ms) if sla_ping_ms else None,
                threshold_label=ping_label,
                plot_left_in=left_in,
            )
            if ping_chart:
                self._add_chart_section(
                    "Ping Latency" + zoom,
                    ping_chart,
                    self._latency_note(data.ping_data, display.get("ping_ms"), self.tz, sla_ping_ms),
                )
                added += 1

        if target.get("enable_tcp_connect"):
            tcp_chart = None
            note = None
            if data.tcp_port_data:
                display_ports = {
                    port: display.get(f"tcp_ms_{port}", points) for port, points in data.tcp_port_data.items()
                }
                tcp_chart = _create_multi_port_tcp_chart(
                    display_ports, from_date=c_from, to_date=c_to, width=width, plot_left_in=left_in
                )
                note = self._multi_port_note(display_ports, self.tz)
            if not tcp_chart and data.tcp_data:
                tcp_chart = _create_tcp_chart(
                    display.get("tcp_ms", data.tcp_data),
                    from_date=c_from,
                    to_date=c_to,
                    width=width,
                    plot_left_in=left_in,
                )
                note = self._latency_note(data.tcp_data, display.get("tcp_ms"), self.tz)
            if tcp_chart:
                self._add_chart_section("TCP Latency" + zoom, tcp_chart, note)
                added += 1

        if not added:
            self.elements.append(Spacer(1, 12 * mm))
            self.elements.append(
                Paragraph("No measurement data is available for this reporting period.", self.styles["empty_text"])
            )
        if page_break:
            self.elements.append(PageBreak())

    def _shared_plot_left_in(
        self, width: float, display: dict, *, c_from_default: datetime, sla: tuple[float | None, float | None]
    ) -> float:
        """Widest Y-axis gutter among the chapter's charts, so every chart's plot area starts at the same x."""
        data = self.report_data
        window = self._detail_window()
        from_date = window[0] if window else c_from_default
        gutters = [
            _latency_plot_left_in(
                display.get("http_ms", data.http_data), from_date, int(sla[0]) if sla[0] else None, width
            )
        ]
        if data.target.get("enable_ping") and data.ping_data:
            gutters.append(
                _latency_plot_left_in(
                    display.get("ping_ms", data.ping_data), from_date, int(sla[1]) if sla[1] else None, width
                )
            )
        if data.target.get("enable_tcp_connect"):
            if data.tcp_port_data:
                ports = {port: display.get(f"tcp_ms_{port}", pts) for port, pts in data.tcp_port_data.items()}
                ports = {port: pts for port, pts in ports.items() if pts}
                if ports:
                    gutters.append(_left_margin(_port_scales(ports, _report_tz(from_date))[2], width, False) * width)
            if data.tcp_data:
                gutters.append(_latency_plot_left_in(display.get("tcp_ms", data.tcp_data), from_date, None, width))
        return max(gutters)

    def _detail_window(self) -> tuple[datetime, datetime] | None:
        """Time span of the recorded samples when low coverage leaves most of the period empty."""
        data = self.report_data
        if not self._is_low_coverage():
            return None
        stamps: list[datetime] = []
        series = [data.uptime_data, data.http_data, data.ping_data, data.tcp_data, *(data.tcp_port_data or {}).values()]
        for points in series:
            ts, vals = _parse_series(points, self.tz)
            stamps.extend(t for t, v in zip(ts, vals, strict=True) if not math.isnan(v))
        if not stamps:
            return None
        first, last = min(stamps), max(stamps)
        period = data.to_date - data.from_date
        if last - first > period * 0.5:
            return None  # scattered samples: zooming would not reveal more
        pad = max((last - first) * 0.05, timedelta(minutes=30))
        return max(first - pad, data.from_date), min(last + pad, data.to_date)

    def _add_status_codes(self, width: float) -> bool:
        """HTTP status codes: a compact share bar for a single class, the donut chart for a mix."""
        data = self.report_data
        counts = _status_counts(data.status_code_data)
        if not counts:
            return False
        unit = "checks" if data.status_counts_exact else "samples"
        note = (
            "Counts cover retained raw HTTP checks only; historical status rollups are excluded."
            if data.status_counts_exact
            else "Counts are stored samples; history may be rolled up, so they are not exact per-check counts."
        )
        if len(counts) > 1:
            chart = _create_status_code_chart(data.status_code_data, width=width, exact=data.status_counts_exact)
            if chart:
                self._add_chart_section("HTTP Status Codes", chart, note)
            return bool(chart)
        (code, count), total = next(iter(counts.items())), sum(counts.values())
        color = colors.HexColor(_STATUS_CLASS_COLORS[code // 100])
        label = f"HTTP {code}" if code else "Error"
        summary = f"<b>{_esc(label)}</b> {_esc(_http_class_label(code))}: {count:,} of {total:,} {unit} (100%)"
        self.elements.append(
            KeepTogether(
                [
                    Paragraph("HTTP Status Codes", self.styles["chart_title"]),
                    Paragraph(summary, self.styles["value"]),
                    Spacer(1, 1.5 * mm),
                    ShareBar([(color, 1.0)], self.content_width),
                    Paragraph(_esc(note), self.styles["chart_note"]),
                ]
            )
        )
        return True

    @staticmethod
    def _latency_note(
        points: list[dict],
        display_points: list[dict] | None = None,
        tz: tzinfo = UTC,
        sla_ms: float | None = None,
    ) -> str | None:
        """Caption of a latency chart: summary statistics plus what the chart shows beyond them."""
        values = _valid_values(points)
        if not values:
            return None
        reported_max = max(values)
        average = sum(values) / len(values)
        note = f"Average {average:,.0f} ms · maximum {reported_max:,.0f} ms · {len(values):,} measurements"
        if display_points:
            view = _build_view(display_points, tz)
            extra = [_peak_text(p, tz) for p in view.peaks[:3]]
            if extra:
                note += ". Highest peaks within an interval (exact time unknown): " + "; ".join(extra)
                if len(view.peaks) > len(extra):
                    note += f"; {len(view.peaks) - len(extra)} further not listed"
            if view.highest_max is not None and view.highest_max > reported_max + 0.5:
                note += (
                    f". The interval maxima include all retained checks and reach {_fmt_ms_value(view.highest_max)}, "
                    "above the maximum of the plotted sample series"
                )
            if view.no_max_intervals and view.has_intervals:
                note += (
                    f". Peaks are unavailable for {view.no_max_intervals:,} of {view.intervals:,} intervals "
                    "(rolled-up history)"
                )
            elif view.no_max_intervals:
                note += ". Peaks are unavailable for this period (rolled-up history)"
            top, _ = _axis_scale([view], sla_ms)
            over = _overflow_values(view, top)
            if over:
                plural = "s" if len(over) != 1 else ""
                note += (
                    f". {len(over)} value{plural} above the axis, highest {_fmt_ms_value(max(over))}, "
                    "marked at the top edge"
                )
        return note

    @staticmethod
    def _multi_port_note(port_data: dict[int, list[dict]], tz: tzinfo) -> str:
        """Caption of the per-port TCP chart, including the real scale mode and the highest peaks."""
        views = {port: _build_view(points, tz) for port, points in sorted(port_data.items()) if points}
        if not views:
            return "One panel per port."
        shared = _ports_share_scale(list(views.values()))
        note = "One panel per port, shared time axis and " + (
            "Y scale." if shared else "independent Y scales (port latencies differ too much for one scale)."
        )
        peaks = sorted(((port, p) for port, v in views.items() for p in v.peaks[:1]), key=lambda i: -i[1].value)[:3]
        if peaks:
            note += " Highest peaks within an interval (exact time unknown): " + "; ".join(
                f"port {port} {_peak_text(p, tz)}" for port, p in peaks
            )
            note += "."
        if any(v.no_max_intervals for v in views.values()):
            note += " Peaks are unavailable for intervals of rolled-up history."
        scales = {port: _axis_scale(list(views.values()) if shared else [v], target=3)[0] for port, v in views.items()}
        if any(_overflow_values(v, scales[port]) for port, v in views.items()):
            note += " Values above a panel's axis are marked at its top edge."
        return note

    def _add_chart_section(self, title: str, chart_bytes: bytes, note: str | None = None, max_height_mm: float = 175):
        """Add a chart at full content width with its title and a data-backed caption, kept together."""
        img = Image(io.BytesIO(chart_bytes))
        aspect = img.imageWidth / img.imageHeight
        img.drawWidth = self.content_width
        img.drawHeight = self.content_width / aspect
        max_height = max_height_mm * mm
        if img.drawHeight > max_height:
            img.drawHeight = max_height
            img.drawWidth = max_height * aspect
        img.hAlign = "LEFT"

        block: list[Any] = [Paragraph(_esc(title), self.styles["chart_title"]), img]
        block.append(Paragraph(_esc(note) if note else "", self.styles["chart_note"]))
        self.elements.append(KeepTogether(block))

    # Downtime Log

    def _build_downtime_page(self):
        """Build the stand-alone Downtime Log chapter."""
        events = self.downtime_events
        kpis = self.kpis
        if events:
            plural = "s" if len(events) != 1 else ""
            subtitle = (
                f"{len(events)} downtime event{plural} · estimated durations · "
                f"all times in {_tz_label(self.report_data.from_date)}"
            )
        else:
            subtitle = self._period_text()
        self._chapter_header("Downtime Log", subtitle)

        if not events:
            self._downtime_empty_state(has_data=kpis.total_checks > 0)
            return

        st = self.styles
        header = [
            Paragraph(h, st["th"])
            for h in (
                "#",
                f"Start ({_tz_label(self.report_data.from_date)})",
                f"End ({_tz_label(self.report_data.from_date)})",
                "Est. duration",
                "Status",
            )
        ]
        rows: list[list[Any]] = [header]
        last_sample = self._last_uptime_sample()
        for i, event in enumerate(events, start=1):
            if event.end is None:
                end_text = "—"
                minutes = int((last_sample - _to_tz(event.start, self.tz)).total_seconds() / 60)
                duration = f"≥ {_fmt_duration(max(minutes, 0))}"
                badge = self._small_badge("ONGOING", COLORS["danger"])
            else:
                end_text = _fmt_dt(event.end, self.tz)
                duration = _fmt_duration(event.duration_minutes)
                badge = self._small_badge("RESOLVED", COLORS["success"])
            rows.append(
                [
                    Paragraph(str(i), st["td_muted"]),
                    Paragraph(_fmt_dt(event.start, self.tz), st["td"]),
                    Paragraph(end_text, st["td"]),
                    Paragraph(duration, st["td"]),
                    badge,
                ]
            )

        last_event_row = len(rows) - 1
        # The note is a table row bound to the last event, so it never ends up alone on a page.
        rows.append(
            [
                Paragraph(
                    "Estimated downtime: durations run from the first failed check to the next successful check "
                    "and are not exact measured outage durations. Periods without measurements are not counted "
                    "as downtime. An ongoing outage is shown as at least the time up to the last sample.",
                    st["note"],
                ),
                *[""] * 4,
            ]
        )
        w = self.content_width
        table = LongTable(
            rows,
            colWidths=[w * 0.08, w * 0.27, w * 0.27, w * 0.2, w * 0.18],
            repeatRows=1,
            splitByRow=1,
        )
        table.setStyle(
            _table_style(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), COLORS["navy"]),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, 0), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
                    ("ROWBACKGROUNDS", (0, 1), (-1, last_event_row), [COLORS["white"], COLORS["surface"]]),
                    ("LINEBELOW", (0, 1), (-1, last_event_row), 0.4, COLORS["divider"]),
                    ("BOX", (0, 0), (-1, last_event_row), 0.5, COLORS["divider"]),
                    ("NOSPLIT", (0, last_event_row), (-1, -1)),
                    ("SPAN", (0, -1), (-1, -1)),
                    ("LEFTPADDING", (0, -1), (-1, -1), 0),
                    ("VALIGN", (0, -1), (-1, -1), "TOP"),
                ]
            )
        )
        self.elements.append(table)

    def _last_uptime_sample(self) -> datetime:
        """Time of the newest availability sample: the proven end of an ongoing outage."""
        last = _latest_uptime_sample(self.report_data.uptime_data, self.tz, self.report_data.to_date)
        return last if last is not None else _to_tz(self.report_data.to_date, self.tz)

    def _add_downtime_summary(self) -> None:
        """Compact downtime summary at the end of the performance page (no incidents recorded)."""
        heading = Paragraph("Downtime Log", self.styles["chart_title"])
        heading.chapter_name = "Downtime Log"
        heading.keepWithNext = 1
        self.elements.append(heading)
        self._downtime_empty_state(has_data=self.kpis.total_checks > 0, compact=True)

    def _downtime_empty_state(self, *, has_data: bool, compact: bool = False) -> None:
        """Calm empty state; it never claims a clean record when nothing was measured or coverage is thin."""
        st = self.styles
        if has_data:
            icon: Flowable = SuccessCheckCircle(size=(9 if compact else 22) * mm)
            thin = self._is_low_coverage()
            title = "No downtime detected in observed checks" if thin else "No downtime recorded"
            text = (
                f"All {self.kpis.total_checks:,} recorded checks succeeded, covering {self.kpis.coverage_pct:g}% "
                "of the reporting period. Downtime outside the recorded checks cannot be ruled out."
                if thin
                else f"All {self.kpis.total_checks:,} availability checks in this reporting period succeeded."
            )
            title_style = st["empty_title"]
        else:
            icon = Spacer(1, 1)
            title = "No availability data"
            text = "No availability measurements exist for this period, so downtime cannot be assessed."
            title_style = _paragraph_style("EmptyTitleNeutral", parent=st["empty_title"], textColor=COLORS["muted"])
        if compact:
            left = _paragraph_style("DowntimeTitle", parent=title_style, fontSize=11.5, leading=15, alignment=0)
            note = _paragraph_style("DowntimeText", parent=st["empty_text"], fontSize=9, leading=12, alignment=0)
            table = Table(
                [[icon, [Paragraph(_esc(title), left), Paragraph(_esc(text), note)]]],
                colWidths=[14 * mm, self.content_width - 14 * mm],
            )
            table.setStyle(
                _table_style(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("BACKGROUND", (0, 0), (-1, -1), COLORS["surface"]),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            self.elements.append(table)
            return
        table = Table(
            [
                [icon],
                [Spacer(1, 4 * mm)],
                [Paragraph(_esc(title), title_style)],
                [Spacer(1, 1 * mm)],
                [Paragraph(_esc(text), st["empty_text"])],
            ],
            colWidths=[self.content_width],
        )
        table.setStyle(_table_style([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        self.elements.append(Spacer(1, 45 * mm))
        self.elements.append(table)


# Public API


def generate_pdf_report(report_data: ReportData, page_size: str = "a4") -> bytes:
    """
    Generate a PDF report for a target.

    Args:
        report_data: Container with all data needed for the report.
        page_size: "a4" or "letter".

    Returns:
        PDF file as bytes.

    Note: This function is serialized with a global lock (Task 6).
    matplotlib is not reentrant - parallel PDF generation can corrupt charts/fonts.
    """
    builder = PDFReportBuilder(report_data, page_size)
    builder.prefetch_external_data()
    with _PDF_LOCK:
        return builder.build()
