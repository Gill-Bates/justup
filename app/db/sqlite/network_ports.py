#!/usr/bin/env python3
#
# app/db/sqlite/network_ports.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Protocol-specific target port identity, service metadata, and latest status."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable

from app.models.network_scan import NmapScanResult
from app.utils.tcp_ports import parse_port_list
from app.utils.time import utcnow

# Column limits for stored service values; identity checks truncate observed values the same way.
SERVICE_NAME_MAX = 100
SERVICE_TEXT_MAX = 150

RAW_STATES = frozenset({"open", "closed", "filtered", "unfiltered", "open|filtered", "closed|filtered", "unknown"})
SERVICE_IDENTITIES = frozenset({"identified", "unknown", "changed"})


def _identity_of(service) -> str:
    """Only an actively probed, named service is an identification; a nmap-services table hit is not."""
    return "identified" if service and service.method == "probed" and service.name else "unknown"


def _service_columns(service) -> tuple:
    """Return (name, product, version, method, confidence) truncated to the column limits; None without a service."""
    if service is None:
        return (None, None, None, None, None)
    return (
        service.name[:SERVICE_NAME_MAX] if service.name else None,
        service.product[:SERVICE_TEXT_MAX] if service.product else None,
        service.version[:SERVICE_TEXT_MAX] if service.version else None,
        service.method,
        service.confidence,
    )


def _port_dict(row: sqlite3.Row) -> dict:
    return {
        "protocol": row["protocol"],
        "port": row["port"],
        "source": row["source"],
        "service_name": row["service_name"],
        "service_product": row["service_product"],
        "service_version": row["service_version"],
        "method": row["service_method"],
        "confidence": row["confidence"],
        "discovered_at": row["discovered_at"],
        "status": row["status"],
        "checked_at": row["checked_at"],
        "last_up_at": row["last_up_at"],
        "detail": row["detail"],
        "raw_state": row["raw_state"],
        "service_identity": row["service_identity"],
        "identity_changed_at": row["identity_changed_at"],
        "latency_ms": row["latency_ms"],
    }


def list_network_ports(conn: sqlite3.Connection, target_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM target_network_ports WHERE target_id = ? ORDER BY protocol, port", (target_id,)
    ).fetchall()
    return [_port_dict(row) for row in rows]


