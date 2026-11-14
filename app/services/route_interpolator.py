#!/usr/bin/env python3
#
# app/services/route_interpolator.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Route interpolation for ICMP-blocked hops (v2, cleaned).

Principles:
- Deterministic state model
- No mixed semantics (status vs. flags)
- O(n) processing for n hops
- Clear separation: detection → interpolation → formatting
"""

from __future__ import annotations

import copy
from typing import Any

from app.services.route_timeline import NO_REPLY_LABEL, no_reply_run_label
from app.services.traceroute_service import STATUS_OK, STATUS_PARTIAL, STATUS_TIMEOUT
from app.utils.constants import HopStatus

# ──────────────────────────────────────────────────────────────────────────────
# Constants / Types
# ──────────────────────────────────────────────────────────────────────────────

STATUS_INTERPOLATED = HopStatus.INTERPOLATED

Hop = dict[str, Any]

__all__ = [
    "STATUS_INTERPOLATED",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_TIMEOUT",
    "collapse_interpolated_hops",
    "ensure_destination_hop",
    "interpolate_route",
]


# ──────────────────────────────────────────────────────────────────────────────
# Core interpolation
# ──────────────────────────────────────────────────────────────────────────────


def interpolate_route(
    hops: list[Hop],
    *,
    destination_geo: dict[str, Any] | None = None,
) -> list[Hop]:
    """Mark silent hops that lie between answering hops as interpolated (estimated).

    Rules:
    - Only hops with status == timeout are marked
    - Trailing timeouts (no answering hop after them) stay timeout
    - Interpolated hops carry no RTT: nothing was measured there
    - Selective copy to avoid mutating the input
    """
    if not hops:
        return hops
    hops = list(hops)
    last_answered = max((h.get("hop", i + 1) for i, h in enumerate(hops) if h.get("status") == STATUS_OK), default=None)
    if last_answered is None:
        return hops
    for i, h in enumerate(hops):
        if h.get("status") != STATUS_TIMEOUT or h.get("hop", i + 1) > last_answered:
            continue
        hop = copy.copy(h)
        hop["status"] = STATUS_INTERPOLATED
        hop["_interpolated"] = True
        hop["rtt_ms"] = None
        if destination_geo:
            hop["_geo_hint"] = destination_geo
        hops[i] = hop
    return hops


# ──────────────────────────────────────────────────────────────────────────────
# Destination enforcement
# ──────────────────────────────────────────────────────────────────────────────


def ensure_destination_hop(
    hops: list[Hop],
    *,
    destination_ip: str | None,
    destination_hostname: str | None = None,
    destination_geo: dict[str, Any] | None = None,
    final_rtt_hint_ms: float | None = None,
) -> list[Hop]:
    """Ensure destination hop exists as final entry."""
    if not hops or not destination_ip:
        return hops

    for h in hops:
        if h.get("ip") == destination_ip and h.get("status") in (STATUS_OK, STATUS_PARTIAL):
            return hops

    last_hop = max(h.get("hop", 0) for h in hops)

    dest: Hop = {
        "hop": last_hop + 1,
        "ip": destination_ip,
        "hostname": destination_hostname or "-",
        # The destination never answered: no RTT is reported. The last router's RTT is a hint
        # only; "partial" avoids claiming a reached target.
        "rtt_ms": None,
        "status": STATUS_PARTIAL,
        "_estimated_latency_ms": final_rtt_hint_ms,
        "_synthetic_destination": True,
        "_via": "non-icmp",
    }

    if destination_geo:
        dest.update(
            lat=destination_geo.get("lat"),
            lon=destination_geo.get("lon"),
            city=destination_geo.get("city"),
            country=destination_geo.get("country"),
        )

    return [*hops, dest]


def _make_collapsed_summary(batch: list[Hop]) -> Hop:
    """Create a summary row for consecutive hops without reply (counted from the measured hop numbers)."""
    first = batch[0]
    last = batch[-1]
    count = len(batch)

    hop_display = str(first.get("hop")) if count == 1 else f"{first.get('hop')} - {last.get('hop')}"

    return {
        "hop_display": hop_display,
        "ip_display": "—",
        "hostname_display": NO_REPLY_LABEL if count == 1 else no_reply_run_label(count),
        "latency_display": "—",
        "status": STATUS_INTERPOLATED,
        "status_display": STATUS_TIMEOUT,  # what the user sees: no reply, not an estimate
        "_collapsed": True,
        "_interpolated": True,
        "_raw_hops": list(batch),  # Defensive copy to avoid reference issues
    }


def collapse_interpolated_hops(hops: list[Hop]) -> list[Hop]:
    """Collapse consecutive interpolated hops into summary rows."""
    if not hops:
        return []

    out: list[Hop] = []
    collapsed_batch: list[Hop] = []

    for h in hops:
        if h.get("status") == STATUS_INTERPOLATED:
            collapsed_batch.append(h)
        else:
            if collapsed_batch:
                out.append(_make_collapsed_summary(collapsed_batch))
                collapsed_batch = []
            out.append(h)

    # Handle trailing batch
    if collapsed_batch:
        out.append(_make_collapsed_summary(collapsed_batch))

    return out
