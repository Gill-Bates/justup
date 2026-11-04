#!/usr/bin/env python3
#
# app/utils/endpoint_view.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""View model of a target's network endpoints (ICMP, TCP and UDP ports) for the dashboard and detail page.

One list drives both pages so they cannot contradict each other or the API: it merges the configured
ports with the persisted per-port state (``target_network_ports``), keeps ICMP separate and gives
UNKNOWN its own presentation (text, icon, shape), never the one of DOWN.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .tcp_ports import parse_port_list

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"

# status -> (label, Material icon, badge variant). Label and icon differ per status: colour is never the only cue.
_STATUS_PRESENTATION = {
    STATUS_UP: ("Up", "check_circle", "success"),
    STATUS_DOWN: ("Down", "cancel", "danger"),
    STATUS_UNKNOWN: ("Unknown", "help", "neutral"),
}

# Nmap UDP states other than "open" and "closed" are not a confirmed result.
_RAW_STATE_NOTES = {
    "open": "open (confirmed)",
    "closed": "closed (confirmed)",
    "open|filtered": "open|filtered (uncertain, not confirmed open)",
    "filtered": "filtered (uncertain)",
    "closed|filtered": "closed|filtered (uncertain)",
    "unfiltered": "unfiltered (uncertain)",
    "unknown": "no usable result",
}


def normalise_status(value: object) -> str:
    """Wire status or ``None`` to ``up``/``down``/``unknown``; an unrecognised value is UNKNOWN, never DOWN."""
    text = str(value).strip().lower() if value is not None else ""
    return text if text in (STATUS_UP, STATUS_DOWN) else STATUS_UNKNOWN


def _get(source: Mapping[str, Any] | Any, key: str) -> Any:
    try:
        return source[key]
    except (KeyError, IndexError, TypeError):
        return None


def _ports(value: object, *, default: list[int] | None = None) -> list[int]:
    try:
        return parse_port_list(str(value or "")) or (default or [])
    except ValueError:
        return []


def service_text(port: Mapping[str, Any] | None) -> tuple[str, str]:
    """``(label, note)`` of the detected service; reaching a port never proves the service identity."""
    if not port or not port.get("service_name"):
        return "", ""
    parts = [str(port["service_name"])]
    parts.extend(str(port[key]) for key in ("service_product", "service_version") if port.get(key))
    label = " ".join(parts)
    if port.get("service_identity") == "changed":
        note = "service changed"
    elif port.get("method") == "table":
        note = "table hint, not verified"
    else:
        note = ""
    return label, note


def _entry(
    *,
    kind: str,
    protocol: str | None,
    port: int | None,
    status: str,
    checked_at: Any = None,
    last_up_at: Any = None,
    stored: Mapping[str, Any] | None = None,
    measured: bool = True,
) -> dict[str, Any]:
    label, icon, variant = _STATUS_PRESENTATION[status]
    service, service_note = service_text(stored)
    raw_state = (stored or {}).get("raw_state")
    return {
        "key": "icmp" if kind == "icmp" else f"{protocol}:{port}",
        "kind": kind,
        "protocol": protocol,
        "port": port,
        "name": "ICMP" if kind == "icmp" else f"{str(protocol).upper()} {port}",
        "status": status,
        # False only while the check is configured but no measurement exists yet ("waiting").
        "measured": measured,
        "status_label": label,
        "status_icon": icon,
        "status_variant": variant,
        "service": service,
        "service_note": service_note,
        "raw_state": raw_state,
        "state_note": _RAW_STATE_NOTES.get(str(raw_state), "") if protocol == "udp" and raw_state else "",
        "detail": (stored or {}).get("detail") or "",
        "checked_at": checked_at,
        "last_up_at": last_up_at,
    }


def build_endpoint_views(
    target: Mapping[str, Any], stored_ports: Iterable[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """Enabled endpoints of one target: ICMP, then TCP, then UDP ports in ascending order.

    Every configured port is listed, also before its first measurement (UNKNOWN, not missing). A port
    that is no longer configured, or whose check is off, is not shown.
    """
    stored = {(str(row["protocol"]), int(row["port"])): row for row in (stored_ports or [])}
    views: list[dict[str, Any]] = []
    if _get(target, "enable_ping"):
        status = normalise_status(_get(target, "ping_status"))
        checked = _get(target, "ping_checked_at")
        views.append(
            _entry(
                kind="icmp",
                protocol=None,
                port=None,
                status=status,
                measured=bool(checked) or status != STATUS_UNKNOWN,
                checked_at=checked,
                # ICMP keeps no history column: the last success is only known while the host answers.
                last_up_at=checked if status == STATUS_UP else None,
            )
        )
    legacy = _get(target, "tcp_port_status") or {}
    if _get(target, "enable_tcp_connect"):
        for port in _ports(_get(target, "tcp_ports"), default=[80]):
            row = stored.get(("tcp", port))
            # A row never written (migration 7 seeds it empty) defers to the status cache.
            if row and row.get("checked_at"):
                raw = row["status"]
            else:
                raw = legacy.get(str(port)) or (row["status"] if row else None)
            views.append(
                _entry(
                    kind="port",
                    protocol="tcp",
                    port=port,
                    status=normalise_status(raw),
                    measured=bool((row and row.get("checked_at")) or legacy.get(str(port))),
                    checked_at=row["checked_at"] if row else None,
                    last_up_at=row["last_up_at"] if row else None,
                    stored=row,
                )
            )
    if _get(target, "enable_udp_scan"):
        for port in _ports(_get(target, "udp_ports")):
            row = stored.get(("udp", port))
            views.append(
                _entry(
                    kind="port",
                    protocol="udp",
                    port=port,
                    status=normalise_status(row["status"] if row else None),
                    measured=bool(row and row.get("checked_at")),
                    checked_at=row["checked_at"] if row else None,
                    last_up_at=row["last_up_at"] if row else None,
                    stored=row,
                )
            )
    return views
