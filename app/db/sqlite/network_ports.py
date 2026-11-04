#!/usr/bin/env python3
#
# app/db/sqlite/network_ports.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Protocol-specific target port identity, service metadata, and latest status."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from app.services.nmap_scanner import NmapScanResult
from app.utils.tcp_ports import parse_port_list
from app.utils.time import utcnow


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
) -> None:
    """Reconcile configured pairs while preserving metadata for unchanged manual ports."""
    tcp = parse_port_list(list(tcp_ports))
    udp = parse_port_list(list(udp_ports))
    selected = {("tcp", port) for port in tcp} | {("udp", port) for port in udp}
    observed = {
        (port.protocol, port.port): port
        for host in scan_result.hosts
        for port in host.ports
    } if scan_result else {}
    existing = {
        (row["protocol"], row["port"])
        for row in conn.execute(
            "SELECT protocol, port FROM target_network_ports WHERE target_id = ?", (target_id,)
        )
    }
    for protocol, port in existing - selected:
        conn.execute(
            "DELETE FROM target_network_ports WHERE target_id = ? AND protocol = ? AND port = ?",
            (target_id, protocol, port),
        )
    for protocol, port in sorted(selected):
        finding = observed.get((protocol, port))
        conn.execute(
            "INSERT OR IGNORE INTO target_network_ports(target_id, protocol, port, source) VALUES(?, ?, ?, 'manual')",
            (target_id, protocol, port),
        )
        if finding is None:
            continue
        service = finding.service
        conn.execute(
            """UPDATE target_network_ports
               SET source = 'discovered', service_name = ?, service_product = ?, service_version = ?,
                   service_method = ?, confidence = ?, discovered_at = ?
               WHERE target_id = ? AND protocol = ? AND port = ?""",
            (
                service.name[:100] if service and service.name else None,
                service.product[:150] if service and service.product else None,
                service.version[:150] if service and service.version else None,
                service.method if service else None,
                service.confidence if service else None,
                utcnow(),
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
) -> None:
    if protocol not in {"tcp", "udp"} or status not in {"up", "down", "unknown"}:
        raise ValueError("Invalid network port status")
    now = utcnow()
    conn.execute(
        """UPDATE target_network_ports
           SET status = ?, checked_at = ?, last_up_at = CASE WHEN ? = 'up' THEN ? ELSE last_up_at END,
               detail = ?
           WHERE target_id = ? AND protocol = ? AND port = ?""",
        (status, now, status, now, detail[:200] if detail else None, target_id, protocol, port),
    )
