#!/usr/bin/env python3
#
# app/services/pdf_report.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server-side PDF report generation with high-quality vector charts."""

from __future__ import annotations

import io
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for server
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from app.utils.whatweb_parser import run_whatweb
from app.utils.sslyze_scanner import scan_ssl, SSLSummary
from app.utils.version import VERSION, BUILD_INFO
from app.services.traceroute_geo import traceroute_to_geo_points, mtr_hops_to_geo_points, geolocate_ip
from app.services.traceroute_worldmap import render_traceroute_worldmap
from app.services.mtr_run import run_mtr, resolve_target_ip, analyze_route
from app.services.provider_lookup import lookup_provider_info
from app.services.route_interpolator import interpolate_route, ensure_destination_hop, collapse_interpolated_hops

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.rl_config import defaultPageSize  # noqa: F401 - ensure config is loaded

# Ensure fonts are embedded (subsetting is still used for smaller file size)
# TTFont automatically embeds fonts; this is the default ReportLab behavior.
# For full font embedding (no subsetting), set TTFont(...).splitByUnicode = False
# but subsetting is preferred for smaller PDFs while still embedding all used glyphs.

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, Flowable, KeepTogether
)

_log = logging.getLogger(__name__)

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
}

# Chart colors
CHART_COLORS = {
    "response_time": "#198754",
    "uptime_up": "#198754",
    "uptime_down": "#dc3545",
    "ping": "#198754",
    "tcp": "#6f42c1",
}

# Color palette for multi-port TCP chart (matches dashboard)
TCP_PORT_COLORS = [
    '#0d6efd',  # primary blue
    '#198754',  # success green
    '#dc3545',  # danger red
    '#fd7e14',  # orange
    '#6f42c1',  # purple
    '#20c997',  # teal
    '#d63384',  # pink
    '#ffc107',  # warning yellow
    '#6c757d',  # secondary gray
    '#0dcaf0',  # info cyan
]

# ---------------------------------------------------------------------------
# Spacing Design Tokens (consistent layout rhythm)
# ---------------------------------------------------------------------------
SPACE_XS = 1 * mm   # Minimal: dividers, tight padding
SPACE_SM = 2 * mm   # Small: inner cell padding
SPACE_MD = 4 * mm   # Medium: between elements
SPACE_LG = 8 * mm   # Large: section spacing
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
    
    # Get destination geo data for interpolation
    destination_geo = None
    if target_ip:
        destination_geo = geolocate_ip(target_ip)
    
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
    from app.services.traceroute_geo import geolocate_with_hostname_hint
    
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
    has_interpolation = any(
        h.get("status") == "interpolated" or h.get("_interpolated")
        for h in interpolated_hops
    )
    
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
    tcp_port_data: Optional[dict[int, list[dict]]] = None  # Per-port TCP data: {port: [points]}


@dataclass
class KPIMetrics:
    """Computed KPI values for the report."""
    uptime_percent: float
    avg_response_ms: float
    max_response_ms: float
    downtime_minutes: int
    total_checks: int
    up_count: int
    down_count: int
    avg_ping_ms: Optional[float] = None


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
    end: Optional[datetime]
    duration_minutes: Optional[int]


# ---------------------------------------------------------------------------
# Font Registration
# ---------------------------------------------------------------------------

_fonts_registered = False
_fonts_lock = threading.Lock()


def _register_fonts() -> None:
    """Register custom fonts with ReportLab (thread-safe)."""
    global _fonts_registered
    if _fonts_registered:
        return
    
    with _fonts_lock:
        # Double-check after acquiring lock
        if _fonts_registered:
            return
        
        try:
            roboto_regular = FONT_DIR / "Roboto-Regular.ttf"
            roboto_bold = FONT_DIR / "Roboto-Bold.ttf"
            noto_serif_bold = FONT_DIR / "NotoSerif-Bold.ttf"
            
            if roboto_regular.exists():
                pdfmetrics.registerFont(TTFont("Roboto", str(roboto_regular)))
                _log.debug("Registered Roboto-Regular font")
            else:
                _log.warning("Roboto-Regular.ttf not found at %s", roboto_regular)
                
            if roboto_bold.exists():
                pdfmetrics.registerFont(TTFont("Roboto-Bold", str(roboto_bold)))
                _log.debug("Registered Roboto-Bold font")
            else:
                _log.warning("Roboto-Bold.ttf not found at %s", roboto_bold)
            
            if noto_serif_bold.exists():
                pdfmetrics.registerFont(TTFont("NotoSerif-Bold", str(noto_serif_bold)))
                _log.debug("Registered NotoSerif-Bold font")
            else:
                _log.warning("NotoSerif-Bold.ttf not found at %s", noto_serif_bold)
            
            noto_serif_regular = FONT_DIR / "NotoSerif-Regular.ttf"
            if noto_serif_regular.exists():
                pdfmetrics.registerFont(TTFont("NotoSerif-Regular", str(noto_serif_regular)))
                _log.debug("Registered NotoSerif-Regular font")
            else:
                _log.warning("NotoSerif-Regular.ttf not found at %s", noto_serif_regular)
            
            # Register Material Icons for PDF icons
            material_icons = FONT_DIR / "MaterialIcons-Regular.ttf"
            if material_icons.exists():
                pdfmetrics.registerFont(TTFont("MaterialIcons", str(material_icons)))
                _log.debug("Registered MaterialIcons font")
            else:
                _log.warning("MaterialIcons-Regular.ttf not found at %s", material_icons)
                
            _fonts_registered = True
        except Exception as e:
            _log.error("Failed to register fonts: %s", e)


def _get_font() -> str:
    """Return the font name to use (Roboto if available, else Helvetica)."""
    _register_fonts()
    try:
        pdfmetrics.getFont("Roboto")
        return "Roboto"
    except KeyError:
        return "Helvetica"


def _get_font_bold() -> str:
    """Return the bold font name."""
    _register_fonts()
    try:
        pdfmetrics.getFont("Roboto-Bold")
        return "Roboto-Bold"
    except KeyError:
        return "Helvetica-Bold"


def _get_font_serif_bold() -> str:
    """Return the NotoSerif-Bold font name for domain display."""
    _register_fonts()
    try:
        pdfmetrics.getFont("NotoSerif-Bold")
        return "NotoSerif-Bold"
    except KeyError:
        return "Times-Bold"


def _get_font_serif_regular() -> str:
    """Return the NotoSerif-Regular font name for website title display."""
    _register_fonts()
    try:
        pdfmetrics.getFont("NotoSerif-Regular")
        return "NotoSerif-Regular"
    except KeyError:
        return "Times-Roman"


# ---------------------------------------------------------------------------
# Matplotlib Chart Generation
# ---------------------------------------------------------------------------

# Guard to prevent repeated matplotlib style setup (global state)
_mpl_initialized = False
_mpl_init_lock = threading.Lock()


def _setup_matplotlib_style() -> None:
    """
    Configure matplotlib for consistent, high-quality charts with Roboto font.
    
    Explicitly registers local Roboto TTF files with matplotlib's font manager
    to ensure charts use the same font as the rest of the PDF.
    
    Only runs once to avoid repeated global state modifications.
    """
    global _mpl_initialized
    if _mpl_initialized:
        return

    with _mpl_init_lock:
        if _mpl_initialized:
            return

        from matplotlib import font_manager

        # 1. Register Roboto fonts FIRST (before any style/rcParams changes)
        roboto_regular = FONT_DIR / "Roboto-Regular.ttf"
        roboto_bold = FONT_DIR / "Roboto-Bold.ttf"

        fonts_registered = False
        if roboto_regular.exists():
            font_manager.fontManager.addfont(str(roboto_regular))
            _log.debug("Registered Roboto-Regular with matplotlib")
            fonts_registered = True
        if roboto_bold.exists():
            font_manager.fontManager.addfont(str(roboto_bold))
            _log.debug("Registered Roboto-Bold with matplotlib")

        # 2. Apply base style (with fallback for matplotlib version changes)
        try:
            plt.style.use('seaborn-v0_8-whitegrid')
        except OSError:
            _log.debug("seaborn-v0_8-whitegrid not available, using default style")
            plt.style.use('default')

        # IMPORTANT: seaborn resets rcParams → force Roboto AFTER style

        # 3. Set rcParams AFTER style (style resets rcParams!)
        # Use Roboto exclusively - no fallbacks for consistent typography
        # Cartopy uses matplotlib's font manager for labels
        if fonts_registered:
            font_config = {
                'font.family': 'sans-serif',
                'font.sans-serif': ['Roboto'],
                'font.weight': 'regular',
                'mathtext.default': 'regular',
            }
        else:
            font_config = {
                'font.family': 'sans-serif',
                'font.sans-serif': ['DejaVu Sans', 'Helvetica', 'Arial'],
            }
            _log.warning("Roboto fonts not found for matplotlib, using fallback")

        plt.rcParams.update({
            **font_config,
            'font.size': 9,
            'axes.titlesize': 11,
            'axes.labelsize': 9,
            'xtick.labelsize': 8,
            'ytick.labelsize': 8,
            'legend.fontsize': 11,
            'figure.dpi': PDF_DPI,
            'savefig.dpi': PDF_DPI,
            'axes.linewidth': 0.5,
            'grid.linewidth': 0.3,
            'lines.linewidth': 1.5,
            'axes.facecolor': '#ffffff',
            'figure.facecolor': '#ffffff',
            'axes.edgecolor': '#e9ecef',
            'grid.color': '#e9ecef',
        })

        _mpl_initialized = True
        _log.debug("matplotlib configured with font.sans-serif=%s", plt.rcParams['font.sans-serif'])


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


