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
from typing import Any
from urllib.parse import urlparse, urlunparse
from xml.sax.saxutils import escape as _xml_escape

import matplotlib as mpl

mpl.use("Agg")  # Non-interactive backend for server
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Rectangle
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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

from app.services.mtr_run import analyze_route, resolve_target_ip, run_mtr
from app.services.provider_lookup import lookup_provider_info
from app.services.route_interpolator import (
    collapse_interpolated_hops,
    ensure_destination_hop,
    interpolate_route,
)
from app.services.traceroute_geo import (
    geolocate_with_hostname_hint,
    mtr_hops_to_geo_points,
)
from app.utils.ssrf import redact_url
from app.utils.tls_model import Check, ScanStatus, TlsScanResult, Verdict
from app.utils.tls_scanner import scan_tls
from app.utils.version import BUILD_INFO, VERSION
from app.utils.whatweb_parser import run_whatweb

_log = logging.getLogger(__name__)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _esc(value: Any) -> str:
    """Escape external text for ReportLab Paragraph markup (strips control chars)."""
    text = _CONTROL_CHARS_RE.sub(" ", str(value if value is not None else ""))
    return _xml_escape(text)


# ─── PDF Generation Lock (Task 6: Thread Safety) ────────────────────────────
# matplotlib is not thread-safe for parallel plot generation.
# Serialize all PDF generation to prevent chart/font corruption.
_PDF_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FONT_DIR = Path(__file__).parent.parent / "static" / "fonts"
STATIC_DIR = Path(__file__).parent.parent / "static"

