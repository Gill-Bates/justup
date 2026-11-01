#!/usr/bin/env python3
#
# app/services/route_timeline.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Route timeline model shared by the traceroute web fragment and the PDF report.

Pure data functions without web or Jinja dependencies: node states (measured,
timeout, interpolated, unreached) kept apart from the location state (mapped,
unlocated), plus grouping of consecutive hops.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

TIMELINE_GROUP_MIN = 3
TIMELINE_NO_VALUE = frozenset({"", "*", "-", "—"})
# Short state wording shared by the web table, the timeline and the PDF (one owner, no duplicates).
NO_REPLY_LABEL = "No reply"
LOCATION_UNKNOWN_LABEL = "Location unknown"
ESTIMATED_LABEL = "Estimated"
APPROXIMATE_LABEL = "approx."
STATE_LABELS = {
    "no_reply": NO_REPLY_LABEL,
    "location_unknown": LOCATION_UNKNOWN_LABEL,
    "estimated": ESTIMATED_LABEL,
    "approx": APPROXIMATE_LABEL,
}
# measurement state -> (visible/ARIA label, Material icon). Colour is never the only cue.
TIMELINE_MEASUREMENTS: dict[str, tuple[str, str]] = {
    "measured": ("Measured", "check_circle"),
    "timeout": (NO_REPLY_LABEL, "report_problem"),
    "interpolated": ("Estimated (interpolated)", "timeline"),
    "unreached": ("Not reached, no reply", "error_outline"),
}


def timeline_text(value: object) -> str:
    text = str(value).strip() if value is not None else ""
    return "" if text in TIMELINE_NO_VALUE else text


def timeline_measurement(hop: dict) -> str:
    """Classify how a hop's data came about: measured, timeout, interpolated or synthetic destination."""
    if hop.get("_synthetic_destination"):
        return "unreached"
    if hop.get("_collapsed") or hop.get("_interpolated") or hop.get("status") == "interpolated":
        return "interpolated"
    if hop.get("status") == "timeout":
        return "timeout"
    return "measured"


def timeline_node(hop: dict, *, mapped_hops: set[int], is_first: bool, is_destination: bool) -> dict:
    """Build one timeline node. A hop without map location is not failed; the two axes stay separate."""
    try:
        number: int | None = int(hop.get("hop"))
    except (TypeError, ValueError):
        number = None
    measurement = timeline_measurement(hop)
    located = number is not None and number in mapped_hops
    measurement_label, icon = TIMELINE_MEASUREMENTS[measurement]
    if measurement == "measured" and hop.get("status") == "partial":
        measurement_label = "Measured, some probes lost"
    silent = measurement in ("timeout", "unreached")
    location_label = "—" if silent else "Located on map" if located else LOCATION_UNKNOWN_LABEL
    ip = timeline_text(hop.get("ip_full") or hop.get("ip"))
    host = timeline_text(hop.get("hostname"))
    country = timeline_text(hop.get("country")).upper()
    if is_first and is_destination:
        kind, title = "single", "First hop · Destination"
    elif is_destination:
        kind, title = "destination", "Destination"
    elif is_first:
        kind, title = "first", "First hop"
    else:
        kind, title = "hop", f"Hop {number if number is not None else '?'}"
    if country and kind == "hop":
        title = f"{title} · {country}"
    aria_title = title.replace(" · ", ", ")
    subtitle = (host or ip) if is_destination else (ip or host)
    if not subtitle:
        subtitle = NO_REPLY_LABEL if silent else location_label
    return {
        "kind": kind,
        "hop": number,
        "first_hop": number,
        "last_hop": number,
        "label": str(number) if number is not None else "?",
        "title": title,
        "subtitle": subtitle,
        "ip": ip,
        "host": host,
        "country": country,
        "city": timeline_text(hop.get("city")),
        "measurement": measurement,
        "measurement_label": measurement_label,
        "location": "mapped" if located else "unlocated",
        "location_label": location_label,
        "icon": icon,
        "aria_label": ", ".join(
            part
            for part in (aria_title, "" if silent else location_label.lower(), measurement_label.lower(), ip or host)
            if part
        ),
        "detail": " · ".join(part for part in (title, ip, host, location_label, measurement_label) if part),
    }


def timeline_group_key(node: dict) -> str | None:
    if node["kind"] != "hop":
        return None
    if node["location"] == "unlocated":
        return "unlocated"
    return "interpolated" if node["measurement"] == "interpolated" else None


def timeline_group(members: list[dict], key: str) -> dict:
    """Collapse a run of consecutive same-kind hops into one expandable node; members stay complete."""
    first, last = members[0]["first_hop"], members[-1]["last_hop"]
    count = len(members)
    measurements = {m["measurement"] for m in members}
    counts = [
        f"{sum(m['measurement'] == name for m in members)} {label.lower()}"
        for name, (label, _icon) in TIMELINE_MEASUREMENTS.items()
        if name in measurements
    ]
    mixed = len(measurements) > 1
    title = {"unlocated": f"{count} hops without map location", "interpolated": f"{count} estimated hops"}.get(
        key, f"{count} hops"
    )
    subtitle = "Mixed probe states" if mixed else members[0]["measurement_label"]
    span = f"{first}–{last}" if first != last else str(first)
    return {
        "kind": "group",
        "id": f"tr-group-{first}-{last}",
        "first_hop": first,
        "last_hop": last,
        "label": span,
        "title": title,
        "subtitle": subtitle,
        "summary": ", ".join(counts),
        "measurement": "mixed" if mixed else members[0]["measurement"],
        "location": "unlocated" if key == "unlocated" else "mapped",
        "members": members,
        "count": count,
        "aria_label": f"Hops {first} to {last}, {title}, {', '.join(counts)}",
        "detail": f"Hops {span} · {title} · {', '.join(counts)}",
    }