def all_network_ports(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for row in conn.execute("SELECT * FROM target_network_ports ORDER BY target_id, protocol, port"):
        grouped.setdefault(int(row["target_id"]), []).append(_port_dict(row))
    return grouped


def sync_network_ports(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    tcp_ports: Iterable[int],
    udp_ports: Iterable[int],
    scan_result: NmapScanResult | None = None,
    reset_protocols: Collection[str] = (),
    scan_protocols: Collection[str] = ("tcp", "udp"),
) -> None:
    """Reconcile configured pairs; drop observations of ``reset_protocols`` whose scan host changed.

    Only findings of ``scan_protocols`` are applied: a scan of one host says nothing about a protocol probed elsewhere.
    """
    reset = sorted({p for p in reset_protocols if p in {"tcp", "udp"}})
    if reset:
        conn.execute(
            f"""UPDATE target_network_ports
               SET source = 'manual', service_name = NULL, service_product = NULL,
                   service_version = NULL, service_method = NULL, confidence = NULL,
                   discovered_at = NULL, status = 'unknown', checked_at = NULL,
                   last_up_at = NULL, detail = NULL, raw_state = NULL,
                   service_identity = 'unknown', identity_changed_at = NULL, latency_ms = NULL
               WHERE target_id = ? AND protocol IN ({",".join("?" * len(reset))})""",  # noqa: S608 - placeholders only
            (target_id, *reset),
        )
    tcp = parse_port_list(list(tcp_ports))
    udp = parse_port_list(list(udp_ports))
    selected = {("tcp", port) for port in tcp} | {("udp", port) for port in udp}
    observed = (
        {
            (port.protocol, port.port): port
            for host in scan_result.hosts
            for port in host.ports
            if port.protocol in scan_protocols
        }
        if scan_result
        else {}
    )
    existing = {
        (row["protocol"], row["port"])
        for row in conn.execute("SELECT protocol, port FROM target_network_ports WHERE target_id = ?", (target_id,))
    }
    for protocol, port in existing - selected:
        conn.execute(
            "DELETE FROM target_network_ports WHERE target_id = ? AND protocol = ? AND port = ?",
            (target_id, protocol, port),
        )
    for protocol, port in sorted(selected):
        finding = observed.get((protocol, port))
        conn.execute(
            "INSERT OR IGNORE INTO target_network_ports(target_id, protocol, port, source, service_identity) "
            "VALUES(?, ?, ?, 'manual', 'unknown')",
            (target_id, protocol, port),
        )
        if finding is None:
            continue
        service = finding.service
        conn.execute(
            """UPDATE target_network_ports
               SET source = 'discovered', service_name = ?, service_product = ?, service_version = ?,
                   service_method = ?, confidence = ?, discovered_at = ?, service_identity = ?,
                   identity_changed_at = NULL
               WHERE target_id = ? AND protocol = ? AND port = ?""",
            (
                *_service_columns(service),
                scan_result.completed_at,
                _identity_of(service),
                target_id,
                protocol,
                port,
            ),
        )


def update_network_port_status(
    conn: sqlite3.Connection,
    target_id: int,
    protocol: str,
    port: int,
    status: str,
    detail: str | None = None,
    *,
    raw_state: str | None = None,
    latency_ms: float | None = None,
) -> None:
    """Store the latest measurement; the port is addressed by protocol + number, never by number alone."""
    if protocol not in {"tcp", "udp"} or status not in {"up", "down", "unknown"}:
        raise ValueError("Invalid network port status")
    if raw_state is not None and raw_state not in RAW_STATES:
        raise ValueError("Invalid raw port state")
    now = utcnow()
    conn.execute(
        """UPDATE target_network_ports
           SET status = :status, checked_at = :now,
               last_up_at = CASE WHEN :status = 'up' THEN :now ELSE last_up_at END,
               detail = :detail, raw_state = :raw_state, latency_ms = :latency_ms
           WHERE target_id = :target_id AND protocol = :protocol AND port = :port""",
        {
            "status": status,
            "now": now,
            "detail": detail[:200] if detail else None,
            "raw_state": raw_state,
            "latency_ms": latency_ms,
            "target_id": target_id,
            "protocol": protocol,
            "port": port,
        },
    )


def record_service_observation(
    conn: sqlite3.Connection,
    target_id: int,
    protocol: str,
    port: int,
    *,
    identity: str,
    detail: str | None = None,
    service=None,
    adopt: bool = False,
    version: str | None = None,
) -> None:
    """Store the outcome of an explicit service re-detection without touching the port status.

    ``adopt`` replaces the base identity; otherwise only identity, diagnostic note and (for a plain
    update) the version are written, so a changed identity stays visible until it is re-adopted.
    """
    if protocol not in {"tcp", "udp"} or identity not in SERVICE_IDENTITIES:
        raise ValueError("Invalid service observation")
    now = utcnow()
    if adopt and service is not None:
        conn.execute(
            """UPDATE target_network_ports
               SET service_name = ?, service_product = ?, service_version = ?, service_method = ?,
                   confidence = ?, discovered_at = ?, service_identity = ?, identity_changed_at = NULL
               WHERE target_id = ? AND protocol = ? AND port = ?""",
            (
                *_service_columns(service),
                now,
                identity,
                target_id,
                protocol,
                port,
            ),
        )
        return
    conn.execute(
        """UPDATE target_network_ports
           SET service_identity = ?,
               identity_changed_at = CASE
                   WHEN ? = 'changed' AND COALESCE(service_identity, '') != 'changed' THEN ?
                   WHEN ? != 'changed' THEN NULL ELSE identity_changed_at END,
               service_version = COALESCE(?, service_version),
               detail = COALESCE(?, detail)
           WHERE target_id = ? AND protocol = ? AND port = ?""",
        (
            identity,
            identity,
            now,
            identity,
            version[:SERVICE_TEXT_MAX] if version else None,
            detail[:200] if detail else None,
            target_id,
            protocol,
            port,
        ),
    )
