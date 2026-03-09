#!/usr/bin/env python3
#
# app/utils/metric_limits.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Helpers to choose chart-friendly point limits.

These limits are for *visual* consumption (UI charts + PDF charts).
The TSDB query layer (query_auto_resolution) will auto-select resolution and then
apply bucket-based resampling via _resample_points() using median/min aggregation.

Resampling Guarantee:
- Points are distributed uniformly across the time range (no tail-heavy bias)
- Binary metrics (http_up, etc.) use min() to preserve downtime
- Latency metrics use median() for robustness against spikes
- http_status uses index-based sampling to preserve distribution
- Start/end timestamps are preserved for stable chart axes

Design goals:
- PDFs: readability > detail
- UI: interactive, can show more detail
- Deterministic across targets and ranges

Profiles:
- "pdf": calm/print-friendly
- "ui": detail-rich
"""

from __future__ import annotations

from datetime import datetime


_PDF_CAP = 900
_UI_CAP = 1500

# Status code distributions benefit from larger samples.
_PDF_STATUS_CAP = 5000
_UI_STATUS_CAP = 10000


def _base_limit_for_range(*, range_seconds: float, profile: str) -> int:
    if profile not in ("pdf", "ui"):
        profile = "ui"

    # Prioritized ranges (praxisbewährt)
    eps = 60.0  # 1 minute tolerance for boundary conditions
    if range_seconds <= (24 * 3600) + eps:
        return 400 if profile == "pdf" else 600
    if range_seconds <= (7 * 24 * 3600) + eps:
        return 600 if profile == "pdf" else 900
    if range_seconds <= (30 * 24 * 3600) + eps:
        return 800 if profile == "pdf" else 1200

    # Beyond 30d: keep bounded, rely on rollups + deterministic resample
    return _PDF_CAP if profile == "pdf" else _UI_CAP


def choose_points_limit(
    *,
    metric: str,
    since: datetime,
    until: datetime,
    profile: str,
) -> int:
    """Choose a max number of points for a metric time series.

    This is intentionally simple and deterministic.

    Metric-specific tweaks:
    - bursty metrics (ping/tcp): +15%
    - uptime binary series: -20%
    - http_status (distribution): higher cap
    """
    range_seconds = (until - since).total_seconds()
    base = _base_limit_for_range(range_seconds=range_seconds, profile=profile)

    m = (metric or "").strip()

    # Special: status codes are used as a distribution chart.
    if m == "http_status":
        cap = _PDF_STATUS_CAP if profile == "pdf" else _UI_STATUS_CAP
        # Use a larger baseline for longer windows (but keep bounded)
        scaled = int(max(1000, base * 5))
        return min(cap, scaled)

    # Bursty network metrics
    if m in ("ping_ms", "tcp_ms"):
        base = int(base * 1.15)

    # Flatter binary metrics (step chart)
    if m in ("http_up", "ping_up", "tcp_up"):
        base = int(base * 0.8)

    cap = _PDF_CAP if profile == "pdf" else _UI_CAP
    # Keep at least a small, meaningful number of points
    return max(50, min(cap, base))