def _create_response_time_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.5,
    height: float = 2.0,
    sla_threshold_ms: int = 1000,
) -> Optional[bytes]:
    """Create a response time line chart as PNG bytes."""
    if not data:
        return None
    
    _setup_matplotlib_style()
    
    with _safe_figure(figsize=(width, height)) as (fig, ax):
        timestamps = [datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) for p in data]
        values = [p["value"] for p in data]
        
        ax.fill_between(timestamps, values, alpha=0.2, color=CHART_COLORS["response_time"])
        ax.plot(timestamps, values, color=CHART_COLORS["response_time"], linewidth=1.2)
        
        max_val = max(values) if values else 0
        y_max = max_val * 1.1  # Default 10% headroom
        
        # Add SLA reference line if threshold is configured
        if sla_threshold_ms:
            # Extend y-axis to include SLA line with headroom
            y_max = max(y_max, sla_threshold_ms * 1.15)
            ax.axhline(
                y=sla_threshold_ms,
                linestyle="--",
                linewidth=1.2,
                color="#ffc107",
                alpha=0.9,
                label=f"SLA {sla_threshold_ms} ms",
            )
            ax.text(
                timestamps[-1],
                sla_threshold_ms,
                f"SLA {sla_threshold_ms} ms ",
                fontsize=7,
                color="#b38600",
                va="bottom",
                ha="right",
            )
        
        ax.set_ylabel("Response Time (ms)", fontsize=8)
        ax.set_ylim(bottom=0, top=y_max)
        
        # Format x-axis based on time range
        _format_time_axis(ax, from_date=from_date, to_date=to_date)
        
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)

        # Deterministic margins for PDF embedding (avoid tight_layout/bbox_inches='tight')
        fig.subplots_adjust(left=0.06, right=0.99, top=0.92, bottom=0.22)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format='png',
            dpi=PDF_DPI,
            facecolor='white',
            edgecolor='none',
            bbox_inches=None,
            pad_inches=0,
        )
        buf.seek(0)
        return buf.getvalue()


def _create_uptime_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.5,
    height: float = 1.8
) -> Optional[bytes]:
    """Create an availability timeline chart as PNG bytes."""
    if not data:
        return None
    
    _setup_matplotlib_style()
    
    with _safe_figure(figsize=(width, height)) as (fig, ax):
        timestamps = [datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) for p in data]
        values = [p["value"] for p in data]
        
        # Create colored segments
        for i in range(len(timestamps) - 1):
            color = CHART_COLORS["uptime_up"] if values[i] == 1 else CHART_COLORS["uptime_down"]
            ax.fill_between(
                [timestamps[i], timestamps[i + 1]],
                [values[i], values[i]],
                alpha=0.3,
                color=color,
                step='post'
            )
            ax.step(
                [timestamps[i], timestamps[i + 1]],
                [values[i], values[i]],
                where='post',
                color=color,
                linewidth=1.5
            )
        
        ax.set_ylabel("Status", fontsize=8)
        ax.set_ylim(-0.1, 1.1)
        ax.set_yticks([0, 1])
        ax.set_yticklabels(["Down", "Up"])
        
        _format_time_axis(ax, from_date=from_date, to_date=to_date)
        
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)

        fig.subplots_adjust(left=0.06, right=0.99, top=0.92, bottom=0.22)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format='png',
            dpi=PDF_DPI,
            facecolor='white',
            edgecolor='none',
            bbox_inches=None,
            pad_inches=0,
        )
        buf.seek(0)
        return buf.getvalue()


def _create_ping_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.5,
    height: float = 2.0,
    sla_threshold_ms: Optional[int] = None,
) -> Optional[bytes]:
    """Create a ping latency chart as PNG bytes."""
    if not data:
        return None
    
    _setup_matplotlib_style()
    
    with _safe_figure(figsize=(width, height)) as (fig, ax):
        timestamps = [datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) for p in data]
        values = [p["value"] for p in data]
        
        ax.fill_between(timestamps, values, alpha=0.2, color=CHART_COLORS["ping"])
        ax.plot(timestamps, values, color=CHART_COLORS["ping"], linewidth=1.2)
        
        max_val = max(values) if values else 0
        y_max = max_val * 1.1  # Default 10% headroom
        
        # Add SLA reference line if threshold is configured
        if sla_threshold_ms:
            # Extend y-axis to include SLA line with headroom
            y_max = max(y_max, sla_threshold_ms * 1.15)
            ax.axhline(
                y=sla_threshold_ms,
                linestyle="--",
                linewidth=1.2,
                color="#ffc107",
                alpha=0.9,
                label=f"SLA {sla_threshold_ms} ms",
            )
            ax.text(
                timestamps[-1],
                sla_threshold_ms,
                f"SLA {sla_threshold_ms} ms ",
                fontsize=7,
                color="#b38600",
                va="bottom",
                ha="right",
            )
        
        ax.set_ylabel("Ping (ms)", fontsize=8)
        ax.set_ylim(bottom=0, top=y_max)
        
        _format_time_axis(ax, from_date=from_date, to_date=to_date)
        
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)

        fig.subplots_adjust(left=0.06, right=0.99, top=0.92, bottom=0.22)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format='png',
            dpi=PDF_DPI,
            facecolor='white',
            edgecolor='none',
            bbox_inches=None,
            pad_inches=0,
        )
        buf.seek(0)
        return buf.getvalue()


def _create_tcp_chart(
    data: list[dict],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.5,
    height: float = 2.0
) -> Optional[bytes]:
    """Create a TCP latency chart as PNG bytes."""
    if not data:
        return None
    
    _setup_matplotlib_style()
    
    with _safe_figure(figsize=(width, height)) as (fig, ax):
        timestamps = [datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) for p in data]
        values = [p["value"] for p in data]
        
        ax.fill_between(timestamps, values, alpha=0.2, color=CHART_COLORS["tcp"])
        ax.plot(timestamps, values, color=CHART_COLORS["tcp"], linewidth=1.2)
        
        ax.set_ylabel("TCP Latency (ms)", fontsize=8)
        ax.set_ylim(bottom=0)
        
        _format_time_axis(ax, from_date=from_date, to_date=to_date)
        
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)

        fig.subplots_adjust(left=0.06, right=0.99, top=0.92, bottom=0.22)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format='png',
            dpi=PDF_DPI,
            facecolor='white',
            edgecolor='none',
            bbox_inches=None,
            pad_inches=0,
        )
        buf.seek(0)
        return buf.getvalue()


def _create_multi_port_tcp_chart(
    port_data: dict[int, list[dict]],
    from_date: datetime,
    to_date: datetime,
    width: float = 6.5,
    height: float = None  # Auto-calculated based on number of ports
) -> Optional[bytes]:
    """Create a Small Multiples TCP latency chart - one subplot per port.
    
    Small Multiples advantages:
    - No line overlap - each port is isolated and easy to read
    - Same X-axis for easy time comparison
    - Same Y-scale for fair value comparison
    - Trends and outliers are immediately visible
    - Port number as title, no legend needed
    
    Args:
        port_data: Dict mapping port numbers to their metric data points
        from_date: Start of time range
        to_date: End of time range
        width: Chart width in inches
        height: Auto-calculated if None
    """
    if not port_data:
        return None
    
    # Filter out empty ports
    port_data = {port: data for port, data in port_data.items() if data}
    if not port_data:
        return None
    
    _setup_matplotlib_style()
    
    # Sort ports for consistent ordering
    sorted_ports = sorted(port_data.keys())
    num_ports = len(sorted_ports)
    
    # Calculate height: ~0.8 inches per port, minimum 1.5 inches
    if height is None:
        height = max(1.5, num_ports * 0.8)
    
    # Create subplot grid - one row per port, sharing X-axis
    with _safe_figure(
        nrows=num_ports,
        ncols=1,
        figsize=(width, height),
        sharex=True,
        squeeze=False  # Always return 2D array
    ) as (fig, axes):
        axes = axes.flatten()  # Flatten to 1D array
        
        # Calculate global Y-max for consistent scale across all ports
        global_y_max = 0
        for port in sorted_ports:
            data = port_data[port]
            if data:
                values = [p["value"] for p in data]
                if values:
                    global_y_max = max(global_y_max, max(values))
        
        # Add 10% headroom and round up
        global_y_max = max(10, int(global_y_max * 1.1))
        
        for idx, port in enumerate(sorted_ports):
            ax = axes[idx]
            data = port_data[port]
            color = TCP_PORT_COLORS[idx % len(TCP_PORT_COLORS)]
            
            if data:
                timestamps = [datetime.fromisoformat(p["ts"].replace("Z", "+00:00")) for p in data]
                values = [p["value"] for p in data]
                
                ax.fill_between(timestamps, values, alpha=0.15, color=color)
                ax.plot(timestamps, values, color=color, linewidth=1.0)
            
            # Port label as title (not legend)
            ax.text(
                0.01, 0.85,
                f"Port {port}",
                transform=ax.transAxes,
                fontsize=8,
                fontweight='bold',
                color=color,
                verticalalignment='top'
            )
            
            # Same Y-scale for all charts (fair comparison)
            ax.set_ylim(0, global_y_max)
            ax.set_yticks([0, global_y_max // 2, global_y_max])
            ax.tick_params(axis='y', labelsize=7)
            
            # Only show Y label on middle chart
            if idx == num_ports // 2:
                ax.set_ylabel("ms", fontsize=7)
            
            # Clean styling
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            
            # Add subtle grid
            ax.grid(True, axis='y', alpha=0.3, linewidth=0.5)
            
            # Hide X-axis labels except for bottom chart
            if idx < num_ports - 1:
                ax.tick_params(axis='x', labelbottom=False)
        
        # Format X-axis on the bottom chart only
        _format_time_axis(axes[-1], from_date=from_date, to_date=to_date)
        
        # Adjust layout for compact display
        fig.subplots_adjust(left=0.08, right=0.98, top=0.95, bottom=0.15, hspace=0.1)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format='png',
            dpi=PDF_DPI,
            facecolor='white',
            edgecolor='none',
            bbox_inches=None,
            pad_inches=0,
        )
        buf.seek(0)
        return buf.getvalue()


def _create_status_code_chart(
    data: list[dict],
    width: float = 7.0,
    height: float = 2.0
) -> Optional[bytes]:
    """Create an HTTP status code distribution chart as PNG bytes (doughnut)."""
    if not data:
        return None
    
    # Count status codes
    status_counts: dict[int, int] = {}
    for p in data:
        code = int(p["value"])
        if 100 <= code <= 599:
            status_counts[code] = status_counts.get(code, 0) + 1
    
    if not status_counts:
        return None
    
    _setup_matplotlib_style()
    
    # Wide layout so the chart can use full PDF width (avoid height clamp in ReportLab)
    fig = plt.figure(figsize=(width, height))
    try:
        ax = fig.add_axes([0.05, 0.10, 0.50, 0.80])  # leave room on the right for legend
        
        codes = sorted(status_counts.keys())
        counts = [status_counts[c] for c in codes]
        labels = [f"HTTP {c}" for c in codes]
        
        # Color slices based on status code range
        slice_colors = []
        for code in codes:
            if code < 300:
                slice_colors.append(CHART_COLORS["uptime_up"])
            elif code < 400:
                slice_colors.append("#ffc107")  # Warning yellow
            else:
                slice_colors.append(CHART_COLORS["uptime_down"])
        
        # Create doughnut chart
        wedges, texts, autotexts = ax.pie(
            counts,
            labels=None,
            colors=slice_colors,
            autopct=lambda pct: f'{int(round(pct/100.*sum(counts)))}' if pct > 5 else '',
            pctdistance=0.75,
            wedgeprops=dict(width=0.5, edgecolor='white'),
            textprops={'fontsize': 12},
        )
        
        # Style the percentage texts
        for autotext in autotexts:
            autotext.set_fontsize(10)
            autotext.set_color('white')
            autotext.set_fontweight('bold')
        
        ax.axis('equal')  # Equal aspect ratio ensures circular shape

        # Legend inside the figure area (prevents clipping when bbox_inches is disabled)
        legend_labels = [f"{label} ({count})" for label, count in zip(labels, counts)]
        fig.legend(
            wedges,
            legend_labels,
            loc="center left",
            bbox_to_anchor=(0.60, 0.50),
            frameon=False,
            fontsize=9,
            handlelength=1.2,
            handletextpad=0.6,
            borderaxespad=0.0,
        )

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format='png',
            dpi=PDF_DPI,
            facecolor='white',
            edgecolor='none',
            bbox_inches=None,
            pad_inches=0,
        )
        buf.seek(0)
        return buf.getvalue()
    finally:
        plt.close(fig)