# Colors matching the justUp theme
COLORS = {
    "primary": colors.HexColor("#198754"),
    "dark": colors.HexColor("#212529"),
    "muted": colors.HexColor("#6c757d"),
    "light": colors.HexColor("#f8f9fa"),
    "white": colors.white,
    "divider": colors.HexColor("#e9ecef"),
    "danger": colors.HexColor("#dc3545"),
    "warning": colors.HexColor("#ffc107"),
    "success": colors.HexColor("#198754"),
    "info": colors.HexColor("#0dcaf0"),
    # Print palette: dark blue structure, green/yellow/red for state
    "navy": colors.HexColor("#12294a"),
    "navy_mid": colors.HexColor("#2b4a7a"),
    "navy_soft": colors.HexColor("#e8eef7"),
    "surface": colors.HexColor("#f4f6fa"),
    "success_soft": colors.HexColor("#e6f4ec"),
    "success_text": colors.HexColor("#146c43"),
    "warning_soft": colors.HexColor("#fff4d6"),
    "warning_text": colors.HexColor("#8a5a00"),
    "danger_soft": colors.HexColor("#fdeaec"),
    "danger_text": colors.HexColor("#b02a37"),
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

# ---------------------------------------------------------------------------
# Spacing Design Tokens (consistent layout rhythm)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Provider Info Lookup
# ---------------------------------------------------------------------------


def _run_mtr_with_interpolation(
    host: str,
    max_hops: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Run MTR with route interpolation for ICMP-blocked segments.

    Returns:
        Tuple of (display_hops, geo_points, has_interpolation)
    """
    # Resolve target IP for destination matching
    target_ip = resolve_target_ip(host)

    # Run MTR
    force_ipv6 = None
    if target_ip:
        force_ipv6 = ":" in target_ip
    elif host and ":" in host:
        force_ipv6 = True
    hops = run_mtr(host, count=3, max_hops=max_hops, force_ipv6=force_ipv6)
    if not hops:
        return [], [], False

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
                    lat = h.get("latency_ms")
                    if lat and isinstance(lat, dict):
                        final_latency = lat.get("avg")
                    break

        interpolated_hops = ensure_destination_hop(
            interpolated_hops,
            destination_ip=target_ip,
            destination_hostname=host,
            destination_geo=destination_geo,
            final_latency_ms=final_latency,
        )

    # Generate geo points for map
    geo_points = mtr_hops_to_geo_points(
        interpolated_hops,
        destination_ip=target_ip,
        destination_geo=destination_geo,
    )

    # Merge geo data back into hops for display (city, country)
    # Similar to frontend enrichment in frontend.py
    geo_by_ip: dict[str, dict] = {}
    for p in geo_points:
        geo_by_ip.setdefault(p.get("ip"), p)

    enriched_hops = []
    for h in interpolated_hops:
        hop_data = dict(h)
        ip = h.get("ip")
        geo = geo_by_ip.get(ip) if ip else None

        # If not in geo_points (deduplicated), resolve directly
        if not geo and ip and ip not in ("*", "-", "???", "—"):
            geo = geolocate_with_hostname_hint(ip, h.get("hostname"))

        if geo:
            hop_data["city"] = geo.get("city")
            hop_data["country"] = geo.get("country")

        enriched_hops.append(hop_data)

    # Collapse consecutive interpolated hops for cleaner table
    display_hops = collapse_interpolated_hops(enriched_hops)

    # Check if any interpolation was applied
    has_interpolation = any(h.get("status") == "interpolated" or h.get("_interpolated") for h in interpolated_hops)

    return display_hops, geo_points, has_interpolation


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Font Registration
# ---------------------------------------------------------------------------

_fonts_registered = False
_fonts_lock = threading.Lock()


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


# ---------------------------------------------------------------------------
# Matplotlib Chart Generation
# ---------------------------------------------------------------------------

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

        # 2. Apply base style (with fallback for matplotlib version changes)
        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except OSError:
            _log.debug("seaborn-v0_8-whitegrid not available, using default style")
            plt.style.use("default")

        # IMPORTANT: seaborn resets rcParams → force IBM Plex Sans AFTER style

        # 3. Set rcParams AFTER style (style resets rcParams!)
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


def _plot_series(ax, ts: list[datetime], vals: list[float], color: str) -> Any:
    """Plot a measured series: gaps stay open, isolated samples get a marker."""
    bt, bv = _break_gaps(ts, vals)
    ax.fill_between(bt, bv, alpha=0.14, color=color, linewidth=0)
    (line,) = ax.plot(bt, bv, color=color, linewidth=1.1, label="Measured")
    iso_t = [
        bt[i]
        for i in range(len(bt))
        if not math.isnan(bv[i]) and (i == 0 or math.isnan(bv[i - 1])) and (i == len(bt) - 1 or math.isnan(bv[i + 1]))
    ]
    if iso_t:
        ax.plot(iso_t, [bv[bt.index(t)] for t in iso_t], linestyle="none", marker="o", markersize=2.5, color=color)
    return line


def _style_axes(ax, *, from_date: datetime, to_date: datetime) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, axis="y", alpha=0.6)
    ax.grid(False, axis="x")
    ax.tick_params(axis="both", length=2.5, width=0.5, labelsize=8, colors="#495057")
    _format_time_axis(ax, from_date=from_date, to_date=to_date)


def _fig_png(fig) -> bytes:
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
) -> bytes | None:
    """Create a latency time-series chart; measurements and the SLA threshold are drawn distinctly."""
    if not data:
        return None
    tz = _report_tz(from_date)
    ts, vals = _parse_series(data, tz)
    valid = [v for v in vals if not math.isnan(v)]
    if not valid:
        return None

    _setup_matplotlib_style()
    with _safe_figure(figsize=(width, height)) as (fig, ax):
        handles = [_plot_series(ax, ts, vals, color)]
        peaks = [
            (_to_tz(datetime.fromisoformat(p["ts"]), tz), float(p["max"]))
            for p in data
            if p.get("max") is not None
            and _num(p.get("max")) is not None
            and _num(p.get("value")) is not None
            and float(p["max"]) > float(p["value"]) * 2
        ]
        if peaks:
            handles.append(
                ax.scatter(
                    [t for t, _ in peaks],
                    [v for _, v in peaks],
                    color=CHART_COLORS["sla"],
                    marker="^",
                    s=14,
                    label="Raw peak",
                )
            )
        full_max = max(valid + [v for _, v in peaks])
        linear_limit = max(1.0, sorted(valid)[int((len(valid) - 1) * 0.9)] * 1.5)
        if full_max > linear_limit * 2:
            ax.set_yscale("symlog", linthresh=linear_limit)
            ylabel += f" (linear to {linear_limit:.0f}, log above)"
        y_max = full_max * 1.12 or 1.0
        if sla_threshold_ms:
            y_max = max(y_max, sla_threshold_ms * 1.12)
            handles.append(
                ax.axhline(
                    sla_threshold_ms,
                    linestyle=(0, (5, 3)),
                    linewidth=1.1,
                    color=CHART_COLORS["sla"],
                    label=threshold_label or f"SLA threshold ({sla_threshold_ms} ms)",
                )
            )
        ax.set_ylim(0, y_max)
        ax.set_ylabel(ylabel, fontsize=8, color="#495057")
        _style_axes(ax, from_date=from_date, to_date=to_date)
        ax.legend(
            handles=handles,
            loc="lower right",
            bbox_to_anchor=(1.0, 1.0),
            ncol=len(handles),
            frameon=False,
            fontsize=8,
            borderaxespad=0.2,
        )
        fig.subplots_adjust(left=0.085, right=0.96, top=0.88, bottom=0.25)
        return _fig_png(fig)


def _create_response_time_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float = 2.5,
    sla_threshold_ms: int | None = None,
    threshold_label: str | None = None,
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
    )


def _create_ping_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float = 2.5,
    sla_threshold_ms: int | None = None,
    threshold_label: str | None = None,
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
    )


def _create_tcp_chart(
    data: list[dict], from_date: datetime, to_date: datetime, width: float = 6.7, height: float = 2.5
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
    )


def _create_uptime_chart(
    data: list[dict], from_date: datetime, to_date: datetime, width: float = 6.7, height: float = 1.45
) -> bytes | None:
    """Create an availability status strip as PNG bytes.

    Up and Down intervals are drawn as blocks; periods without samples stay empty.
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
        fig.subplots_adjust(left=0.03, right=0.96, top=0.78, bottom=0.34)
        return _fig_png(fig)


def _create_multi_port_tcp_chart(
    port_data: dict[int, list[dict]],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.7,
    height: float | None = None,  # Auto-calculated based on number of ports
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

    with _safe_figure(nrows=num_ports, ncols=1, figsize=(width, height), sharex=True, squeeze=False) as (fig, axes):
        flat_axes = axes.flatten()
        valid_values = [v for _, vals in series.values() for v in vals if not math.isnan(v)]
        raw_peaks = [
            float(p["max"])
            for data in port_data.values()
            for p in data
            if p.get("max") is not None and _num(p.get("max")) is not None
        ]
        peak = max(valid_values + raw_peaks)
        y_max = max(10, int(peak * 1.15) + 1)
        linear_limit = max(1.0, sorted(valid_values)[int((len(valid_values) - 1) * 0.9)] * 1.5)

        for idx, port in enumerate(sorted_ports):
            ax = flat_axes[idx]
            ts, vals = series[port]
            color = TCP_PORT_COLORS[idx % len(TCP_PORT_COLORS)]
            _plot_series(ax, ts, vals, color)
            markers = [
                (_to_tz(datetime.fromisoformat(p["ts"]), tz), float(p["max"]))
                for p in port_data[port]
                if p.get("max") is not None
                and _num(p.get("value")) is not None
                and float(p["max"]) > float(p["value"]) * 2
            ]
            if markers:
                ax.scatter(
                    [t for t, _ in markers], [v for _, v in markers], color=CHART_COLORS["sla"], marker="^", s=10
                )
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
            ax.set_ylim(0, y_max)
            if peak > linear_limit * 2:
                ax.set_yscale("symlog", linthresh=linear_limit)
            ax.set_yticks([0, y_max // 2, y_max])
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(True, axis="y", alpha=0.6)
            ax.tick_params(axis="both", length=2.5, width=0.5, labelsize=7, colors="#495057")
            if idx == num_ports // 2:
                label = f"ms (linear ≤{linear_limit:.0f})" if peak > linear_limit * 2 else "ms"
                ax.set_ylabel(label, fontsize=8, color="#495057")

        _format_time_axis(flat_axes[-1], from_date=from_date, to_date=to_date)
        fig.subplots_adjust(left=0.085, right=0.96, top=0.95, bottom=min(0.3, 0.55 / height), hspace=0.6)
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


def _create_status_code_chart(
    data: list[dict], width: float = 6.7, height: float | None = None, *, exact: bool = False
) -> bytes | None:
    """Create an HTTP status code distribution as a labelled donut with a legend table.

    Counts are stored samples (history may be rolled up), not exact per-check counts.
    """
    status_counts: dict[int, int] = {}
    for p in data or []:
        number = _num(p.get("value"))
        if number is None:
            continue
        code = int(number)
        if code == 0 or 100 <= code <= 599:
            status_counts[code] = status_counts.get(code, 0) + int(p.get("count", 1))
    if not status_counts:
        return None

    _setup_matplotlib_style()
    codes = sorted(status_counts)
    counts = [status_counts[c] for c in codes]
    total = sum(counts)
    if height is None:
        height = max(1.9, 0.5 + 0.2 * len(codes))

    base = {0: "#7f1d1d", 1: "#6c757d", 2: "#198754", 3: "#e0a800", 4: "#e8590c", 5: "#dc3545"}
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
        ax.text(0, -0.2, "checks" if exact else "samples", ha="center", va="center", fontsize=7.5, color="#6c757d")
        ax.axis("equal")

        x0 = side / width + 0.07
        row_h = min(0.2, 0.84 / max(len(codes), 1)) * 1.0
        y = 0.5 + row_h * (len(codes) - 1) / 2 + 0.04
        fig.text(x0, min(y + row_h, 0.97), "Status code", fontsize=7.5, color="#6c757d")
        fig.text(
            x0 + 0.40, min(y + row_h, 0.97), "Checks" if exact else "Samples", fontsize=7.5, color="#6c757d", ha="right"
        )
        fig.text(x0 + 0.52, min(y + row_h, 0.97), "Share", fontsize=7.5, color="#6c757d", ha="right")
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
        fontsize=7.5,
        color="#6c757d",
        labelpad=3,
    )


# ---------------------------------------------------------------------------
# KPI Calculation
# ---------------------------------------------------------------------------


def _valid_values(points: list[dict]) -> list[float]:
    return [v for v in (_num(p.get("value")) for p in points) if v is not None]


def _availability_pct(up: int, total: int) -> float | None:
    """Availability in percent; anything below a perfect run never rounds up to 100."""
    if not total:
        return None
    pct = round(up / total * 100, 2)
    return pct if up == total else min(pct, 99.99)


# Below this coverage, availability is shown as an approximation with a caveat.
LOW_COVERAGE_PCT = 80.0


def _coverage_pct(report_data: ReportData, total_checks: int, interval_seconds: float) -> float | None:
    window = (report_data.to_date - report_data.from_date).total_seconds()
    if window <= 0:
        return None
    return round(min(100.0, total_checks * interval_seconds / window * 100), 1)


def _compute_kpis(report_data: ReportData) -> KPIMetrics:
    """Compute KPI metrics; a metric without measurements stays None (never 0).

    downtime_minutes is an estimate: failed checks times the check interval.
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
        downtime_minutes=int(down_count * interval_seconds / 60),
        total_checks=total_checks,
        up_count=up_count,
        down_count=down_count,
        avg_ping_ms=round(sum(ping) / len(ping), 1) if ping else None,
        response_samples=len(response),
        coverage_pct=_coverage_pct(report_data, total_checks, interval_seconds),
    )


def _extract_downtime_events(uptime_data: list[dict]) -> list[DowntimeEvent]:
    """Extract downtime periods from uptime data (points without a value are ignored)."""
    events: list[DowntimeEvent] = []
    in_downtime = False
    downtime_start: datetime | None = None
    skipped_count = 0  # Track invalid timestamps

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
        if value == 0 and not in_downtime:
            in_downtime = True
            downtime_start = ts
        elif value == 1 and in_downtime:
            in_downtime = False
            if downtime_start:
                duration = int((ts - downtime_start).total_seconds() / 60)
                events.append(DowntimeEvent(start=downtime_start, end=ts, duration_minutes=duration))
            downtime_start = None

    if in_downtime and downtime_start:
        events.append(DowntimeEvent(start=downtime_start, end=None, duration_minutes=None))

    if skipped_count > 0:
        _log.warning("Downtime extraction: skipped %d datapoints with invalid/missing timestamps", skipped_count)

    return events


# ---------------------------------------------------------------------------
# Custom Flowables
# ---------------------------------------------------------------------------


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
        canvas.drawCentredString(w / 2, h - 7.5 * mm, self.label)

        size = self.value_size
        max_width = w - 8 * mm
        while size > 8 and pdfmetrics.stringWidth(self.value, self.font_bold, size) > max_width:
            size -= 1
        canvas.setFont(self.font_bold, size)
        canvas.setFillColor(self.value_color)
        canvas.drawCentredString(w / 2, h / 2 - size * 0.18 - (1.5 * mm if self.hint else 0), self.value)

        if self.hint:
            canvas.setFont(self.font, 7.5)
            canvas.setFillColor(COLORS["muted"])
            canvas.drawCentredString(w / 2, 3.5 * mm, self.hint)

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
        text_width = pdfmetrics.stringWidth(self.text, self.font, font_size)
        self.badge_width = text_width + 2 * padding_x
        self.badge_height = font_size + 2 * padding_y

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab wrap() callback signature
        return self.badge_width, self.badge_height

    def draw(self):
        canvas = self.canv
        canvas.saveState()

        if self.shadow:
            _draw_soft_shadow(canvas, self.badge_width, self.badge_height, self.corner_radius)

        # Draw rounded rectangle background
        canvas.setFillColor(self.bg_color)
        canvas.roundRect(0, 0, self.badge_width, self.badge_height, self.corner_radius, fill=1, stroke=0)

        # Draw centered text (vertically centered using font metrics)
        canvas.setFont(self.font, self.font_size)
        canvas.setFillColor(self.text_color)
        # Calculate vertical center: (badge_height - font_size) / 2, adjusted for baseline
        # Font descender is typically ~20% of font size
        descender = self.font_size * 0.2
        text_y = (self.badge_height - self.font_size) / 2 + descender
        canvas.drawCentredString(self.badge_width / 2, text_y, self.text)

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

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab wrap() callback signature  # noqa: N803, ARG002 - reportlab wrap() callback signature
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


class SuccessCheckCircle(Flowable):
    """A green circle with a white checkmark inside."""

    def __init__(self, size: float = 12 * mm, bg_color=None, check_color=None):
        super().__init__()
        self.size = size
        self.bg_color = bg_color or COLORS["success"]
        self.check_color = check_color or colors.white

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab wrap() callback signature  # noqa: N803, ARG002 - reportlab wrap() callback signature
        return self.size, self.size

    def draw(self):
        canvas = self.canv
        canvas.saveState()

        # Draw filled circle
        canvas.setFillColor(self.bg_color)
        canvas.circle(self.size / 2, self.size / 2, self.size / 2, fill=1, stroke=0)

        # Draw checkmark
        canvas.setStrokeColor(self.check_color)
        canvas.setLineWidth(self.size * 0.08)  # Proportional line width
        canvas.setLineCap(1)  # Round cap
        canvas.setLineJoin(1)  # Round join

        # Checkmark coordinates (scaled to circle size)
        cx, cy = self.size / 2, self.size / 2
        scale = self.size / 12  # Base scale factor

        # Checkmark path: start from left, go down-right, then up-right
        p = canvas.beginPath()
        p.moveTo(cx - 2.5 * scale, cy - 0.2 * scale)  # Left point
        p.lineTo(cx - 0.5 * scale, cy - 2.2 * scale)  # Bottom point
        p.lineTo(cx + 3 * scale, cy + 2.3 * scale)  # Top-right point
        canvas.drawPath(p, fill=0, stroke=1)

        canvas.restoreState()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_pct(value: float | None) -> str:
    """Format an availability percentage; only a perfect run reads 100 %."""
    if value is None:
        return "n/a"
    if value >= 100:
        return "100%"
    return f"{min(value, 99.99):.2f}%"


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
    "unknown": ("muted", "unknown_soft", "muted"),
}

# Highest hop count requested from MTR; a route reaching it may be cut off.
_MTR_MAX_HOPS = 20

# Chart reference lines when SLA is enabled without an own threshold (not an agreed SLA)
_DEFAULT_RESPONSE_MS = 500
_DEFAULT_PING_MS = 100


ROUTE_INCOMPLETE = f"Route incomplete – {_MTR_MAX_HOPS}-hop limit reached"


class RouteSchematic(Flowable):
    """Compact static vector route schematic for the Network Path chapter.

    Draws the hop order as a left-to-right, wrapping row of boxes with solid
    connectors between located hops and dashed connectors across an unknown gap.
    Start and destination are distinguishable by shape and label without relying
    on color, so the schematic survives greyscale printing. It never plots a
    geographic position; it is the always-available linear fallback when there is
    no usable geodata. Nodes come from the shared route model's rules: an unknown
    hop is labeled "position unknown", a non-responding destination "no reply".
    """

    _BOX_W = 30 * mm
    _BOX_H = 13 * mm
    _GAP_X = 9 * mm
    _GAP_Y = 7 * mm

    def __init__(self, nodes: list[dict[str, Any]], font: str, font_bold: str, max_width: float):
        super().__init__()
        self.nodes = nodes
        self.font = font
        self.font_bold = font_bold
        self.max_width = max_width
        self._per_row = max(1, int((max_width + self._GAP_X) // (self._BOX_W + self._GAP_X)))
        rows = max(1, -(-len(nodes) // self._per_row))  # ceil division
        self.width = max_width
        self.height = rows * self._BOX_H + (rows - 1) * self._GAP_Y

    def wrap(self, availWidth, availHeight):  # noqa: N803, ARG002 - reportlab callback signature
        return self.width, self.height

    def _draw_box(self, canvas, x, y, node):
        kind = node.get("kind", "hop")
        is_endpoint = kind in ("start", "destination", "destination_unknown", "both")
        unknown = kind in ("unknown", "destination_unknown")
        canvas.setLineWidth(1.4 if is_endpoint else 0.8)
        canvas.setStrokeColor(COLORS["muted"] if unknown else COLORS["navy"])
        canvas.setFillColor(COLORS["surface"] if unknown else COLORS["navy_soft"])
        # Start is a square-cornered box, other nodes rounded; destination gets a
        # second outline. Shape alone (not color) separates the endpoints.
        if kind == "start":
            canvas.rect(x, y, self._BOX_W, self._BOX_H, fill=1, stroke=1)
        else:
            canvas.roundRect(x, y, self._BOX_W, self._BOX_H, 2 * mm, fill=1, stroke=1)
        if kind in ("destination", "destination_unknown", "both"):
            canvas.setFillColor(colors.white)
            canvas.roundRect(
                x + 1.2 * mm, y + 1.2 * mm, self._BOX_W - 2.4 * mm, self._BOX_H - 2.4 * mm, 1.4 * mm, fill=0, stroke=1
            )

        role = {
            "start": "START",
            "destination": "DEST",
            "destination_unknown": "DEST",
            "both": "START/DEST",
        }.get(kind, f"Hop {node.get('label', '?')}")
        canvas.setFont(self.font_bold, 7.5)
        canvas.setFillColor(COLORS["muted"] if unknown else COLORS["navy"])
        canvas.drawCentredString(x + self._BOX_W / 2, y + self._BOX_H - 5.2 * mm, role)
        if is_endpoint and node.get("label"):
            canvas.setFont(self.font, 6.5)
            canvas.drawCentredString(x + self._BOX_W / 2, y + self._BOX_H - 8.6 * mm, f"Hop {node['label']}")
        canvas.setFont(self.font, 6.5)
        canvas.setFillColor(COLORS["dark"] if not unknown else COLORS["muted"])
        secondary = node.get("location_text") or ("position unknown" if unknown else "")
        if secondary:
            canvas.drawCentredString(x + self._BOX_W / 2, y + 2.2 * mm, secondary[:28])

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        for index, node in enumerate(self.nodes):
            row = index // self._per_row
            col = index % self._per_row
            x = col * (self._BOX_W + self._GAP_X)
            # Top-down rows: first row at the top of the flowable box.
            y = self.height - self._BOX_H - row * (self._BOX_H + self._GAP_Y)
            if index > 0 and col > 0:
                self._draw_connector(canvas, x - self._GAP_X, x, y + self._BOX_H / 2, node.get("gap_before"))
            self._draw_box(canvas, x, y, node)
        canvas.restoreState()

    def _draw_connector(self, canvas, x1, x2, y, dashed):
        canvas.setStrokeColor(COLORS["muted"] if dashed else COLORS["navy"])
        canvas.setLineWidth(1.0)
        canvas.setDash([2, 2] if dashed else [])
        canvas.line(x1, y, x2, y)
        canvas.setDash([])


class _ReportDocTemplate(SimpleDocTemplate):
    """Records chapter start pages (flowables carrying `chapter_name`) and pages that hold real content."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chapter_pages: dict[str, int] = {}
        self.content_pages: set[int] = set()

    def afterFlowable(self, flowable):  # noqa: N802 - reportlab callback name
        if not isinstance(flowable, ActionFlowable | PageBreak | Spacer):
            self.content_pages.add(self.page)
        name = getattr(flowable, "chapter_name", None)
        if name:
            self.chapter_pages.setdefault(name, self.page)


# ---------------------------------------------------------------------------
# PDF Document Generation
# ---------------------------------------------------------------------------


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
        self.downtime_events = _extract_downtime_events(report_data.uptime_data)
        self.chapter_pages: dict[str, int] = {}
        self.page_count = 0
        self.content_pages: set[int] = set()
        # MTR cache: stores (display_hops, geo_points, has_interpolation)
        self._mtr_cache: dict[tuple[str, int], tuple[list, list, bool]] = {}
        # True when the trace ended in silent hops at the hop limit (destination never confirmed)
        self._mtr_silent_at_limit = False
        # WhatWeb cache: stores the tech stack dict
        self._whatweb_cache: dict | None = None
        self._whatweb_fetched: bool = False
        # TLS scan cache: normalized result (None = not applicable, e.g. non-HTTPS target)
        self._ssl_cache: TlsScanResult | None = None
        self._ssl_fetched: bool = False

    def prefetch_external_data(self) -> None:
        """Fetch WhatWeb, TLS scan, and MTR data (blocking I/O, call before build).

        This method should be called before acquiring the PDF generation lock
        to avoid blocking other PDF requests on slow external scans.
        """
        self._get_whatweb_data()
        self._get_ssl_data()
        self._get_mtr_data()

    def _create_styles(self) -> dict:
        """Create the paragraph styles used across all chapters."""

        def ps(name: str, *, bold: bool = False, **kw) -> ParagraphStyle:
            kw.setdefault("textColor", COLORS["dark"])
            return ParagraphStyle(name, fontName=self.font_bold if bold else self.font, **kw)

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

        # Page 4: Charts ("Availability & Performance"); the builder ends with a page break
        self._build_charts_page()

        # Page 5+: Downtime Log
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
            title="justUp Monitoring Report",
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

        # Get target domain
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
        # Line 1: heartbeat + justUp! v<VERSION> | © <YEAR> Gill-Bates | Lightweight Uptime Monitor
        # Line 2: Domain | Report generated at timestamp

        # Draw simple heartbeat line (stylized)
        canvas.setStrokeColor(COLORS["muted"])
        canvas.setLineWidth(0.75)
        hb_x = self.margin
        hb_y = 12 * mm
        # Simple heartbeat pattern: _/\_/\_
        canvas.line(hb_x, hb_y, hb_x + 4 * mm, hb_y)
        canvas.line(hb_x + 4 * mm, hb_y, hb_x + 5 * mm, hb_y + 2 * mm)
        canvas.line(hb_x + 5 * mm, hb_y + 2 * mm, hb_x + 6 * mm, hb_y - 2 * mm)
        canvas.line(hb_x + 6 * mm, hb_y - 2 * mm, hb_x + 7 * mm, hb_y + 1 * mm)
        canvas.line(hb_x + 7 * mm, hb_y + 1 * mm, hb_x + 8 * mm, hb_y)
        canvas.line(hb_x + 8 * mm, hb_y, hb_x + 12 * mm, hb_y)

        # justUp! Logo (40% transparency)
        logo_path = STATIC_DIR / "justup_1c.png"
        logo_height = 4 * mm  # Height for footer logo
        logo_width = logo_height * 3.5  # Approximate aspect ratio
        logo_x = hb_x + 13 * mm
        if logo_path.exists():
            canvas.saveState()
            canvas.setFillAlpha(0.6)  # 40% transparency = 60% opacity
            canvas.drawImage(
                str(logo_path),
                logo_x,
                10.0 * mm,
                width=logo_width,
                height=logo_height,
                preserveAspectRatio=True,
                anchor="sw",
                mask="auto",
            )
            canvas.restoreState()

        canvas.setFont(self.font, 7)
        canvas.setFillColor(COLORS["muted"])
        now = datetime.now(tz=UTC)
        build_suffix = f" ({BUILD_INFO[:7]})" if BUILD_INFO and BUILD_INFO != "dev" else ""
        footer_text = f"v{VERSION}{build_suffix} | © {now.year} Gill-Bates | Lightweight Uptime Monitor | "
        footer_text_x = logo_x + logo_width - 2 * mm
        canvas.drawString(footer_text_x, 11 * mm, footer_text)

        # GitHub link (after footer text)
        github_url = "https://github.com/Gill-Bates/justUp"
        footer_text_width = canvas.stringWidth(footer_text, self.font, 7)
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

        canvas.setFont(self.font, 7)
        canvas.setFillColor(COLORS["muted"])
        github_label = "GitHub"
        canvas.drawString(github_x + icon_width, 11 * mm, github_label)

        # Make it clickable (both icon and text)
        github_label_width = canvas.stringWidth(github_label, self.font, 7)
        canvas.linkURL(github_url, (github_x, 9 * mm, github_x + icon_width + github_label_width, 14 * mm), relative=0)

        # Right side: page number
        canvas.drawRightString(self.width - self.margin, 11 * mm, f"Page {doc.page}")

        # Second line: Domain and timestamp
        timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
        canvas.setFont(self.font, 6)
        right_text = f"Report generated at {timestamp}"
        room = self.content_width - canvas.stringWidth(right_text, self.font, 6) - 4 * mm
        while len(domain) > 4 and canvas.stringWidth(domain, self.font, 6) > room:
            domain = domain[:-4] + "…"
        canvas.drawString(self.margin, 6 * mm, domain)
        canvas.drawRightString(self.width - self.margin, 6 * mm, right_text)

        canvas.restoreState()

    # ─── Shared building blocks ──────────────────────────────────────────────

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
        presets = {
            "pass": ("PASS", COLORS["success"], colors.white),
            "fail": ("FAIL", COLORS["danger"], colors.white),
            "warn": ("WARNING", COLORS["warning"], COLORS["dark"]),
            "unknown": ("NOT TESTED", COLORS["unknown_soft"], COLORS["dark"]),
            "info": ("INFO", COLORS["navy_soft"], COLORS["navy"]),
        }
        label, bg, fg = presets[kind]
        return Badge(
            text=text or label,
            font=self.font,
            font_size=7,
            padding_x=2.2 * mm,
            padding_y=0.9 * mm,
            corner_radius=1.6 * mm,
            bg_color=bg,
            text_color=fg,
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
            TableStyle(
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

    # ─── Cover ───────────────────────────────────────────────────────────────

    def _status_info(self) -> tuple[str, str, str]:
        """Return (label, detail, kind) derived only from measured availability."""
        kpis = self.kpis
        if kpis.uptime_percent is None:
            return "No data", "No availability measurements were recorded in this period.", "unknown"
        ongoing = any(e.end is None for e in self.downtime_events)
        tail = " · Last check: down (outage ongoing at end of period)" if ongoing else ""
        detail = (
            f"{_fmt_pct(kpis.uptime_percent)} availability · "
            f"{kpis.down_count} of {kpis.total_checks} checks failed{tail}"
        )
        if kpis.coverage_pct is not None and kpis.coverage_pct < LOW_COVERAGE_PCT:
            detail += f" · Low data coverage ({kpis.coverage_pct:g}% of the period)"
        if kpis.uptime_percent >= 99:
            return "Operational", detail, "success"
        if kpis.uptime_percent >= 95:
            return "Degraded", detail, "warning"
        return "Critical", detail, "danger"

    def _cover_name(self) -> tuple[str, str]:
        """Return (title, secondary line): the target name, plus its host when it differs."""
        target = self.report_data.target
        host = target.get("ping_host") or target.get("host") or target.get("http_url", "")
        host = str(host).replace("https://", "").replace("http://", "").split("/")[0] if host else ""
        name = str(target.get("name") or host or "Unknown target")
        return name, ("" if host.lower() == name.lower() else host)

    def _build_cover_page(self):
        """Build the stand-alone cover: target, period, status and headline KPIs only."""
        from_date, to_date = self.report_data.from_date, self.report_data.to_date

        logo_path = STATIC_DIR / "justup_1c.png"
        if logo_path.exists():
            logo = Image(str(logo_path))
            # Read the intrinsic size first: accessing it after setting drawWidth resets it
            aspect = logo.imageHeight / logo.imageWidth
            logo.drawWidth = 34 * mm
            logo.drawHeight = 34 * mm * aspect
            logo.hAlign = "LEFT"
            self.elements.append(logo)
        self.elements.append(Spacer(1, 8 * mm))

        name, host = self._cover_name()
        size = 32
        while size > 16 and pdfmetrics.stringWidth(name, self.font_bold, size) > self.content_width:
            size -= 1
        title_style = ParagraphStyle(
            "CoverTitle",
            fontName=self.font_bold,
            fontSize=size,
            leading=size + 6,
            textColor=COLORS["navy"],
            wordWrap="CJK",
        )
        title = Paragraph(_esc(name), title_style)
        title.chapter_name = "Cover"
        self.elements.append(title)

        website_title = (self._get_whatweb_data() or {}).get("Title")
        secondary = " · ".join(part for part in (host, website_title) if part)
        if secondary:
            self.elements.append(
                Paragraph(
                    _esc(secondary),
                    ParagraphStyle("CoverHost", fontName=self.font, fontSize=11, leading=15, textColor=COLORS["muted"]),
                )
            )
        self.elements.append(Spacer(1, 3 * mm))
        self.elements.append(
            Paragraph(
                "Uptime &amp; Performance Report",
                ParagraphStyle(
                    "CoverSub", fontName=self.font_bold, fontSize=15, leading=20, textColor=COLORS["navy_mid"]
                ),
            )
        )
        self.elements.append(Spacer(1, 3 * mm))
        self.elements.append(
            HorizontalLine(width_pct=0.18, color=COLORS["success"], thickness=2.2, space_before=0, space_after=0)
        )
        self.elements.append(Spacer(1, 5 * mm))

        period_style = ParagraphStyle(
            "CoverPeriod", fontName=self.font, fontSize=10.5, leading=15, textColor=COLORS["dark"]
        )
        period_label = ParagraphStyle(
            "CoverPeriodLabel", fontName=self.font_bold, fontSize=8, leading=11, textColor=COLORS["muted"]
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
        self.elements.append(Spacer(1, 5 * mm))
        self._add_hero_kpi()
        self.elements.append(Spacer(1, 4 * mm))
        self._add_supporting_kpis()
        self._add_summary_sentence()

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
            ParagraphStyle("StatusBanner", fontName=self.font_bold, fontSize=17, leading=21, textColor=text_color),
        )
        detail_para = Paragraph(
            _esc(detail),
            ParagraphStyle("StatusDetail", fontName=self.font, fontSize=10, leading=14, textColor=COLORS["dark"]),
        )
        table = Table([[status_para, detail_para]], colWidths=[48 * mm, self.content_width - 48 * mm])
        table.setStyle(
            TableStyle(
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
        """Add availability as the central hero KPI."""
        kpis = self.kpis
        pct = kpis.uptime_percent
        if pct is None:
            color, hint = COLORS["muted"], "no measurements in this period"
        else:
            color = COLORS["success"] if pct >= 99 else COLORS["warning_text"] if pct >= 95 else COLORS["danger"]
            hint = f"{kpis.up_count:,} of {kpis.total_checks:,} checks successful"
            if kpis.coverage_pct is not None:
                hint += f" · {kpis.coverage_pct:g}% coverage"

        width, height = 100 * mm, 34 * mm
        hero_card = RoundedKPICard(
            label="AVAILABILITY",
            value=self._availability_value(pct),
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
        table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        table.hAlign = "CENTER"
        self.elements.append(table)

    def _availability_value(self, pct: float | None) -> str:
        """Format availability; with low coverage show only a rounded approximation."""
        cov = self.kpis.coverage_pct
        if pct is not None and cov is not None and cov < LOW_COVERAGE_PCT:
            return f"≈ {round(pct):d} %"
        return _fmt_pct(pct)

    def _add_supporting_kpis(self):
        """Add the three equal KPI cards: average response, maximum response, estimated downtime."""
        kpis = self.kpis
        gap = 5 * mm
        card_width = (self.content_width - 2 * gap) / 3
        has_checks = kpis.total_checks > 0
        samples = f"{kpis.response_samples:,} samples" if kpis.response_samples else "no measurements"
        down_color = COLORS["danger"] if kpis.downtime_minutes > 0 else COLORS["dark"]

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
                f"≈ {kpis.downtime_minutes:,} min" if has_checks else "n/a",
                down_color if has_checks else COLORS["muted"],
                f"{kpis.down_count:,} failed checks × interval" if has_checks else "no measurements",
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
                label_size=7.5,
                hint=hint,
            )
            for label, value, color, hint in specs
        ]
        table = Table([cards], colWidths=[card_width + gap, card_width + gap, card_width])
        table.setStyle(
            TableStyle(
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

    def _add_summary_sentence(self):
        """One data-backed sentence under the KPI cards; omitted when nothing was measured."""
        kpis = self.kpis
        if not kpis.total_checks:
            return
        parts = [f"{kpis.up_count:,} of {kpis.total_checks:,} availability checks succeeded"]
        events = len(self.downtime_events)
        if events:
            parts.append(f"{kpis.down_count:,} failed checks form {events} downtime event{'s' if events != 1 else ''}")
            parts.append(f"estimated downtime is about {kpis.downtime_minutes:,} min")
        else:
            parts.append("no downtime was recorded")
        sentence = "; ".join(parts) + "."
        if kpis.coverage_pct is not None and kpis.coverage_pct < LOW_COVERAGE_PCT:
            sentence += f" Checks cover only {kpis.coverage_pct:g}% of the period, so these figures are approximate."
        if kpis.avg_ping_ms is not None:
            sentence += f" Average ping latency was {kpis.avg_ping_ms:g} ms."
        self.elements.append(Spacer(1, 5 * mm))
        self.elements.append(
            Paragraph(
                _esc(sentence),
                ParagraphStyle(
                    "CoverSummary", fontName=self.font, fontSize=10, leading=14, textColor=COLORS["muted"], alignment=1
                ),
            )
        )

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
            dev_text = (
                f"{d.deviation:+.2f} pp"
                if d.metric_name == "Availability"
                else f"{d.deviation:+.1f} {d.unit} ({d.deviation_pct:+.1f}%)"
            )
            status = (
                f'<font name="{self.font_bold}" color="{COLORS["success_text"].hexval()}">Compliant</font>'
                if d.is_compliant
                else f'<font name="{self.font_bold}" color="{COLORS["danger_text"].hexval()}">Non-compliant</font>'
            )
            rows.append(
                [
                    Paragraph(d.metric_name, td),
                    Paragraph(f"{d.sla_target} {d.unit}", td),
                    Paragraph(f"{d.actual_value} {d.unit}", td),
                    Paragraph(dev_text, td),
                    Paragraph(status, td),
                ]
            )

        w = self.content_width
        table = Table(rows, colWidths=[w * 0.22, w * 0.18, w * 0.18, w * 0.24, w * 0.18], repeatRows=1)
        table.setStyle(
            TableStyle(
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

    # ─── Technical Summary ───────────────────────────────────────────────────

    def _add_technical_summary(self):
        """Technical Summary: Target Information, Certificate, TLS & Security, technology stack."""
        self._chapter_header("Technical Summary", self._period_text())

        self._group_title("Target Information")
        self._add_check_badges()
        self._add_provider_info()

        self._add_ssl_summary()
        if self._get_ssl_data() is None:
            self._group_title("Certificate & TLS")
            self.elements.append(
                Paragraph(
                    "Not applicable: the target is not served over HTTPS, so no certificate or TLS scan was run.",
                    self.styles["note"],
                )
            )

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
            ("HTTP", target.get("enable_http_check"), COLORS["success"]),
            ("PING", target.get("enable_ping"), COLORS["success"]),
            ("Cert", target.get("enable_cert_expiration"), COLORS["success"]),
            ("TCP", target.get("enable_tcp_connect"), COLORS["navy_mid"]),
        ]
        enabled = [
            Badge(text=name, font=self.font, bg_color=color, padding_y=1 * mm, shadow=False)
            for name, on, color in checks
            if on
        ]
        if enabled:
            inner = Table([enabled], colWidths=[b.badge_width + 3 * mm for b in enabled])
            inner.setStyle(
                TableStyle(
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
        info = lookup_provider_info(lookup_host)

        country_code = info["country_code"]
        location = (
            f"{info['location']} ({country_code})" if country_code and info["location"] != "-" else info["location"]
        )

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

    def _get_ssl_data(self) -> TlsScanResult | None:
        """Fetch and cache the normalized TLS scan for the target.

        Returns cached result on subsequent calls.
        Respects custom cert_host/cert_port or port in http_url.
        """
        if self._ssl_fetched:
            return self._ssl_cache

        self._ssl_fetched = True

        target = self.report_data.target

        # Priority 1: Explicit cert_host and cert_port
        cert_host = target.get("cert_host")
        cert_port = target.get("cert_port")
        http_port = target.get("http_port")

        if cert_host:
            port = cert_port or http_port or 443
            ssl_target = f"https://{cert_host}:{port}"
            _log.debug("TLS scan using explicit cert_host: %s", ssl_target)
        else:
            # Priority 2: Extract from http_url (may contain custom port)
            http_url = target.get("http_url") or target.get("url") or ""

            if not http_url:
                host = target.get("host", "")
                if host:
                    http_url = f"https://{host}"

            if not http_url.startswith("https://"):
                _log.debug("Skipping SSL scan for non-HTTPS target: %s", redact_url(http_url))
                return None

            # Respect request-port override if URL has no explicit port.
            ssl_target = http_url
            try:
                parsed = urlparse(http_url)
                if http_port and parsed.hostname and parsed.port is None:
                    host = parsed.hostname
                    if ":" in host and not host.startswith("["):
                        host = f"[{host}]"
                    ssl_target = urlunparse(parsed._replace(netloc=f"{host}:{int(http_port)}"))
            except ValueError:
                ssl_target = http_url

        _log.debug("Running TLS scan for %s", redact_url(ssl_target))
        try:
            self._ssl_cache = scan_tls(ssl_target, timeout=15)
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
        cert = ssl.certificate
        if cert is None:
            state = "fail" if ssl.certificate_check.verdict is Verdict.INSECURE else "unknown"
            self.elements.append(
                self._kv_table([("Certificate", Paragraph("Certificate details unavailable", token), state)])
            )
            return

        def verdict_row(label: str, check: Check, ok: str, bad: str) -> tuple[str, Paragraph, str]:
            text, state = self._verdict(check, ok, bad)
            return label, Paragraph(_esc(text), token), state

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
                    f"{cert.valid_from:%Y-%m-%d} – {cert.valid_until:%Y-%m-%d} ({cert.valid_from.tzname() or 'UTC'})",
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
                    f"{len(cert.san)} name{'s' if len(cert.san) != 1 else ''}<br/>{sans}" if cert.san else "none", token
                ),
                None,
            ),
            verdict_row("Chain", cert.chain_order, f"{cert.chain_length} certificates, valid order", "Invalid order"),
        ]
        self.elements.append(self._kv_table(rows))

    def _add_tls_group(self, ssl: TlsScanResult) -> None:
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
        else:
            rows.append(
                ("Scan Status", Paragraph("Completed, all checks returned a result", token), ("pass", "COMPLETE"))
            )

        for proto in ssl.protocols:
            suites = "<br/>".join(_esc(s) for s in proto.cipher_suites)
            if proto.supported is None:
                label, state = "Not tested", "unknown"
            elif proto.supported:
                label, state = ("Offered (legacy)", "fail") if proto.legacy else ("Offered", "pass")
            else:
                label, state = ("Not offered", "pass") if proto.legacy else ("Not offered", "info")
            body = f"<b>{label}</b>" + (f"<br/>{suites}" if suites else "")
            rows.append((proto.name, Paragraph(body, token), state))

        rows.append(("Preferred Cipher", Paragraph(_esc(ssl.preferred_cipher or "—"), token), None))

        def check_row(label: str, check: Check, ok: str, bad: str):
            text, state = self._verdict(check, ok, bad)
            rows.append((label, Paragraph(_esc(text), token), state))

        check_row("Weak Protocols", ssl.weak_protocols, "None offered", ssl.weak_protocols.detail or "Offered")
        check_row("Heartbleed", ssl.heartbleed, "Not vulnerable", "Vulnerable")
        check_row("CCS Injection", ssl.ccs_injection, "Not vulnerable", "Vulnerable")
        check_row("ROBOT", ssl.robot, "Not vulnerable", "Vulnerable")
        check_row("Secure Renegotiation", ssl.secure_renegotiation, "Supported", "Not supported")
        check_row("CRIME (compression)", ssl.compression, "Compression off", "Compression enabled")
        self.elements.append(self._kv_table(rows))

    # ─── Network Path ────────────────────────────────────────────────────────

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
            return info["last_hop_num"] >= _MTR_MAX_HOPS
        return self._mtr_silent_at_limit

    def _build_network_path_page(self):
        """Build the Network Path chapter with the hop table."""
        display_hops, _geo, has_interpolation = self._get_mtr_data(max_hops=_MTR_MAX_HOPS)
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
        latency = ((reply or {}).get("latency_ms") or {}).get("avg")
        if reply and latency:
            parts.append(f"latency at last responding hop {latency:.1f} ms")
        self._chapter_header("Network Path", " · ".join(parts))
        self._add_route_schematic(display_hops, host)
        self._add_traceroute(display_hops, has_interpolation, info)

    @staticmethod
    def _schematic_nodes(display_hops: list[dict[str, Any]], host: str | None) -> list[dict[str, Any]]:
        """Derive the schematic's nodes from the already-enriched display hops.

        Applies the shared route-model rules: an interpolated/blocked or
        unlocated hop carries no location and is marked unknown; a hop with a
        city is located; the destination and a non-responding (synthetic)
        destination are flagged. Hop order and numbers are preserved.
        """
        nodes: list[dict[str, Any]] = []
        last_index = len(display_hops) - 1
        prev_unknown = False
        for index, hop in enumerate(display_hops):
            blocked = bool(hop.get("_collapsed") or hop.get("status") == "interpolated" or hop.get("_interpolated"))
            synthetic = bool(hop.get("_synthetic_destination"))
            city, country = hop.get("city"), hop.get("country")
            located = bool(city) and not blocked and not synthetic
            label = str(hop.get("hop_display") or hop.get("hop") or "?")

            if index == 0:
                kind = "start"
            elif synthetic:
                kind = "destination_unknown"
            elif index == last_index:
                kind = "destination" if located else "destination_unknown"
            elif located:
                kind = "hop"
            else:
                kind = "unknown"

            if located:
                location_text = f"{city} ({country})" if country else str(city)
            elif synthetic:
                location_text = "no reply"
            elif blocked:
                location_text = "ICMP blocked"
            else:
                location_text = "position unknown"

            nodes.append(
                {
                    "label": label,
                    "kind": kind,
                    "location_text": location_text,
                    "gap_before": prev_unknown,
                }
            )
            prev_unknown = not located
        if nodes and host and nodes[-1]["kind"] in ("destination", "destination_unknown"):
            nodes[-1]["location_text"] = nodes[-1]["location_text"] or _esc(host)
        return nodes

    def _add_route_schematic(self, display_hops: list[dict[str, Any]], host: str | None):
        """Add the compact static vector route schematic before the hop table."""
        nodes = self._schematic_nodes(display_hops, host)
        if not nodes:
            return
        schematic = RouteSchematic(nodes, self.font, self.font_bold, self.content_width)
        self.elements.append(KeepTogether([schematic, Spacer(1, 3 * mm)]))

    def _status_badge(self, text: str, kind: str) -> Badge:
        bg, fg = {
            "ok": (COLORS["success"], colors.white),
            "partial": (COLORS["warning"], COLORS["dark"]),
        }.get(kind, (COLORS["unknown_soft"], COLORS["dark"]))
        return Badge(
            text=text,
            font=self.font,
            font_size=7,
            padding_x=2.2 * mm,
            padding_y=0.9 * mm,
            corner_radius=1.6 * mm,
            bg_color=bg,
            text_color=fg,
            shadow=False,
        )

    def _add_traceroute(
        self,
        display_hops: list[dict[str, Any]] | None = None,
        has_interpolation: bool | None = None,
        info: dict[str, Any] | None = None,
    ):
        """Add the hop table; the header repeats on every page."""
        if display_hops is None:
            display_hops, _geo, has_interpolation = self._get_mtr_data(max_hops=_MTR_MAX_HOPS)
        if not display_hops:
            return
        info = info or self._route_summary(display_hops)
        st = self.styles

        rows: list[list[Any]] = [
            [
                Paragraph("Hop", st["th"]),
                Paragraph("Host / Address", st["th"]),
                Paragraph("Location", st["th"]),
                Paragraph("Latency", st["th_right"]),
                Paragraph("Loss", st["th_right"]),
                Paragraph("Status", st["th"]),
            ]
        ]
        for hop in display_hops:
            blocked = bool(hop.get("_interpolated") or hop.get("_collapsed") or hop.get("status") == "interpolated")
            synthetic = bool(hop.get("_synthetic_destination"))
            status = str(hop.get("status") or "timeout").lower()
            muted = blocked or status == "timeout"
            td = st["td_muted"] if muted else st["td"]
            td_right = st["td_muted_right"] if muted else st["td_right"]

            hop_num = hop.get("hop_display") or str(hop.get("hop", "?"))
            ip_raw = hop.get("ip_display") or hop.get("ip")
            ip = str(ip_raw) if ip_raw and ip_raw not in ("-", "*", "???") else "—"
            hostname = hop.get("hostname_display") or hop.get("hostname")
            hostname = str(hostname) if hostname and hostname not in ("-", "—") and hostname != ip else ""
            if synthetic:
                hostname = f"{hostname} · destination did not reply to probes".strip(" ·")
            path_html = _esc(ip)
            if hostname:
                path_html = f'{path_html}<br/><font size="8" color="{COLORS["muted"].hexval()}">{_esc(hostname)}</font>'
            path = Paragraph(path_html, st["hop_ip_muted"] if muted else st["hop_ip"])

            city, country = hop.get("city") or "", hop.get("country") or ""
            location = (
                "—"
                if blocked
                else f"{city} ({country})"
                if city and country
                else city or (f"({country})" if country else "—")
            )

            lat = (hop.get("latency_ms") or {}).get("avg") if isinstance(hop.get("latency_ms"), dict) else None
            latency = f"{lat:.1f} ms" if lat else "—"
            loss_pct = hop.get("loss_pct")
            loss = f"{loss_pct:.0f}%" if loss_pct is not None and not blocked else "—"

            if synthetic:
                badge = self._status_badge("NO REPLY", "blocked")
            elif blocked:
                badge = self._status_badge("BLOCKED", "blocked")
            elif status in ("ok", "partial", "timeout"):
                badge = self._status_badge(status.upper(), status)
            else:
                badge = self._status_badge(status.upper()[:10], "blocked")

            rows.append(
                [
                    Paragraph(_esc(hop_num), td),
                    path,
                    Paragraph(_esc(location), td),
                    Paragraph(latency, td_right),
                    Paragraph(loss, td_right),
                    badge,
                ]
            )

        w = self.content_width
        fixed = [13 * mm, 34 * mm, 20 * mm, 14 * mm, 22 * mm]
        col_widths = [fixed[0], w - sum(fixed), *fixed[1:]]
        table = LongTable(rows, colWidths=col_widths, repeatRows=1, splitByRow=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), COLORS["navy"]),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, 0), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [COLORS["white"], COLORS["surface"]]),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.4, COLORS["divider"]),
                    ("BOX", (0, 0), (-1, -1), 0.5, COLORS["divider"]),
                ]
            )
        )
        self.elements.append(table)

        notes = []
        if has_interpolation:
            notes.append(
                "BLOCKED: intermediate routers did not answer ICMP probes; these hops are interpolated, "
                "not measured. Missing latency and loss are shown as —."
            )
        for note in notes:
            self.elements.append(Paragraph(_esc(note), st["note"]))
        if self._route_incomplete(info):
            self.elements.append(
                Paragraph(
                    f"<b>{ROUTE_INCOMPLETE}.</b> The destination was not reached within {_MTR_MAX_HOPS} hops; "
                    "the route may continue beyond this limit and is not shown completely.",
                    ParagraphStyle(
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

    def _get_mtr_data(self, *, max_hops: int = _MTR_MAX_HOPS) -> tuple[list[dict[str, Any]], list[dict], bool]:
        """Run MTR once per report build and cache the result.

        Returns:
            Tuple of (display_hops, geo_points, has_interpolation)
        """
        host = self._get_traceroute_host()
        if not host:
            return [], [], False

        cache_key = (host, max_hops)
        if cache_key in self._mtr_cache:
            return self._mtr_cache[cache_key]

        display_hops, geo_points, has_interpolation = _run_mtr_with_interpolation(host, max_hops=max_hops)

        tail = display_hops[-1] if display_hops else {}
        self._mtr_silent_at_limit = (
            tail.get("status") == "timeout"
            and not tail.get("_interpolated")
            and self._route_summary(display_hops)["last_hop_num"] >= max_hops
        )

        # Strip trailing pure timeouts (keep interpolated hops)
        while (
            display_hops and display_hops[-1].get("status") == "timeout" and not display_hops[-1].get("_interpolated")
        ):
            display_hops.pop()

        if not display_hops:
            return [], [], False

        result = (display_hops, geo_points, has_interpolation)
        self._mtr_cache[cache_key] = result
        return result

    # ─── Availability & Performance ──────────────────────────────────────────

    def _chart_width_in(self) -> float:
        return self.content_width / 72.0

    def _build_charts_page(self):
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
                sla_response_ms, response_label = _DEFAULT_RESPONSE_MS, "Default threshold (500 ms, no SLA set)"
            if sla_ping_ms is None:
                sla_ping_ms, ping_label = _DEFAULT_PING_MS, "Default threshold (100 ms, no SLA set)"
        else:
            sla_response_ms = sla_ping_ms = None

        width = self._chart_width_in()
        kpis = self.kpis
        added = 0

        uptime_chart = _create_uptime_chart(
            data.uptime_data, from_date=data.from_date, to_date=data.to_date, width=width
        )
        if uptime_chart:
            note = f"{kpis.up_count:,} of {kpis.total_checks:,} checks up ({_fmt_pct(kpis.uptime_percent)})"
            note += f" · {len(self.downtime_events)} downtime event{'s' if len(self.downtime_events) != 1 else ''}"
            self._add_chart_section(
                "Availability Timeline", uptime_chart, note + ". Short outages are drawn with a minimum visible width."
            )
            added += 1

        display = data.display_data or {}
        response_chart = _create_response_time_chart(
            display.get("http_ms", data.http_data),
            from_date=data.from_date,
            to_date=data.to_date,
            width=width,
            sla_threshold_ms=int(sla_response_ms) if sla_response_ms else None,
            threshold_label=response_label,
        )
        if response_chart:
            self._add_chart_section("Response Time", response_chart, self._latency_note(data.http_data))
            added += 1

        if data.status_code_data:
            status_chart = _create_status_code_chart(data.status_code_data, width=width, exact=data.status_counts_exact)
            if status_chart:
                self._add_chart_section(
                    "HTTP Status Codes",
                    status_chart,
                    "Counts cover retained raw HTTP checks only; historical status rollups are excluded."
                    if data.status_counts_exact
                    else "Counts are stored samples; history may be rolled up, so they are not exact per-check counts.",
                )
                added += 1

        if target.get("enable_ping") and data.ping_data:
            ping_chart = _create_ping_chart(
                display.get("ping_ms", data.ping_data),
                from_date=data.from_date,
                to_date=data.to_date,
                width=width,
                sla_threshold_ms=int(sla_ping_ms) if sla_ping_ms else None,
                threshold_label=ping_label,
            )
            if ping_chart:
                self._add_chart_section("Ping Latency", ping_chart, self._latency_note(data.ping_data))
                added += 1

        if target.get("enable_tcp_connect"):
            tcp_chart = None
            note = None
            if data.tcp_port_data:
                display_ports = {
                    port: display.get(f"tcp_ms_{port}", points) for port, points in data.tcp_port_data.items()
                }
                tcp_chart = _create_multi_port_tcp_chart(
                    display_ports, from_date=data.from_date, to_date=data.to_date, width=width
                )
                note = "One panel per port, shared time axis and Y scale."
            if not tcp_chart and data.tcp_data:
                tcp_chart = _create_tcp_chart(
                    display.get("tcp_ms", data.tcp_data), from_date=data.from_date, to_date=data.to_date, width=width
                )
                note = self._latency_note(data.tcp_data)
            if tcp_chart:
                self._add_chart_section("TCP Latency", tcp_chart, note)
                added += 1

        if not added:
            self.elements.append(Spacer(1, 12 * mm))
            self.elements.append(
                Paragraph("No measurement data is available for this reporting period.", self.styles["empty_text"])
            )
        self.elements.append(PageBreak())

    @staticmethod
    def _latency_note(points: list[dict]) -> str | None:
        values = _valid_values(points)
        if not values:
            return None
        avg = sum(values) / len(values)
        return f"Average {avg:,.0f} ms · maximum {max(values):,.0f} ms · {len(values):,} measurements"

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

    # ─── Downtime Log ────────────────────────────────────────────────────────

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
                badge = Badge(
                    text="ONGOING",
                    font=self.font,
                    font_size=7,
                    padding_x=2.2 * mm,
                    padding_y=0.9 * mm,
                    corner_radius=1.6 * mm,
                    bg_color=COLORS["danger"],
                    shadow=False,
                )
            else:
                end_text = _fmt_dt(event.end, self.tz)
                duration = _fmt_duration(event.duration_minutes)
                badge = Badge(
                    text="RESOLVED",
                    font=self.font,
                    font_size=7,
                    padding_x=2.2 * mm,
                    padding_y=0.9 * mm,
                    corner_radius=1.6 * mm,
                    bg_color=COLORS["success"],
                    shadow=False,
                )
            rows.append(
                [
                    Paragraph(str(i), st["td_muted"]),
                    Paragraph(_fmt_dt(event.start, self.tz), st["td"]),
                    Paragraph(end_text, st["td"]),
                    Paragraph(duration, st["td"]),
                    badge,
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
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), COLORS["navy"]),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, 0), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [COLORS["white"], COLORS["surface"]]),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.4, COLORS["divider"]),
                    ("BOX", (0, 0), (-1, -1), 0.5, COLORS["divider"]),
                ]
            )
        )
        self.elements.append(table)
        self.elements.append(
            Paragraph(
                "Estimated downtime: durations run from the first failed check to the next successful check "
                "and are not exact measured outage durations. An ongoing outage is shown as at least the time "
                "up to the last sample.",
                st["note"],
            )
        )

    def _last_uptime_sample(self) -> datetime:
        """Time of the newest availability sample: the proven end of an ongoing outage."""
        stamps = []
        for p in self.report_data.uptime_data:
            try:
                stamps.append(_to_tz(datetime.fromisoformat(p["ts"]), self.tz))
            except (KeyError, TypeError, ValueError):
                continue
        return max(stamps) if stamps else _to_tz(self.report_data.to_date, self.tz)

    def _downtime_empty_state(self, *, has_data: bool) -> None:
        """Calm empty state; it never claims a clean record when nothing was measured."""
        st = self.styles
        if has_data:
            icon: Flowable = SuccessCheckCircle(size=22 * mm)
            title = "No downtime recorded"
            text = f"All {self.kpis.total_checks:,} availability checks in this reporting period succeeded."
            title_style = st["empty_title"]
        else:
            icon = Spacer(1, 1)
            title = "No availability data"
            text = "No availability measurements exist for this period, so downtime cannot be assessed."
            title_style = ParagraphStyle("EmptyTitleNeutral", parent=st["empty_title"], textColor=COLORS["muted"])
        table = Table(
            [
                [icon],
                [Spacer(1, 4 * mm)],
                [Paragraph(title, title_style)],
                [Spacer(1, 1 * mm)],
                [Paragraph(_esc(text), st["empty_text"])],
            ],
            colWidths=[self.content_width],
        )
        table.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        self.elements.append(Spacer(1, 45 * mm))
        self.elements.append(table)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
