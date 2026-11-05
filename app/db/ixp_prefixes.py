#!/usr/bin/env python3
#
# app/db/ixp_prefixes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Persistence of the IXP peering-LAN prefix snapshot (ixp_exchanges, ixp_prefixes, ixp_snapshot).

A snapshot replaces the previous one in ONE transaction: a failure rolls everything back, so the last
complete snapshot stays active. Prefixes are not POP aliases; they prove IXP membership only.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from ..utils.time import utcnow

_log = logging.getLogger(__name__)

SOURCE_PEERINGDB_IXP = "peeringdb_ixp"

# location_class values: what the structured sources evidence about where the exchange is.
LOC_METRO = "metro"  # facilities agree on one metro region (position = region reference)
LOC_DISTRIBUTED = "distributed"  # facilities in several regions: no single position
LOC_CONFLICTING = "conflicting"  # exchange record and facilities contradict each other
LOC_UNKNOWN = "unknown"  # no usable facility data


@dataclass(frozen=True, slots=True)
class IxpExchange:
    ix_id: int
    name: str
    city: str | None
    country: str | None
    location_class: str
    ref_city: str | None = None
    ref_country: str | None = None  # set when the sources agree on one country, even without a position
    ref_lat: float | None = None
    ref_lon: float | None = None
    ref_radius_km: float | None = None
    facility_count: int = 0


@dataclass(frozen=True, slots=True)
class IxpPrefix:
    prefix: str  # canonical network string (strict ip_network form)
    ip_version: int
    prefix_length: int
    ix_id: int
    ixlan_id: int


@dataclass(frozen=True, slots=True)
class IxpSnapshot:
    exchanges: tuple[IxpExchange, ...]
    prefixes: tuple[IxpPrefix, ...]


@dataclass(slots=True)
class IxpApplyStats:
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    exchanges: int = 0
    prefixes: int = 0
    details: dict[str, int] = field(default_factory=dict)


class IxpSnapshotRejected(ValueError):
    """The snapshot is implausible (empty, oversized or shrunk too far); the active one is kept."""


# Plausibility bounds: PeeringDB lists a few thousand prefixes. A snapshot far outside is a broken download.
MAX_IXP_EXCHANGES = 10_000
MAX_IXP_PREFIXES = 50_000
MIN_PREVIOUS_FOR_SHRINK_GUARD = 100
MIN_RETAINED_FRACTION = 0.5


def snapshot_status(conn: sqlite3.Connection) -> dict | None:
    """Metadata of the last complete snapshot, or None before the first successful import."""
    row = conn.execute(
        "SELECT import_run_id, completed_at, exchange_count, prefix_count, source FROM ixp_snapshot WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "import_run_id": row[0],
        "completed_at": row[1],
        "exchange_count": row[2],
        "prefix_count": row[3],
        "source": row[4],
    }


def check_plausible(snapshot: IxpSnapshot, previous_prefix_count: int) -> None:
    """Reject an empty, oversized or abruptly shrunk snapshot before it touches the database."""
    count = len(snapshot.prefixes)
    if count == 0:
        raise IxpSnapshotRejected("snapshot contains no valid IXP prefix")
    if count > MAX_IXP_PREFIXES or len(snapshot.exchanges) > MAX_IXP_EXCHANGES:
        raise IxpSnapshotRejected(f"snapshot exceeds the size limits ({count} prefixes)")
    if previous_prefix_count >= MIN_PREVIOUS_FOR_SHRINK_GUARD and count < previous_prefix_count * MIN_RETAINED_FRACTION:
        raise IxpSnapshotRejected(
            f"snapshot shrank from {previous_prefix_count} to {count} prefixes; treated as incomplete"
        )