def _format_time_axis(ax, *, from_date: datetime, to_date: datetime) -> None:
    """Format the x-axis based on the report time range (fixed limits)."""
    # Enforce strict limits determined by the report period, not the data
    ax.set_xlim(from_date, to_date)
    
    time_range = (to_date - from_date).total_seconds()
    
    if time_range <= 6 * 3600:  # <= 6 hours
        locator = mdates.MinuteLocator(interval=30)
        formatter = mdates.DateFormatter('%H:%M')
    elif time_range <= 24 * 3600:  # <= 1 day
        locator = mdates.HourLocator(interval=2)
        formatter = mdates.DateFormatter('%H:%M')
    elif time_range <= 7 * 24 * 3600:  # <= 7 days
        locator = mdates.DayLocator(interval=1)
        formatter = mdates.DateFormatter('%m/%d')
    else:  # > 7 days
        locator = mdates.DayLocator(interval=2)
        formatter = mdates.DateFormatter('%m/%d')
    
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha='center')


# ---------------------------------------------------------------------------
# KPI Calculation
# ---------------------------------------------------------------------------

def _compute_kpis(report_data: ReportData) -> KPIMetrics:
    """Compute KPI metrics from the report data."""
    http_data = report_data.http_data
    uptime_data = report_data.uptime_data
    ping_data = report_data.ping_data
    interval_seconds = report_data.target.get("interval_seconds", 60)
    
    # Response time stats
    if http_data:
        response_values = [p["value"] for p in http_data if p.get("value") is not None]
        avg_response = sum(response_values) / len(response_values) if response_values else 0
        max_response = max(response_values) if response_values else 0
    else:
        avg_response = 0
        max_response = 0
    
    # Ping stats
    avg_ping: Optional[float] = None
    if ping_data:
        ping_values = [p["value"] for p in ping_data if p.get("value") is not None]
        avg_ping = sum(ping_values) / len(ping_values) if ping_values else None
    
    # Uptime stats
    if uptime_data:
        up_count = sum(1 for p in uptime_data if p.get("value") == 1)
        total_checks = len(uptime_data)
        down_count = total_checks - up_count
        uptime_percent = (up_count / total_checks * 100) if total_checks > 0 else 0
        downtime_minutes = int(down_count * interval_seconds / 60)
    else:
        up_count = 0
        total_checks = 0
        down_count = 0
        uptime_percent = 0
        downtime_minutes = 0
    
    return KPIMetrics(
        uptime_percent=round(uptime_percent, 2),
        avg_response_ms=round(avg_response),
        max_response_ms=round(max_response),
        downtime_minutes=downtime_minutes,
        total_checks=total_checks,
        up_count=up_count,
        down_count=down_count,
        avg_ping_ms=round(avg_ping, 1) if avg_ping is not None else None,
    )


def _extract_downtime_events(uptime_data: list[dict]) -> list[DowntimeEvent]:
    """Extract downtime periods from uptime data."""
    events: list[DowntimeEvent] = []
    if not uptime_data:
        return events
    
    # Sort by timestamp to ensure correct event ordering
    sorted_data = sorted(uptime_data, key=lambda p: p.get("ts", ""))
    
    in_downtime = False
    downtime_start: Optional[datetime] = None
    skipped_count = 0  # Track invalid timestamps
    
    for p in sorted_data:
        ts_raw = p.get("ts")
        if not ts_raw:
            skipped_count += 1
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            skipped_count += 1
            continue
        
        value = p.get("value", 1)
        
        if value == 0 and not in_downtime:
            in_downtime = True
            downtime_start = ts
        elif value == 1 and in_downtime:
            in_downtime = False
            if downtime_start:
                duration = int((ts - downtime_start).total_seconds() / 60)
                events.append(DowntimeEvent(
                    start=downtime_start,
                    end=ts,
                    duration_minutes=duration
                ))
            downtime_start = None
    
    # Handle ongoing downtime
    if in_downtime and downtime_start:
        events.append(DowntimeEvent(
            start=downtime_start,
            end=None,
            duration_minutes=None
        ))
    
    # Log warning if timestamps were skipped (helps debugging data issues)
    if skipped_count > 0:
        _log.warning(
            "Downtime extraction: skipped %d datapoints with invalid/missing timestamps",
            skipped_count
        )
    
    return events


# ---------------------------------------------------------------------------
# Custom Flowables
# ---------------------------------------------------------------------------

class RoundedKPICard(Flowable):
    """A KPI card with rounded corners, background, and centered text."""
    
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
    ):
        super().__init__()
        self.label = label
        self.value = value
        self.card_width = width
        self.card_height = height
        self.value_color = value_color
        self.font = font
        self.font_bold = font_bold
        self.bg_color = bg_color or colors.HexColor("#f8f9fa")
        self.corner_radius = corner_radius
        self.hint = hint
        self.value_size = value_size
        self.label_size = label_size
        self.border_width = border_width
    
    def wrap(self, availWidth, availHeight):
        return self.card_width, self.card_height
    
    def draw(self):
        canvas = self.canv
        w, h = self.card_width, self.card_height
        r = self.corner_radius
        
        canvas.saveState()
        
        # Draw simulated blur shadow using multiple layers with decreasing opacity
        _draw_soft_shadow(canvas, w, h, r)
        
        # Draw rounded rectangle background
        canvas.setFillColor(self.bg_color)
        canvas.setStrokeColor(COLORS["divider"])
        canvas.setLineWidth(self.border_width)
        canvas.roundRect(0, 0, w, h, r, fill=1, stroke=1)
        
        # Draw label (top, smaller, muted)
        canvas.setFont(self.font, self.label_size)
        canvas.setFillColor(COLORS["muted"])
        
        # Dynamic positioning based on height
        label_y = h - (h * 0.25)  # Label at 75% height
        canvas.drawCentredString(w / 2, label_y, self.label)
        
        # Draw value (centered relative to remaining space)
        canvas.setFont(self.font_bold, self.value_size)
        canvas.setFillColor(self.value_color)
        value_y = h * 0.35  # Value at 35% height
        canvas.drawCentredString(w / 2, value_y, self.value)
        
        # Draw hint (at bottom if space allows)
        if self.hint and h > 20 * mm:
            canvas.setFont(self.font, self.label_size - 2)
            canvas.setFillColor(COLORS["muted"])
            canvas.drawCentredString(w / 2, 2 * mm, self.hint)
        
        canvas.restoreState()


class Badge(Flowable):
    """A small badge with rounded corners and colored background."""
    
    def __init__(
        self,
        text: str,
        font: str = "Helvetica",
        padding_x: float = 3 * mm,
        padding_y: float = 1.5 * mm,
        font_size: int = 8,
        bg_color=None,
        text_color=None,
        corner_radius: float = 2 * mm,
        bold: bool = True,
    ):
        super().__init__()
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
    
    def wrap(self, availWidth, availHeight):
        return self.badge_width, self.badge_height
    
    def draw(self):
        canvas = self.canv
        canvas.saveState()
        
        # Draw simulated blur shadow using multiple layers with decreasing opacity
        _draw_soft_shadow(canvas, self.badge_width, self.badge_height, self.corner_radius)
        
        # Draw rounded rectangle background
        canvas.setFillColor(self.bg_color)
        canvas.roundRect(
            0, 0,
            self.badge_width,
            self.badge_height,
            self.corner_radius,
            fill=1,
            stroke=0
        )
        
        # Draw centered text (vertically centered using font metrics)
        canvas.setFont(self.font, self.font_size)
        canvas.setFillColor(self.text_color)
        # Calculate vertical center: (badge_height - font_size) / 2, adjusted for baseline
        # Font descender is typically ~20% of font size
        descender = self.font_size * 0.2
        text_y = (self.badge_height - self.font_size) / 2 + descender
        canvas.drawCentredString(
            self.badge_width / 2,
            text_y,
            self.text
        )
        
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
    
    def wrap(self, availWidth, availHeight):
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
    
    def wrap(self, availWidth, availHeight):
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
        p.lineTo(cx + 3 * scale, cy + 2.3 * scale)    # Top-right point
        canvas.drawPath(p, fill=0, stroke=1)
        
        canvas.restoreState()


# ---------------------------------------------------------------------------
# PDF Document Generation
# ---------------------------------------------------------------------------