def group_timeline_nodes(
    nodes: list[dict],
    *,
    key_fn: Callable[[dict], str | None] = timeline_group_key,
    min_run: int = TIMELINE_GROUP_MIN,
) -> list[dict]:
    """Merge runs of `min_run`+ nodes sharing a `key_fn` key, only across consecutive hop numbers."""
    out: list[dict] = []
    run: list[dict] = []
    run_key: str | None = None

    def flush() -> None:
        nonlocal run, run_key
        if run_key is not None and len(run) >= min_run:
            out.append(timeline_group(run, run_key))
        else:
            out.extend(run)
        run, run_key = [], None

    for node in nodes:
        key = key_fn(node)
        # A gap in the hop numbering ends the run: a group label must not claim unseen hops.
        contiguous = (
            bool(run)
            and node["first_hop"] is not None
            and run[-1]["last_hop"] is not None
            and node["first_hop"] == run[-1]["last_hop"] + 1
        )
        if key is None or key != run_key or not contiguous:
            flush()
        if key is None:
            out.append(node)
        else:
            run_key = key
            run.append(node)
    flush()
    return out


def build_route_timeline(
    sequence_hops: list[dict],
    mapped_hops: set[int],
    target_ip: str | None,
    destination_geo_point: dict | None,
) -> dict:
    """Merge first hop, intermediate hops and destination into ONE ordered node list.

    The first displayed hop is labelled 'First hop' (not necessarily the real source machine).
    Exactly one destination node exists; a synthetic destination is 'unreached', never successful.
    """
    destination_index = None
    for index, hop in enumerate(sequence_hops):
        if hop.get("_destination") or hop.get("_synthetic_destination") or (target_ip and hop.get("ip") == target_ip):
            destination_index = index
    flat = [
        timeline_node(hop, mapped_hops=mapped_hops, is_first=index == 0, is_destination=index == destination_index)
        for index, hop in enumerate(sequence_hops)
    ]
    if sequence_hops and destination_index is None:
        geo = destination_geo_point or {}
        flat.append(
            timeline_node(
                {
                    "hop": geo.get("hop") if isinstance(geo.get("hop"), int) else None,
                    "ip": target_ip,
                    "country": geo.get("country"),
                    "city": geo.get("city"),
                    "_synthetic_destination": True,
                },
                mapped_hops=mapped_hops,
                is_first=False,
                is_destination=True,
            )
        )
    numbers = [n["hop"] for n in flat if n["hop"] is not None]
    present = {n["measurement"] for n in flat}
    return {
        "nodes": group_timeline_nodes(flat),
        "flat_nodes": flat,
        "hop_count": len(flat),
        "first_hop": min(numbers) if numbers else None,
        "last_hop": max(numbers) if numbers else None,
        "first_ip": flat[0]["ip"] if flat else "",
        "last_ip": (target_ip or flat[-1]["ip"]) if flat else "",
        "legend": [
            {"key": name, "label": label, "icon": icon}
            for name, (label, icon) in TIMELINE_MEASUREMENTS.items()
            if name in present
        ],
        "has_unlocated": any(n["location"] == "unlocated" for n in flat),
    }


def expand_sequence_hops(display_hops: Iterable[dict]) -> list[dict]:
    """Flatten collapsed summary rows into the individual hops they stand for."""
    out: list[dict] = []
    for hop in display_hops:
        raw_hops = hop.get("_raw_hops") if hop.get("_collapsed") else None
        if raw_hops:
            out.extend(raw_hops)
        else:
            out.append(hop)
    return out


def mapped_hop_numbers(geo_points: Iterable[dict]) -> set[int]:
    """Hop numbers that have a map position; a point may stand for a `_hop_range` like "3-5"."""
    mapped: set[int] = set()
    for point in geo_points:
        try:
            if point.get("_hop_range"):
                start, end = (int(value) for value in point["_hop_range"].split("-", maxsplit=1))
                mapped.update(range(start, end + 1))
            elif point.get("hop") is not None:
                mapped.add(int(point["hop"]))
        except (TypeError, ValueError):
            continue
    return mapped


def logical_hop_count(display_hops: Iterable[dict]) -> int:
    """Count route hops, expanding collapsed ICMP-blocked rows into the hops they stand for."""
    total = 0
    for hop in display_hops:
        raw = hop.get("_raw_hops")
        total += len(raw) if hop.get("_collapsed") and raw else 1
    return total


def route_start_is_mapped(start_hop: object, geo_points: list[dict]) -> bool:
    """True when the route's first hop is the first map point (or inside its merged hop range)."""
    if not geo_points:
        return False
    first = geo_points[0]
    try:
        number = int(start_hop)  # type: ignore[call-overload]
        hop_range = first.get("_hop_range")
        if hop_range:
            low, high = (int(value) for value in hop_range.split("-", maxsplit=1))
            return low <= number <= high
        return int(first.get("hop")) == number
    except (TypeError, ValueError):
        return False
