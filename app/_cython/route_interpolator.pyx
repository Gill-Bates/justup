# cython: language_level=3
# cython: boundscheck=False, wraparound=False

"""Route interpolation for ICMP-blocked hops (v2, cleaned).

Principles:
- Deterministic state model
- No mixed semantics (status vs. flags)
- O(n) processing where n is the number of hops
- Clear separation: detection → interpolation → formatting

Note: Bisect lookups add O(b·log r) where b=blocks, r=responding hops,
but total complexity is dominated by linear scans → O(n) in practice.
"""

from __future__ import annotations

import bisect
import copy
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Constants / Types
# ──────────────────────────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_TIMEOUT = "timeout"
STATUS_INTERPOLATED = "interpolated"

Hop = dict[str, Any]

__all__ = [
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_TIMEOUT",
    "STATUS_INTERPOLATED",
    "interpolate_route",
    "ensure_destination_hop",
    "collapse_interpolated_hops",
]


# ──────────────────────────────────────────────────────────────────────────────
# Core interpolation
# ──────────────────────────────────────────────────────────────────────────────

def interpolate_route(
    hops: list[Hop],
    *,
    destination_geo: dict[str, Any] | None = None,
) -> list[Hop]:
    """Interpolate ICMP-blocked hops deterministically.
    
    Rules:
    - Only hops with status == timeout are interpolated
    - Interpolated hops get status == interpolated
    - Latency is linearly interpolated between surrounding responding hops
    - Selective copy to avoid mutation (only modified hops are copied)
    
    Complexity: O(n) where n is the number of hops.
    (Bisect lookups are O(log r) per block, but dominated by linear scans.)
    
    Warning: Geographic hint is applied to all interpolated hops regardless
    of plausibility (all get destination coords, not interpolated path).
    """
    
    if not hops:
        return hops

    # Shallow copy of list (individual hops copied only when modified)
    hops = list(hops)

    # ------------------------------------------------------------------
    # 1. Collect responding hops (anchors)
    # ------------------------------------------------------------------
    responding: list[tuple[int, float]] = []  # (hop_num, latency)

    for i, h in enumerate(hops):
        if h.get("status") in (STATUS_OK, STATUS_PARTIAL):
            hop_num = h.get("hop", i + 1)
            lat = h.get("latency_ms")
            avg = None

            if isinstance(lat, dict):
                avg = lat.get("avg")
            elif isinstance(lat, (int, float)):
                avg = float(lat)

            if avg is not None:
                responding.append((hop_num, float(avg)))
    
    # Sort responding hops for bisect (assumes hops are usually ordered,
    # but handles out-of-order probes)
    responding.sort(key=lambda r: r[0])

    # ------------------------------------------------------------------
    # 2. Detect timeout blocks
    # ------------------------------------------------------------------
    detected_blocks: list[tuple[int, int]] = []
    block_start: int | None = None
    last_timeout_hop: int | None = None

    for i, h in enumerate(hops):
        hop_num = h.get("hop", i + 1)
        if h.get("status") == STATUS_TIMEOUT:
            if block_start is None:
                block_start = hop_num
            last_timeout_hop = hop_num
        else:
            if block_start is not None:
                detected_blocks.append((block_start, last_timeout_hop))  # type: ignore[arg-type]
                block_start = None

    if block_start is not None and last_timeout_hop is not None:
        detected_blocks.append((block_start, last_timeout_hop))

    if not detected_blocks:
        return hops

    # ------------------------------------------------------------------
    # 3. Precompute block anchors (O(log r) lookup with bisect)
    # ------------------------------------------------------------------
    block_anchors: dict[int, tuple[float, float]] = {}
    hop_to_block: dict[int, int] = {}
    
    # Extract hop numbers for bisect (already sorted above)
    responding_nums = [r[0] for r in responding]

    for block_idx, (start, end) in enumerate(detected_blocks):
        for h in range(start, end + 1):
            hop_to_block[h] = block_idx

        # Latency before block (O(log r) with bisect)
        start_lat = 0.0
        idx = bisect.bisect_left(responding_nums, start) - 1
        if idx >= 0:
            start_lat = responding[idx][1]

        # Latency after block (O(log r) with bisect)
        # Defaults to start_lat if no responding hop after block
        end_lat = start_lat
        idx = bisect.bisect_right(responding_nums, end)
        if idx < len(responding):
            end_lat = responding[idx][1]

        block_anchors[block_idx] = (start_lat, end_lat)

    # ------------------------------------------------------------------
    # 4. Apply interpolation (selective copy to avoid mutation)
    # ------------------------------------------------------------------
    for i, h in enumerate(hops):
        hop_num = h.get("hop", i + 1)

        if h.get("status") != STATUS_TIMEOUT:
            continue

        block_idx = hop_to_block.get(hop_num)
        if block_idx is None:
            continue

        # Shallow copy the hop before modifying (avoid mutating input)
        h = copy.copy(h)
        hops[i] = h

        block_start, block_end = detected_blocks[block_idx]
        start_lat, end_lat = block_anchors[block_idx]

        block_len = block_end - block_start + 1
        position = hop_num - block_start + 1

        h["status"] = STATUS_INTERPOLATED
        h["_interpolated"] = True
        h["loss_pct"] = 100.0

        # Linear interpolation (end_lat is always float, never None)
        # Position is 1-based index within block (1 to len)
        # block_len+1 ensures we don't hit end_lat until the hop after the block
        if block_len > 0:
            progress = position / (block_len + 1)
            lat = start_lat + (end_lat - start_lat) * progress
            h["latency_ms"] = {
                "avg": round(lat, 1),
                "min": None,
                "max": None,
                "_interpolated": True,
            }
        else:
            h["latency_ms"] = {
                "avg": None,
                "min": None,
                "max": None,
                "_interpolated": True,
            }

        if destination_geo:
            h["_geo_hint"] = destination_geo

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
    final_latency_ms: float | None = None,
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
        "latency_ms": (
            {"avg": final_latency_ms, "min": None, "max": None}
            if final_latency_ms is not None
            else None
        ),
        "loss_pct": 0.0,
        "status": STATUS_OK if final_latency_ms is not None else STATUS_PARTIAL,
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

    return hops + [dest]


def _make_collapsed_summary(batch: list[Hop]) -> Hop:
    """Create a summary hop for a batch of collapsed interpolated hops."""
    first = batch[0]
    last = batch[len(batch) - 1]  # Avoid negative index (wraparound=False)
    count = len(batch)
    
    hop_display = (
        str(first.get('hop')) if count == 1
        else f"{first.get('hop')} - {last.get('hop')}"
    )
    
    return {
        "hop_display": hop_display,
        "ip_display": "—",
        "hostname_display": "ICMP blocked" if count == 1 else f"({count} hops interpolated)",
        "latency_display": "—",
        "loss_display": "—",
        "status": STATUS_INTERPOLATED,
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