class PDFReportBuilder:
    """Builds the PDF report using ReportLab."""
    
    def __init__(self, report_data: ReportData, page_size: str = "a4"):
        self.report_data = report_data
        self.page_size = LETTER if page_size.lower() == "letter" else A4
        self.width, self.height = self.page_size
        self.margin = 20 * mm
        self.font = _get_font()
        self.font_regular = self.font  # Alias for consistency
        self.font_bold = _get_font_bold()
        self.font_serif_bold = _get_font_serif_bold()
        self.font_serif_regular = _get_font_serif_regular()
        self.styles = self._create_styles()
        self.elements: list = []
        self.kpis = _compute_kpis(report_data)
        self.downtime_events = _extract_downtime_events(report_data.uptime_data)
        # MTR cache: stores (display_hops, geo_points, has_interpolation)
        self._mtr_cache: dict[tuple[str, int], tuple[list, list, bool]] = {}
        # WhatWeb cache: stores the tech stack dict
        self._whatweb_cache: Optional[dict] = None
        self._whatweb_fetched: bool = False
        # SSLyze cache: stores the SSL summary
        self._ssl_cache: Optional["SSLSummary"] = None
        self._ssl_fetched: bool = False
    
    def prefetch_external_data(self) -> None:
        """Fetch WhatWeb, SSLyze, and MTR data (blocking I/O, call before build).
        
        This method should be called before acquiring the PDF generation lock
        to avoid blocking other PDF requests on slow external scans.
        """
        self._get_whatweb_data()
        self._get_ssl_data()
        self._get_mtr_data()
    
    def _create_styles(self) -> dict:
        """Create custom paragraph styles."""
        base_styles = getSampleStyleSheet()
        
        return {
            "title": ParagraphStyle(
                "Title",
                parent=base_styles["Normal"],
                fontName=self.font_bold,
                fontSize=24,
                leading=28,
                textColor=COLORS["dark"],
                spaceAfter=6,
            ),
            "domain_title": ParagraphStyle(
                "DomainTitle",
                parent=base_styles["Normal"],
                fontName=self.font_serif_bold,
                fontSize=30,
                leading=38,
                textColor=COLORS["dark"],
                spaceAfter=4,
            ),
            "subtitle": ParagraphStyle(
                "Subtitle",
                parent=base_styles["Normal"],
                fontName=self.font,
                fontSize=12,
                leading=18,
                textColor=COLORS["muted"],
                spaceAfter=0,
            ),
            "website_title": ParagraphStyle(
                "WebsiteTitle",
                parent=base_styles["Normal"],
                fontName=self.font_serif_regular,
                fontSize=11,
                leading=14,
                textColor=COLORS["muted"],
                spaceAfter=2,
            ),
            "report_period": ParagraphStyle(
                "ReportPeriod",
                parent=base_styles["Normal"],
                fontName=self.font,
                fontSize=12,
                leading=18,
                textColor=COLORS["muted"],
                spaceAfter=0,
            ),
            "section": ParagraphStyle(
                "Section",
                parent=base_styles["Normal"],
                fontName=self.font_bold,
                fontSize=13,
                leading=18,
                textColor=COLORS["dark"],
                spaceBefore=16,
                spaceAfter=6,
            ),
            "body": ParagraphStyle(
                "Body",
                parent=base_styles["Normal"],
                fontName=self.font,
                fontSize=10,
                leading=14,
                textColor=COLORS["dark"],
            ),
            "small": ParagraphStyle(
                "Small",
                parent=base_styles["Normal"],
                fontName=self.font,
                fontSize=8,
                leading=10,
                textColor=COLORS["muted"],
            ),
            "kpi_value": ParagraphStyle(
                "KPIValue",
                parent=base_styles["Normal"],
                fontName=self.font_bold,
                fontSize=16,
                leading=20,
                textColor=COLORS["primary"],
                alignment=1,  # Center
            ),
            "kpi_label": ParagraphStyle(
                "KPILabel",
                parent=base_styles["Normal"],
                fontName=self.font,
                fontSize=8,
                leading=10,
                textColor=COLORS["muted"],
                alignment=1,  # Center
            ),
            # New Hero KPI style for Uptime
            "hero_kpi_value": ParagraphStyle(
                "HeroKPIValue",
                parent=base_styles["Normal"],
                fontName=self.font_bold,
                fontSize=34,
                leading=40,
                textColor=COLORS["primary"],
                alignment=1,
            ),
            "hero_kpi_label": ParagraphStyle(
                "HeroKPILabel",
                parent=base_styles["Normal"],
                fontName=self.font,
                fontSize=10,
                leading=12,
                textColor=COLORS["muted"],
                alignment=1,
                spaceBefore=2,
            ),
            # Supporting KPIs (smaller)
            "supporting_kpi_value": ParagraphStyle(
                "SupportingKPIValue",
                parent=base_styles["Normal"],
                fontName=self.font_bold,
                fontSize=14,
                leading=18,
                textColor=COLORS["dark"],
                alignment=1,
            ),
            "supporting_kpi_label": ParagraphStyle(
                "SupportingKPILabel",
                parent=base_styles["Normal"],
                fontName=self.font,
                fontSize=8,
                leading=10,
                textColor=COLORS["muted"],
                alignment=1,
            ),
            # Status banner
            "status_text": ParagraphStyle(
                "StatusText",
                parent=base_styles["Normal"],
                fontName=self.font_bold,
                fontSize=11,
                leading=14,
                textColor=COLORS["dark"],
            ),
        }
    
    def _get_whatweb_data(self) -> Optional[dict]:
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
        
        _log.debug("Fetching WhatWeb data for %s", http_url)
        try:
            self._whatweb_cache = run_whatweb(http_url, aggression=1, timeout=20)
        except ValueError as e:
            _log.warning("Invalid URL for whatweb: %s", e)
            self._whatweb_cache = None
        
        return self._whatweb_cache
    
    def build(self) -> bytes:
        """Build the complete PDF and return as bytes."""
        buffer = io.BytesIO()
        
        doc = SimpleDocTemplate(
            buffer,
            pagesize=self.page_size,
            leftMargin=self.margin,
            rightMargin=self.margin,
            topMargin=self.margin,
            bottomMargin=self.margin,
        )
        
        self._build_cover_page()
        
        # Page 2: Technical Summary
        self._add_technical_summary()
        self.elements.append(PageBreak())
        
        # Page 3: Network Path
        self._build_network_path_page()
        self.elements.append(PageBreak())
        
        # Page 4: Charts ("Availability & Performance")
        # Note: _build_charts_page() already ends with PageBreak()
        self._build_charts_page()
        
        # Page 5+: Downtime Log
        self._build_downtime_page()
        
        doc.build(self.elements, onFirstPage=self._add_header_footer, onLaterPages=self._add_header_footer)
        
        buffer.seek(0)
        return buffer.getvalue()
    
    def _add_header_footer(self, canvas, doc):
        """Add header and footer to each page matching website style."""
        # Header bar
        canvas.saveState()
        canvas.setFillColor(COLORS["primary"])
        canvas.rect(0, self.height - 8 * mm, self.width, 8 * mm, fill=1, stroke=0)
        
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
            except Exception:
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
        logo_x = hb_x + 13 * mm  # 1mm left from original 14mm
        if logo_path.exists():
            canvas.saveState()
            canvas.setFillAlpha(0.6)  # 40% transparency = 60% opacity
            canvas.drawImage(
                str(logo_path),
                logo_x,
                10.0 * mm,  # 0.5mm lower from 10.5mm
                width=logo_width,
                height=logo_height,
                preserveAspectRatio=True,
                anchor='sw',
                mask='auto',
            )
            canvas.restoreState()
        
        canvas.setFont(self.font, 7)
        canvas.setFillColor(COLORS["muted"])
        now = datetime.now(tz=timezone.utc)
        build_suffix = f" ({BUILD_INFO[:7]})" if BUILD_INFO and BUILD_INFO != "dev" else ""
        footer_text = f"v{VERSION}{build_suffix} | © {now.year} Gill-Bates | Lightweight Uptime Monitor | "
        footer_text_x = logo_x + logo_width - 2 * mm
        canvas.drawString(footer_text_x, 11 * mm, footer_text)
        
        # GitHub link (after footer text)
        github_url = "https://github.com/Gill-Bates/justUp"
        footer_text_width = canvas.stringWidth(footer_text, self.font, 7)
        github_x = footer_text_x + footer_text_width
        
        # Draw GitHub icon (14px * 0.7 * 0.8 = 7.84px for 50% smaller total, ~2.77mm at 72 DPI)
        # With 40% transparency (alpha=0.6)
        github_icon_path = STATIC_DIR / "github.png"
        icon_size = (14 * 0.7 * 0.8) / 72 * 25.4 * mm  # 7.84px converted to mm
        if github_icon_path.exists():
            canvas.saveState()
            canvas.setFillAlpha(0.6)  # 40% transparency = 60% opacity
            canvas.drawImage(
                str(github_icon_path),
                github_x,
                10.5 * mm,
                width=icon_size,
                height=icon_size,
                preserveAspectRatio=True,
                anchor='c',
                mask='auto',  # Handle PNG transparency correctly
            )
            canvas.restoreState()
        icon_width = icon_size + 0.8 * mm
        
        # Draw GitHub text after icon
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
        canvas.drawString(self.margin, 6 * mm, f"{domain}")
        canvas.drawRightString(self.width - self.margin, 6 * mm, f"Report generated at {timestamp}")
        
        canvas.restoreState()
    
    def _build_cover_page(self):
        """Build the redesigned cover/title page with status banner and hierarchical KPIs."""
        target = self.report_data.target
        from_date = self.report_data.from_date
        to_date = self.report_data.to_date
        
        # ─── Hero Zone (emotional, trust-building) ──────────────────────────────
        self.elements.append(Spacer(1, 20 * mm))
        
        # Domain/Host (largest element)
        domain = target.get("ping_host") or target.get("host") or target.get("http_url", "")
        if domain:
            # Strip protocol if present
            domain = domain.replace("https://", "").replace("http://", "").split("/")[0]
        else:
            domain = "Unknown Domain"
        
        # Auto-shrink domain font to fit on one line
        available_width = self.width - 2 * self.margin
        base_font_size = 30
        min_font_size = 14
        font_name = self.font_serif_bold
        
        # Calculate required font size to fit text
        text_width = pdfmetrics.stringWidth(domain, font_name, base_font_size)
        if text_width > available_width:
            # Scale down proportionally, but not below minimum
            scale_factor = available_width / text_width
            font_size = max(min_font_size, int(base_font_size * scale_factor))
        else:
            font_size = base_font_size
        
        # Create dynamic style for this domain
        domain_style = ParagraphStyle(
            "DomainTitleDynamic",
            fontName=font_name,
            fontSize=font_size,
            leading=font_size + 8,
            textColor=COLORS["dark"],
            spaceAfter=4,
        )
        self.elements.append(Paragraph(domain, domain_style))
        
        # Website title from WhatWeb (if available), otherwise target name
        whatweb_data = self._get_whatweb_data()
        website_title = whatweb_data.get("Title") if whatweb_data else None
        if not website_title:
            website_title = target.get("name", "")
        if website_title:
            self.elements.append(Paragraph(website_title, self.styles["website_title"]))
        
        self.elements.append(Spacer(1, 4 * mm))
        
        # Subtitle
        self.elements.append(Paragraph("Uptime &amp; Performance Report", self.styles["subtitle"]))
        
        # Gray hairline separator
        self.elements.append(HorizontalLine(
            color=colors.HexColor("#e0e0e0"),
            thickness=0.5,
            space_before=3 * mm,
            space_after=3 * mm
        ))

        # Report period (page 1, prominent under heading)
        range_days = (to_date - from_date).days + 1
        day_label = "day" if range_days == 1 else "days"
        period_text = (
            f"Report Period: {from_date.strftime('%Y-%m-%d')} – {to_date.strftime('%Y-%m-%d')} "
            f"({range_days} {day_label})"
        )
        self.elements.append(Paragraph(period_text, self.styles["report_period"]))
        
        self.elements.append(Spacer(1, 10 * mm))
        
        # ─── Status Banner (wider, more dominant) ───────────────────────────────
        self._add_status_banner()
        
        self.elements.append(Spacer(1, 20 * mm))
        
        # ─── Hero KPI (directly after status) ───────────────────────────────────
        self._add_hero_kpi()
        
        self.elements.append(Spacer(1, 8 * mm))
        
        # ─── Supporting KPIs ────────────────────────────────────────────────────
        self._add_supporting_kpis()
        
        # SLA Compliance section (if SLA enabled)
        if self.report_data.target.get("sla_enabled"):
            self.elements.append(Spacer(1, 8 * mm))
            self._add_sla_compliance()
        
        self.elements.append(PageBreak())
    
    def _add_status_banner(self):
        """Add a color-coded status banner showing operational status."""
        kpis = self.kpis
        
        # Determine status based on uptime: >=99% green, >=95% yellow, <95% red
        if kpis.uptime_percent >= 99:
            status = "Operational"
            status_detail = f"Service Healthy · {kpis.uptime_percent}% uptime"
            bg_color = colors.HexColor("#e8f5e9")  # Light green
            text_color = colors.HexColor("#2e7d32")  # Dark green
            icon = "●"
        elif kpis.uptime_percent >= 95:
            status = "Degraded"
            status_detail = f"Intermittent Issues · {kpis.downtime_minutes} min downtime"
            bg_color = colors.HexColor("#fff8e1")  # Light amber
            text_color = colors.HexColor("#f57c00")  # Dark amber
            icon = "◐"
        else:
            status = "Degraded"
            status_detail = f"Service Disruption · {kpis.downtime_minutes} min downtime"
            bg_color = colors.HexColor("#ffebee")  # Light red
            text_color = colors.HexColor("#c62828")  # Dark red
            icon = "○"
        
        # Create status banner as a table with background (wider, more dominant)
        status_style = ParagraphStyle(
            "StatusBanner",
            fontName=self.font_bold,
            fontSize=13,
            leading=16,
            textColor=text_color,
        )
        detail_style = ParagraphStyle(
            "StatusDetail",
            fontName=self.font,
            fontSize=10,
            leading=13,
            textColor=text_color,
        )
        
        status_para = Paragraph(f"{icon} {status}", status_style)
        detail_para = Paragraph(status_detail, detail_style)
        
        # Layout: status left, detail right - wider banner (90% width)
        available_width = self.width - 2 * self.margin
        table_data = [[status_para, detail_para]]
        table = Table(table_data, colWidths=[55 * mm, available_width - 55 * mm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), bg_color),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('ROUNDEDCORNERS', [4, 4, 4, 4]),
        ]))
        
        self.elements.append(table)
    
    def _add_hero_kpi(self):
        """Add Uptime as the hero KPI (larger, more prominent)."""
        kpis = self.kpis
        
        from app.utils.formatters import format_percent
        
        # Determine uptime color: >=99% green, >=95% yellow, <95% red
        if kpis.uptime_percent >= 99:
            uptime_color = COLORS["primary"]
        elif kpis.uptime_percent >= 95:
            uptime_color = COLORS["warning"]
        else:
            uptime_color = COLORS["danger"]
        
        # Hero KPI card (wider, centered)
        hero_width = 75 * mm
        hero_height = 38 * mm
        
        hero_card = RoundedKPICard(
            label="Uptime",
            value=format_percent(kpis.uptime_percent),
            width=hero_width,
            height=hero_height,
            value_color=uptime_color,
            font=self.font,
            font_bold=self.font_bold,
            value_size=34,
            label_size=11,
            border_width=0.3,
        )
        
        # Center the hero KPI
        table = Table([[hero_card]], colWidths=[hero_width])
        table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ]))
        table.hAlign = 'CENTER'
        
        self.elements.append(table)
    
    def _add_supporting_kpis(self):
        """Add supporting KPIs (smaller, secondary) - flush with page margins."""
        kpis = self.kpis
        
        # Downtime in red if > 0
        downtime_color = COLORS["danger"] if kpis.downtime_minutes > 0 else COLORS["muted"]
        
        # Calculate available width and card dimensions to fill margin-to-margin
        available_width = self.width - 2 * self.margin
        
        # Determine number of cards (3 or 4 depending on ping)
        has_ping = kpis.avg_ping_ms is not None
        num_cards = 4 if has_ping else 3
        
        # Gap between cards
        gap = 4 * mm
        
        # Calculate card width to fill available space
        total_gap = gap * (num_cards - 1)
        card_width = (available_width - total_gap) / num_cards
        card_height = 24 * mm
        
        cards = [
            RoundedKPICard(
                label="Avg Response",
                value=f"{kpis.avg_response_ms} ms",
                width=card_width,
                height=card_height,
                value_color=COLORS["dark"],
                font=self.font,
                font_bold=self.font_bold,
                value_size=14,
                label_size=7,
                border_width=0.3,
            ),
            RoundedKPICard(
                label="Max Response",
                value=f"{kpis.max_response_ms} ms",
                width=card_width,
                height=card_height,
                value_color=COLORS["dark"],
                font=self.font,
                font_bold=self.font_bold,
                value_size=14,
                label_size=7,
                border_width=0.3,
            ),
            RoundedKPICard(
                label="Downtime",
                value=f"{kpis.downtime_minutes} min",
                width=card_width,
                height=card_height,
                value_color=downtime_color,
                font=self.font,
                font_bold=self.font_bold,
                value_size=14,
                label_size=7,
                border_width=0.3,
            ),
        ]
        
        # Add Avg Ping if available
        if has_ping:
            cards.append(RoundedKPICard(
                label="Avg Ping",
                value=f"{kpis.avg_ping_ms} ms",
                width=card_width,
                height=card_height,
                value_color=COLORS["dark"],
                font=self.font,
                font_bold=self.font_bold,
                value_size=14,
                label_size=7,
                border_width=0.3,
            ))
        
        # Arrange in a row - each column gets card_width, gaps handled by padding
        col_widths = [card_width + gap] * (num_cards - 1) + [card_width]
        table = Table([cards], colWidths=col_widths)
        table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        table.hAlign = 'LEFT'
        
        self.elements.append(table)
    
    def _add_sla_compliance(self):
        """Add SLA compliance section with deviation analysis."""
        target = self.report_data.target
        kpis = self.kpis
        
        # Get SLA targets
        sla_availability = target.get("sla_availability_pct")
        sla_response = target.get("sla_response_time_ms")
        sla_ping = target.get("sla_ping_latency_ms")
        
        deviations: list[SLADeviation] = []
        
        # Availability comparison
        if sla_availability is not None:
            actual = kpis.uptime_percent
            deviation = actual - sla_availability
            deviations.append(SLADeviation(
                metric_name="Availability",
                sla_target=sla_availability,
                actual_value=actual,
                deviation=deviation,
                deviation_pct=deviation,  # Already in percent
                is_compliant=actual >= sla_availability,
                unit="%",
            ))
        
        # Response time comparison
        if sla_response is not None and kpis.avg_response_ms > 0:
            actual = kpis.avg_response_ms
            deviation = actual - sla_response
            deviation_pct = (deviation / sla_response * 100) if sla_response > 0 else 0
            deviations.append(SLADeviation(
                metric_name="Response Time",
                sla_target=sla_response,
                actual_value=actual,
                deviation=deviation,
                deviation_pct=deviation_pct,
                is_compliant=actual <= sla_response,  # Lower is better for latency
                unit="ms",
            ))
        
        # Ping latency comparison
        if sla_ping is not None and kpis.avg_ping_ms is not None:
            actual = kpis.avg_ping_ms
            deviation = actual - sla_ping
            deviation_pct = (deviation / sla_ping * 100) if sla_ping > 0 else 0
            deviations.append(SLADeviation(
                metric_name="Ping Latency",
                sla_target=sla_ping,
                actual_value=actual,
                deviation=deviation,
                deviation_pct=deviation_pct,
                is_compliant=actual <= sla_ping,  # Lower is better for latency
                unit="ms",
            ))
        
        if not deviations:
            return
        
        # Build SLA compliance table
        header_style = ParagraphStyle(
            "SLAHeader",
            fontName=self.font_bold,
            fontSize=9,
            textColor=COLORS["dark"],
        )
        cell_style = ParagraphStyle(
            "SLACell",
            fontName=self.font,
            fontSize=9,
            textColor=COLORS["dark"],
        )
        
        # Table header
        table_data = [[
            Paragraph("Metric", header_style),
            Paragraph("SLA Target", header_style),
            Paragraph("Actual", header_style),
            Paragraph("Deviation", header_style),
            Paragraph("Status", header_style),
        ]]
        
        for d in deviations:
            # Format deviation with sign
            if d.metric_name == "Availability":
                dev_text = f"{d.deviation:+.2f}%"
            else:
                dev_text = f"{d.deviation:+.1f} {d.unit} ({d.deviation_pct:+.1f}%)"
            
            # Status indicator
            if d.is_compliant:
                status_text = '<font color="#198754">✓ Compliant</font>'
            else:
                status_text = '<font color="#dc3545">✗ Non-Compliant</font>'
            
            table_data.append([
                Paragraph(d.metric_name, cell_style),
                Paragraph(f"{d.sla_target} {d.unit}", cell_style),
                Paragraph(f"{d.actual_value} {d.unit}", cell_style),
                Paragraph(dev_text, cell_style),
                Paragraph(status_text, cell_style),
            ])
        
        col_widths = [40 * mm, 30 * mm, 30 * mm, 40 * mm, 32 * mm]
        table = Table(table_data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), COLORS["light"]),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), self.font_bold),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, COLORS["divider"]),
            ('LINEBELOW', (0, -1), (-1, -1), 0.5, COLORS["divider"]),
        ]))
        
        # Section title
        sla_title = ParagraphStyle(
            "SLATitle",
            fontName=self.font_bold,
            fontSize=10,
            textColor=COLORS["dark"],
            spaceBefore=4,
            spaceAfter=4,
        )
        self.elements.append(Paragraph("SLA Compliance", sla_title))
        self.elements.append(table)
    
    def _add_check_badges(self):
        """Add badges showing which checks are enabled (green=active, info=secondary)."""
        target = self.report_data.target
        
        # Define checks with their colors
        checks = [
            ("HTTP", target.get("enable_http_check"), COLORS["success"]),
            ("PING", target.get("enable_ping"), COLORS["success"]),
            ("Cert", target.get("enable_cert_expiration"), COLORS["success"]),
            ("TCP", target.get("enable_tcp_connect"), COLORS["muted"]),
        ]
        
        # Only show enabled checks
        enabled_checks = [(name, color) for name, enabled, color in checks if enabled]
        
        if not enabled_checks:
            return
        
        # Create Badge flowables with differentiated colors
        badge_flowables = [
            Badge(
                text=name,
                font=self.font,
                bg_color=color,
                padding_y=1 * mm,
            )
            for name, color in enabled_checks
        ]
        
        # Label paragraph
        label_para = Paragraph("Checks:", self.styles["small"])
        
        row_content = [label_para] + badge_flowables
        
        # Calculate column widths
        col_widths = [18 * mm] + [b.badge_width + 4 * mm for b in badge_flowables]
        
        table = Table([row_content], colWidths=col_widths)
        table.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ]))
        table.hAlign = 'LEFT'
        
        self.elements.append(table)
    
    def _add_provider_info(self):
        """Add provider info section (IP, ASN, Provider, Location)."""
        target = self.report_data.target
        
        # Determine the host to lookup (prefer http_url host, fallback to ping_host, then host)
        lookup_host = None
        http_url = target.get("http_url", "")
        if http_url:
            # Extract host from URL
            try:
                parsed = urlparse(http_url)
                lookup_host = parsed.hostname
            except Exception:
                pass
        
        if not lookup_host:
            lookup_host = target.get("ping_host") or target.get("host") or target.get("cert_host")
        
        if not lookup_host:
            return
        
        # Fetch provider info
        info = lookup_provider_info(lookup_host)
        
        # Build info table
        label_style = ParagraphStyle(
            "ProviderLabel",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["muted"],
        )
        value_style = ParagraphStyle(
            "ProviderValue",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["dark"],
        )
        
        # Build location with country code (ReportLab doesn't support emoji flags)
        country_code = info["country_code"]
        if country_code and info["location"] != "-":
            location_text = f"{info['location']} ({country_code})"
        else:
            location_text = info["location"]
        
        # Two-column layout: Label | Value | Label | Value
        data = [
            [
                Paragraph("IP Address:", label_style),
                Paragraph(info["server_ip"], value_style),
                Paragraph("ASN:", label_style),
                Paragraph(info["asn"], value_style),
            ],
            [
                Paragraph("Provider:", label_style),
                Paragraph(info["provider"], value_style),
                Paragraph("Location:", label_style),
                Paragraph(location_text, value_style),
            ],
        ]
        
        col_widths = [18 * mm, 45 * mm, 15 * mm, 55 * mm]
        table = Table(data, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 1),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        table.hAlign = 'LEFT'
        
        self.elements.append(table)
    
    def _add_detected_stack(self):
        """Add Detected Technology Stack section from whatweb scan."""
        # Use cached WhatWeb data
        stack = self._get_whatweb_data()
        
        if not stack:
            _log.debug("No tech stack detected for target")
            return
        
        # Section title (matching Network Path geographic view style)
        section_style = ParagraphStyle(
            "StackTitle",
            fontName=self.font_bold,
            fontSize=10,
            leading=14,
            textColor=COLORS["dark"],
            spaceBefore=6,
            spaceAfter=4,
        )
        self.elements.append(Paragraph("Detected Technology Stack", section_style))
        
        # Build info table
        label_style = ParagraphStyle(
            "StackLabel",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["muted"],
        )
        value_style = ParagraphStyle(
            "StackValue",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["dark"],
        )
        
        # Security status (new structure)
        sec = stack.get("security", {})
        https_status = "Yes" if sec.get("https") else "No"
        hsts_status = "Yes" if sec.get("hsts") else "No"
        xfo_status = "Yes" if sec.get("xfo") else "No"
        debug_status = "Yes (!)" if sec.get("debug_headers") else "No"
        
        # Build rows (compact, no frameworks)
        rows = [
            [
                Paragraph("Web Server", label_style),
                Paragraph(stack.get("webserver") or "-", value_style),
                Paragraph("CMS", label_style),
                Paragraph(stack.get("cms") or "-", value_style),
            ],
            [
                Paragraph("Content Lang", label_style),
                Paragraph(stack.get("content_language") or stack.get("language") or "-", value_style),
                Paragraph("Country", label_style),
                Paragraph(stack.get("country") or "-", value_style),
            ],
            [
                Paragraph("App/Runtime", label_style),
                Paragraph(stack.get("programming_language") or "-", value_style),
                Paragraph("IP", label_style),
                Paragraph(stack.get("ip") or stack.get("server_ip") or "-", value_style),
            ],
            [
                Paragraph("HTTPS", label_style),
                Paragraph(https_status, value_style),
                Paragraph("HSTS", label_style),
                Paragraph(hsts_status, value_style),
            ],
            [
                Paragraph("X-Frame-Options", label_style),
                Paragraph(xfo_status, value_style),
                Paragraph("Debug Headers", label_style),
                Paragraph(debug_status, value_style),
            ],
        ]
        
        col_widths = [22 * mm, 63 * mm, 22 * mm, 48 * mm]
        table = Table(rows, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 1),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        table.hAlign = 'LEFT'
        
        self.elements.append(table)
    
    def _get_ssl_data(self) -> Optional[SSLSummary]:
        """Fetch and cache SSLyze data for the target.
        
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
            # Use explicit cert settings
            port = cert_port or http_port or 443
            ssl_target = f"https://{cert_host}:{port}"
            _log.debug("SSLyze using explicit cert_host: %s", ssl_target)
        else:
            # Priority 2: Extract from http_url (may contain custom port)
            http_url = target.get("http_url") or target.get("url") or ""
            
            if not http_url:
                # Fallback: Build from host
                host = target.get("host", "")
                if host:
                    http_url = f"https://{host}"
            
            if not http_url.startswith("https://"):
                _log.debug("Skipping SSL scan for non-HTTPS target: %s", http_url)
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
            except Exception:
                ssl_target = http_url
        
        _log.debug("Fetching SSLyze data for %s", ssl_target)
        try:
            self._ssl_cache = scan_ssl(ssl_target, timeout=15)
        except Exception as e:
            _log.warning("SSLyze scan failed: %s", e)
            self._ssl_cache = None
        
        return self._ssl_cache

    def _add_ssl_summary(self):
        """Add SSL/TLS Security Summary section from SSLyze scan."""
        ssl = self._get_ssl_data()
        
        if not ssl:
            _log.debug("No SSL data available for target")
            return
        
        # Section title
        section_style = ParagraphStyle(
            "SSLTitle",
            fontName=self.font_bold,
            fontSize=10,
            leading=14,
            textColor=COLORS["dark"],
            spaceBefore=6,
            spaceAfter=4,
        )
        self.elements.append(Paragraph("SSL/TLS Security", section_style))
        
        # Build info table
        label_style = ParagraphStyle(
            "SSLLabel",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["muted"],
        )
        value_style = ParagraphStyle(
            "SSLValue",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["dark"],
        )
        warn_style = ParagraphStyle(
            "SSLWarn",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["danger"],
        )
        ok_style = ParagraphStyle(
            "SSLOK",
            fontName=self.font,
            fontSize=8,
            textColor=COLORS["success"],
        )
        
        # Protocol support summary
        protocols = []
        if ssl.supports_tls13:
            protocols.append("TLS 1.3")
        if ssl.supports_tls12:
            protocols.append("TLS 1.2")
        if ssl.supports_tls11:
            protocols.append("TLS 1.1 (!)")
        if ssl.supports_tls10:
            protocols.append("TLS 1.0 (!)")
        if ssl.supports_ssl3:
            protocols.append("SSL 3.0 (!)")
        if ssl.supports_ssl2:
            protocols.append("SSL 2.0 (!)")
        protocols_text = ", ".join(protocols) if protocols else "-"
        
        # Certificate validity
        if ssl.cert_days_remaining > 30:
            days_text = f"{ssl.cert_days_remaining} days"
            days_style = ok_style
        elif ssl.cert_days_remaining > 0:
            days_text = f"{ssl.cert_days_remaining} days (!)"
            days_style = warn_style
        else:
            days_text = "Expired!"
            days_style = warn_style
        
        # Trust status
        trust_text = "Yes" if ssl.certificate_trusted else "No (!)"
        trust_style = ok_style if ssl.certificate_trusted else warn_style
        
        # Key info
        key_text = f"{ssl.cert_key_type} {ssl.cert_key_size}-bit" if ssl.cert_key_size else "-"
        
        # Preferred cipher (show TLS 1.3 if available, otherwise TLS 1.2)
        cipher = ssl.tls13_cipher or ssl.tls12_cipher or "-"
        # Truncate long cipher names
        if len(cipher) > 35:
            cipher = cipher[:32] + "..."
        
        # Vulnerabilities check
        vulns = []
        if ssl.vulnerable_heartbleed:
            vulns.append("Heartbleed")
        if ssl.vulnerable_ccs_injection:
            vulns.append("CCS Injection")
        if ssl.vulnerable_robot:
            vulns.append("ROBOT")
        if ssl.supports_compression:
            vulns.append("CRIME (compression)")
        if ssl.has_weak_protocols:
            vulns.append("Weak protocols")
        if ssl.has_sha1_signature:
            vulns.append("SHA-1 signature")
        
        if vulns:
            vuln_text = ", ".join(vulns)
            vuln_style = warn_style
        else:
            vuln_text = "None detected"
            vuln_style = ok_style
        
        # Build rows
        rows = [
            [
                Paragraph("Certificate", label_style),
                Paragraph(ssl.cert_subject[:50] + "..." if len(ssl.cert_subject) > 50 else ssl.cert_subject, value_style),
                Paragraph("Issuer", label_style),
                Paragraph(ssl.cert_issuer[:40] + "..." if len(ssl.cert_issuer) > 40 else ssl.cert_issuer, value_style),
            ],
            [
                Paragraph("Valid Until", label_style),
                Paragraph(ssl.cert_valid_until, value_style),
                Paragraph("Days Left", label_style),
                Paragraph(days_text, days_style),
            ],
            [
                Paragraph("Key", label_style),
                Paragraph(key_text, value_style),
                Paragraph("Trusted", label_style),
                Paragraph(trust_text, trust_style),
            ],
            [
                Paragraph("Protocols", label_style),
                Paragraph(protocols_text, warn_style if ssl.has_weak_protocols else value_style),
                Paragraph("Secure Reneg.", label_style),
                Paragraph("Yes" if ssl.supports_secure_renegotiation else "No (!)", 
                         ok_style if ssl.supports_secure_renegotiation else warn_style),
            ],
            [
                Paragraph("Preferred Cipher", label_style),
                Paragraph(cipher, value_style),
                Paragraph("Vulnerabilities", label_style),
                Paragraph(vuln_text, vuln_style),
            ],
        ]
        
        col_widths = [22 * mm, 63 * mm, 22 * mm, 48 * mm]
        table = Table(rows, colWidths=col_widths)
        table.setStyle(TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 0), (-1, -1), 1),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        table.hAlign = 'LEFT'
        
        self.elements.append(table)
    
    def _add_traceroute(self):
        """Add MTR network path section showing hops to target."""
        display_hops, geo_points, has_interpolation = self._get_mtr_data(max_hops=20)
        if not display_hops:
            return
        
        # Section label
        title_style = ParagraphStyle(
            "TracerouteTitle",
            fontName=self.font_bold,
            fontSize=10,
            leading=14,
            textColor=COLORS["dark"],
            spaceBefore=6,
            spaceAfter=4,
        )
        self.elements.append(Paragraph("Network Path (MTR)", title_style))

        def _status_badge(text: str, *, bg, text_color=colors.white) -> Badge:
            # Match the visual language of "Checks" badges, but slightly smaller
            return Badge(
                text=text,
                font=self.font,
                font_size=7,
                padding_x=2.2 * mm,
                padding_y=0.9 * mm,
                corner_radius=1.6 * mm,
                bg_color=bg,
                text_color=text_color,
                bold=True,
            )
        
        # Build table data with columns: Hop, Network Path (IP + Host), Location, Latency, Loss, Status
        table_data = [["Hop", "Network Path", "Location", "Latency", "Loss", "Status"]]
        
        for hop in display_hops:
            is_collapsed = hop.get("_collapsed", False)
            is_interpolated = hop.get("_interpolated", False)
            
            # Use display fields from format_hop_for_display()
            hop_num = hop.get("hop_display") or str(hop.get("hop", "?"))
            
            # IPv6 OPTIMIZATION: Use shortened IP if available, otherwise shorten manually
            ip = hop.get("ip_display")
            if not ip or ip == "—":
                ip_raw = hop.get("ip")
                if ip_raw and ip_raw not in ("-", "*", None, "—"):
                    # Import here to avoid circular dependency
                    from app.services.traceroute_geo import shorten_ipv6_for_display
                    ip = shorten_ipv6_for_display(ip_raw, max_length=22)  # Slightly shorter for PDF
                else:
                    ip = "—"
            
            # Build Network Path cell: IP on first line, hostname in gray on second line
            hostname = hop.get("hostname_display") or (hop.get("hostname") if hop.get("hostname") not in ("-", None) else "—")
            
            # Create IP + Hostname cell (hostname in smaller gray text below IP)
            if hostname and hostname != "—":
                # Use Paragraph for multi-line cell with different font sizes/colors
                network_path_html = f'''{ip}<br/>
                <font size="6" color="{COLORS['muted']}">{hostname}</font>'''
                network_path_cell = Paragraph(network_path_html, ParagraphStyle(
                    "NetworkPathCell",
                    fontName=self.font,
                    fontSize=8,
                    leading=10,
                ))
            else:
                network_path_cell = ip
            
            # Build city string with country in parentheses
            city = hop.get("city", "")
            country = hop.get("country", "")
            if is_collapsed:
                city_str = "—"
            elif city and country:
                city_str = f"{city} ({country})"
            elif city:
                city_str = city
            elif country:
                city_str = f"({country})"
            else:
                city_str = "—"
            
            # Format latency: show avg (or "—" if None)
            latency_ms = hop.get("latency_ms")
            if latency_ms and isinstance(latency_ms, dict):
                avg = latency_ms.get("avg")
                if avg is not None and avg > 0:
                    latency_str = f"{avg:.1f} ms"
                else:
                    latency_str = "—"
            else:
                latency_str = "—"
            
            # Format loss: show percentage (or "—" if 100% / timeout)
            loss_pct = hop.get("loss_pct")
            if loss_pct is not None and loss_pct < 100:
                loss_str = f"{loss_pct:.0f}%"
            else:
                loss_str = "—"
            
            status_str = hop.get("status_display") or hop.get("status", "timeout")
			
            # Create colored status badge (rounded, like "Checks")
            status_lower = str(status_str).strip().lower()
            if status_lower == "ok":
                status_cell = _status_badge(status_str, bg=COLORS["success"])
            elif status_lower in ("n/a", "timeout", "—"):
                status_cell = _status_badge(status_str, bg=COLORS["muted"])
            elif status_lower == "interpolated":
                # Show interpolated hops as gray "n/a"
                status_cell = _status_badge("n/a", bg=COLORS["muted"])
            else:
                status_cell = _status_badge(status_str, bg=COLORS["warning"], text_color=COLORS["dark"])
            
            table_data.append([
                hop_num,
                network_path_cell,
                city_str,
                latency_str,
                loss_str,
                status_cell,
            ])
        
        # Calculate available content width for full-width table
        content_width = self.width - 2 * self.margin
        # Column distribution: Hop 6%, Network Path 44%, City 18%, Latency 12%, Loss 10%, Status 10%
        col_widths = [
            content_width * 0.06,  # Hop
            content_width * 0.44,  # Network Path (IP + Host merged)
            content_width * 0.18,  # Location
            content_width * 0.12,  # Latency
            content_width * 0.10,  # Loss
            content_width * 0.10,  # Status
        ]
        
        table = Table(table_data, colWidths=col_widths)
        
        # Build dynamic styles for interpolated rows
        table_styles = [
            # Header row
            ('FONTNAME', (0, 0), (-1, 0), self.font_bold),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('TEXTCOLOR', (0, 0), (-1, 0), COLORS["dark"]),
            ('BACKGROUND', (0, 0), (-1, 0), COLORS["light"]),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
            ('TOPPADDING', (0, 0), (-1, 0), 4),
            ('ALIGN', (0, 0), (-1, 0), 'LEFT'),
            
            # Data rows - compact spacing (~10% reduced)
            ('FONTNAME', (0, 1), (-1, -1), self.font),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('TEXTCOLOR', (0, 1), (-1, -1), COLORS["dark"]),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
            ('TOPPADDING', (0, 1), (-1, -1), 4),
            ('ALIGN', (0, 1), (0, -1), 'LEFT'),
            
            # Alternating row colors
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [COLORS["white"], COLORS["light"]]),
            
            # Light gray hairline border
            ('BOX', (0, 0), (-1, -1), 0.5, COLORS["divider"]),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, COLORS["divider"]),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]
        
        # Add muted text color for interpolated/collapsed rows
        for row_idx, hop in enumerate(display_hops, start=1):
            if hop.get("_interpolated") or hop.get("_collapsed"):
                table_styles.append(('TEXTCOLOR', (0, row_idx), (-1, row_idx), COLORS["muted"]))
        
        table.setStyle(TableStyle(table_styles))
        
        self.elements.append(table)
        
        # Add interpolation note if applicable
        if has_interpolation:
            # Info icon from Material Icons (codepoint U+E88E = 'info')
            info_icon = '\ue88e'
            note_style = ParagraphStyle(
                "InterpolationNote",
                fontName=self.font,
                fontSize=7,
                textColor=COLORS["muted"],
                spaceBefore=2,
                spaceAfter=4,
            )
            icon_style = ParagraphStyle(
                "InfoIcon",
                fontName="MaterialIcons",
                fontSize=8,
                textColor=COLORS["muted"],
            )
            # Combine icon and text using inline font switching
            note_text = f'<font name="MaterialIcons" size="8">{info_icon}</font> Note: Intermediate routers do not respond to ICMP. Route was logically interpolated.'
            self.elements.append(Paragraph(note_text, note_style))
    
    def _add_traceroute_geomap(self):
        """Render traceroute geographic visualization (600 DPI world map)."""
        try:
            display_hops, geo_points, has_interpolation = self._get_mtr_data(max_hops=20)
            if not display_hops or not geo_points:
                return
            
            # Calculate available width: page width minus margins
            # self.page_size[0] and self.margin are in points (1 inch = 72 points)
            available_width_pt = self.page_size[0] - 2 * self.margin
            available_width_in = available_width_pt / 72  # Convert points to inches
            
            # Fixed box height for consistent PDF layout
            box_height_pt = 100 * mm
            box_height_in = box_height_pt / 72  # points -> inches
            
            geo_map = render_traceroute_worldmap(
                geo_points,
                width_in=available_width_in,
                height_in=box_height_in,
                dpi=PDF_DPI,
            )

            if geo_map is None:
                _log.warning("World map rendering returned None")
                return
            
            # Full-width image derives height from actual image aspect ratio
            self._add_full_width_image("Network Path (Geographic View)", geo_map)
        except Exception as e:
            _log.warning("Failed to render MTR geo-map: %s", e)

    def _get_traceroute_host(self) -> str | None:
        """Pick a stable traceroute host for this report."""
        target = self.report_data.target
        http_url = target.get("http_url", "")
        if http_url:
            try:
                parsed = urlparse(http_url)
                if parsed.hostname:
                    return parsed.hostname
            except Exception:
                pass

        return target.get("ping_host") or target.get("host") or target.get("cert_host")

    def _get_mtr_data(self, *, max_hops: int = 20) -> tuple[list[dict[str, Any]], list[dict], bool]:
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
        
        # Strip trailing pure timeouts (keep interpolated hops)
        while (
            display_hops
            and display_hops[-1].get("status") == "timeout"
            and not display_hops[-1].get("_interpolated")
        ):
            display_hops.pop()
        
        if not display_hops:
            return [], [], False

        result = (display_hops, geo_points, has_interpolation)
        self._mtr_cache[cache_key] = result
        return result
    
    def _add_technical_summary(self):
        """Add Technical Summary section with check badges, provider info, and traceroute."""
        # Section title (same style as Availability & Performance)
        self.elements.append(Paragraph("Technical Summary", self.styles["section"]))
        
        # Hairline separator
        self.elements.append(HorizontalLine(
            color=colors.HexColor("#e0e0e0"),
            thickness=0.5,
            space_before=1 * mm,
            space_after=4 * mm
        ))
        
        # Check badges
        self._add_check_badges()
        self.elements.append(Spacer(1, 4 * mm))
        
        # Provider info
        self._add_provider_info()
        self.elements.append(Spacer(1, 4 * mm))
        
        # Detected Technology Stack (whatweb)
        self._add_detected_stack()
        self.elements.append(Spacer(1, 4 * mm))
        
        # SSL/TLS Security Summary (sslyze)
        self._add_ssl_summary()

    def _build_network_path_page(self):
        """Build the Network Path page with geo-map and traceroute table."""
        self.elements.append(Paragraph("Network Path", self.styles["section"]))
        
        # Hairline separator
        self.elements.append(HorizontalLine(
            color=colors.HexColor("#e0e0e0"),
            thickness=0.5,
            space_before=1 * mm,
            space_after=4 * mm
        ))

        # Network Path Geo-Map (includes its own title)
        self._add_traceroute_geomap()
        
        self.elements.append(Spacer(1, 6 * mm))
        
        # Traceroute table directly below the geo-map
        self._add_traceroute()
    
    def _build_charts_page(self):
        """Build the page(s) with charts."""
        self.elements.append(Paragraph("Availability &amp; Performance", self.styles["section"]))
        
        # Hairline separator
        self.elements.append(HorizontalLine(
            color=colors.HexColor("#e0e0e0"),
            thickness=0.5,
            space_before=1 * mm,
            space_after=4 * mm
        ))
        
        # Get SLA values from target (with defaults when enabled)
        sla_enabled = self.report_data.target.get("sla_enabled", False)
        if sla_enabled:
            sla_response_ms = self.report_data.target.get("sla_response_time_ms") or 500
            sla_ping_ms = self.report_data.target.get("sla_ping_latency_ms") or 100
        else:
            sla_response_ms = None
            sla_ping_ms = None
        
        # Availability Timeline
        uptime_chart = _create_uptime_chart(
            self.report_data.uptime_data,
            from_date=self.report_data.from_date,
            to_date=self.report_data.to_date
        )
        if uptime_chart:
            self._add_chart_section("Availability Timeline", uptime_chart)
            self.elements.append(Spacer(1, 8 * mm))
        
        # Response Time (with SLA line if SLA enabled)
        response_chart = _create_response_time_chart(
            self.report_data.http_data,
            from_date=self.report_data.from_date,
            to_date=self.report_data.to_date,
            sla_threshold_ms=int(sla_response_ms) if sla_response_ms else None
        )
        if response_chart:
            self._add_chart_section("Response Time (ms)", response_chart)
            self.elements.append(Spacer(1, 8 * mm))
        
        # HTTP Status Codes
        if self.report_data.status_code_data:
            status_chart = _create_status_code_chart(self.report_data.status_code_data)
            if status_chart:
                self._add_chart_section("HTTP Status Codes", status_chart)
                self.elements.append(Spacer(1, 8 * mm))
        
        # Ping Latency (with SLA line if configured)
        if self.report_data.target.get("enable_ping") and self.report_data.ping_data:
            ping_chart = _create_ping_chart(
                self.report_data.ping_data,
                from_date=self.report_data.from_date,
                to_date=self.report_data.to_date,
                sla_threshold_ms=int(sla_ping_ms) if sla_ping_ms else None
            )
            if ping_chart:
                self._add_chart_section("Ping Latency (ms)", ping_chart)
                self.elements.append(Spacer(1, 8 * mm))
        
        # TCP Latency (single or multi-port) - directly after PING on same page
        if self.report_data.target.get("enable_tcp_connect"):
            tcp_chart = None
            
            # Try multi-port chart first if we have per-port data
            if self.report_data.tcp_port_data:
                tcp_chart = _create_multi_port_tcp_chart(
                    self.report_data.tcp_port_data,
                    from_date=self.report_data.from_date,
                    to_date=self.report_data.to_date
                )
            
            # Fallback to single-port chart
            if not tcp_chart and self.report_data.tcp_data:
                tcp_chart = _create_tcp_chart(
                    self.report_data.tcp_data,
                    from_date=self.report_data.from_date,
                    to_date=self.report_data.to_date
                )
            
            if tcp_chart:
                self._add_chart_section("TCP Latency (ms)", tcp_chart)
                self.elements.append(Spacer(1, 8 * mm))
        
        self.elements.append(PageBreak())
    
    def _add_chart_section(self, title: str, chart_bytes: bytes, max_height_mm: float = 55):
        """Add a chart with title to the document, keeping them together on the same page."""
        # Title
        title_style = ParagraphStyle(
            "ChartTitle",
            fontName=self.font_bold,
            fontSize=10,
            leading=14,
            textColor=COLORS["dark"],
            spaceBefore=6,
            spaceAfter=4,
        )
        title_para = Paragraph(title, title_style)
        
        # Chart image
        img_buffer = io.BytesIO(chart_bytes)
        img = Image(img_buffer)
        
        # Scale to fit width
        available_width = self.width - 2 * self.margin
        aspect = img.imageWidth / img.imageHeight
        img.drawWidth = available_width
        img.drawHeight = available_width / aspect
        
        # Limit height (configurable per chart type)
        max_height = max_height_mm * mm
        if img.drawHeight > max_height:
            img.drawHeight = max_height
            img.drawWidth = max_height * aspect
        
        # Use KeepTogether to prevent page break between title and chart
        self.elements.append(KeepTogether([title_para, img, Spacer(1, 4 * mm)]))
    
    def _add_full_width_image(self, title: str, image_bytes: bytes | None):
        """Add a full-width image frame (for maps).

        Width-based scaling: fills available page width, height derived from
        the image aspect ratio. This avoids white side margins.
        """
        # Title
        title_style = ParagraphStyle(
            "MapTitle",
            fontName=self.font_bold,
            fontSize=10,
            leading=14,
            textColor=COLORS["dark"],
            spaceBefore=6,
            spaceAfter=4,
        )
        title_para = Paragraph(title, title_style)

        available_width = self.width - 2 * self.margin
        MAX_HEIGHT = 100 * mm  # Cap to avoid overly tall images

        if image_bytes:
            img_buffer = io.BytesIO(image_bytes)
            img = Image(img_buffer)

            # Width-driven scaling: fill page width, derive height from aspect
            aspect_wh = img.imageWidth / img.imageHeight  # width/height (standard convention)
            img.drawWidth = available_width
            img.drawHeight = available_width / aspect_wh

            # Cap height if image would be too tall
            if img.drawHeight > MAX_HEIGHT:
                img.drawHeight = MAX_HEIGHT
                img.drawWidth = MAX_HEIGHT * aspect_wh

            # Wrap image in a table with gray hairline border
            img_table = Table(
                [[img]],
                colWidths=[img.drawWidth],
                rowHeights=[img.drawHeight],
            )
            img_table.setStyle(TableStyle([
                ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor("#c0c0c0")),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]))

            self.elements.append(KeepTogether([title_para, img_table]))
        else:
            # Placeholder for missing image
            fixed_height = available_width * 0.5
            placeholder = Paragraph(
                "Map unavailable for this traceroute.",
                ParagraphStyle(
                    "MapPlaceholder",
                    parent=self.styles["small"],
                    textColor=COLORS["muted"],
                    alignment=1,  # center
                ),
            )
            frame = Table(
                [[placeholder]],
                colWidths=[available_width],
                rowHeights=[fixed_height],
            )
            frame.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.5, COLORS["divider"]),
                ("BACKGROUND", (0, 0), (-1, -1), COLORS["light"]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            self.elements.append(KeepTogether([title_para, frame]))
    
    def _build_downtime_page(self):
        """Build the downtime log page."""
        self.elements.append(Paragraph("Downtime Log", self.styles["section"]))
        self.elements.append(Spacer(1, 4 * mm))
        
        if not self.downtime_events:
            # No downtime - show success icon with centered layout
            self.elements.append(Spacer(1, 8 * mm))
            
            # Create centered table with icon and text
            icon = SuccessCheckCircle(size=14 * mm)
            
            title_style = ParagraphStyle(
                "NoDowntimeTitle",
                fontName=self.font_bold,
                fontSize=13,
                leading=18,
                textColor=COLORS["success"],
                alignment=1,  # Center
            )
            subtitle_style = ParagraphStyle(
                "NoDowntimeSubtitle",
                fontName=self.font_regular,
                fontSize=10,
                leading=14,
                textColor=COLORS["muted"],
                alignment=1,  # Center
            )
            
            title_para = Paragraph("No downtime recorded", title_style)
            subtitle_para = Paragraph(
                "All availability checks were successful during the reporting period.",
                subtitle_style
            )
            
            # Stack vertically: icon, title, subtitle
            content_table = Table(
                [[icon], [Spacer(1, 3 * mm)], [title_para], [Spacer(1, 1 * mm)], [subtitle_para]],
                colWidths=[self.width - 2 * self.margin],
            )
            content_table.setStyle(TableStyle([
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            
            self.elements.append(content_table)
            self.elements.append(Spacer(1, 8 * mm))
            return
        
        # Downtime table
        table_data = [["Start", "End", "Duration"]]
        
        for event in self.downtime_events[:30]:  # Limit to 30 events
            start_str = event.start.strftime("%Y-%m-%d %H:%M")
            end_str = event.end.strftime("%Y-%m-%d %H:%M") if event.end else "Ongoing"
            
            if event.duration_minutes is not None:
                if event.duration_minutes >= 60:
                    hours = event.duration_minutes // 60
                    mins = event.duration_minutes % 60
                    duration_str = f"{hours}h {mins}m"
                else:
                    duration_str = f"{event.duration_minutes} min"
            else:
                duration_str = "Ongoing"
            
            table_data.append([start_str, end_str, duration_str])
        
        # Calculate available content width
        content_width = self.width - 2 * self.margin
        # Distribute columns: Start 40%, End 40%, Duration 20%
        table = Table(
            table_data,
            colWidths=[content_width * 0.40, content_width * 0.40, content_width * 0.20]
        )
        table.setStyle(TableStyle([
            # Header row
            ('FONTNAME', (0, 0), (-1, 0), self.font_bold),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('TEXTCOLOR', (0, 0), (-1, 0), COLORS["dark"]),
            ('BACKGROUND', (0, 0), (-1, 0), COLORS["light"]),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            
            # Data rows
            ('FONTNAME', (0, 1), (-1, -1), self.font),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('TEXTCOLOR', (0, 1), (1, -1), COLORS["dark"]),
            ('TEXTCOLOR', (2, 1), (2, -1), COLORS["danger"]),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
            ('TOPPADDING', (0, 1), (-1, -1), 6),
            
            # Borders
            ('BOX', (0, 0), (-1, -1), 0.5, COLORS["divider"]),
            ('LINEBELOW', (0, 0), (-1, 0), 0.5, COLORS["divider"]),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [COLORS["white"], COLORS["light"]]),
        ]))
        
        self.elements.append(table)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_pdf_report(
    report_data: ReportData,
    page_size: str = "a4"
) -> bytes:
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