def replace_snapshot(conn: sqlite3.Connection, snapshot: IxpSnapshot, run_id: str) -> IxpApplyStats:
    """Atomically make *snapshot* the active one; any error rolls back and keeps the previous snapshot."""
    previous = snapshot_status(conn)
    check_plausible(snapshot, previous["prefix_count"] if previous else 0)
    now = utcnow().isoformat()
    stats = IxpApplyStats(exchanges=len(snapshot.exchanges), prefixes=len(snapshot.prefixes))
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        old_prefixes = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT prefix, ix_id, ixlan_id FROM ixp_prefixes")}
        new_prefixes = {p.prefix: p for p in snapshot.prefixes}
        for gone in sorted(set(old_prefixes) - set(new_prefixes)):
            conn.execute("DELETE FROM ixp_prefixes WHERE prefix = ?", (gone,))
            stats.removed += 1
        keep_ids = [e.ix_id for e in snapshot.exchanges]
        old_ids = {row[0] for row in conn.execute("SELECT ix_id FROM ixp_exchanges")}
        for gone_id in sorted(old_ids - set(keep_ids)):
            conn.execute("DELETE FROM ixp_exchanges WHERE ix_id = ?", (gone_id,))
        for ex in sorted(snapshot.exchanges, key=lambda e: e.ix_id):
            conn.execute(
                """INSERT INTO ixp_exchanges
                (ix_id, name, city, country, location_class, ref_city, ref_country, ref_lat, ref_lon,
                 ref_radius_km, facility_count, source, import_run_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ix_id) DO UPDATE SET
                    name = excluded.name, city = excluded.city, country = excluded.country,
                    location_class = excluded.location_class, ref_city = excluded.ref_city,
                    ref_country = excluded.ref_country, ref_lat = excluded.ref_lat, ref_lon = excluded.ref_lon,
                    ref_radius_km = excluded.ref_radius_km, facility_count = excluded.facility_count,
                    source = excluded.source, import_run_id = excluded.import_run_id,
                    updated_at = excluded.updated_at""",
                (
                    ex.ix_id, ex.name, ex.city, ex.country, ex.location_class, ex.ref_city, ex.ref_country,
                    ex.ref_lat, ex.ref_lon, ex.ref_radius_km, ex.facility_count, SOURCE_PEERINGDB_IXP, run_id, now,
                ),
            )  # fmt: skip
        for prefix in sorted(new_prefixes):
            item = new_prefixes[prefix]
            existed = old_prefixes.get(prefix)
            if existed == (item.ix_id, item.ixlan_id):
                continue
            conn.execute(
                """INSERT INTO ixp_prefixes
                (prefix, ip_version, prefix_length, ix_id, ixlan_id, source, import_run_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prefix) DO UPDATE SET
                    ip_version = excluded.ip_version, prefix_length = excluded.prefix_length,
                    ix_id = excluded.ix_id, ixlan_id = excluded.ixlan_id,
                    import_run_id = excluded.import_run_id, updated_at = excluded.updated_at""",
                (prefix, item.ip_version, item.prefix_length, item.ix_id, item.ixlan_id, SOURCE_PEERINGDB_IXP, run_id, now),
            )  # fmt: skip
            if existed is None:
                stats.inserted += 1
            else:
                stats.updated += 1
        orphans = conn.execute("PRAGMA foreign_key_check(ixp_prefixes)").fetchall()
        if orphans:
            raise sqlite3.IntegrityError(f"{len(orphans)} IXP prefix(es) reference an unknown exchange")
        conn.execute(
            """INSERT INTO ixp_snapshot (id, import_run_id, completed_at, exchange_count, prefix_count, source)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET import_run_id = excluded.import_run_id,
                completed_at = excluded.completed_at, exchange_count = excluded.exchange_count,
                prefix_count = excluded.prefix_count, source = excluded.source""",
            (run_id, now, len(snapshot.exchanges), len(snapshot.prefixes), SOURCE_PEERINGDB_IXP),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    _log.info(
        "IXP snapshot %s active: %d exchanges, %d prefixes (+%d ~%d -%d)",
        run_id, stats.exchanges, stats.prefixes, stats.inserted, stats.updated, stats.removed,
    )  # fmt: skip
    return stats


def load_snapshot(conn: sqlite3.Connection) -> tuple[list[IxpExchange], list[IxpPrefix]]:
    """Read the stored snapshot; an inconsistent row (unknown exchange) is skipped, not trusted."""
    exchanges = [
        IxpExchange(*row)
        for row in conn.execute(
            "SELECT ix_id, name, city, country, location_class, ref_city, ref_country, ref_lat, ref_lon,"
            " ref_radius_km, facility_count FROM ixp_exchanges"
        )
    ]
    known = {e.ix_id for e in exchanges}
    prefixes = [
        IxpPrefix(*row)
        for row in conn.execute("SELECT prefix, ip_version, prefix_length, ix_id, ixlan_id FROM ixp_prefixes")
        if row[3] in known
    ]
    return exchanges, prefixes
