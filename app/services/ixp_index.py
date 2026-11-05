#!/usr/bin/env python3
#
# app/services/ixp_index.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""In-memory longest-prefix index over the IXP prefix snapshot (IPv4 and IPv6).

Lookups never touch the network and never scan the table: one dict probe per distinct prefix length.
The index is an immutable object swapped atomically, so parallel traceroutes read a consistent
snapshot while a refresh replaces it. A failed (re)load keeps the active index.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from collections.abc import Iterable

from ..db import ixp_prefixes
from ..db import sqlite as sqlite_db
from ..db.ixp_prefixes import IxpExchange, IxpPrefix
from . import geo_resolver as gr

_log = logging.getLogger(__name__)

LOAD_RETRY_SECONDS = 30.0  # a failing load is not retried per hop


class IxpIndex:
    """Immutable prefix index; build once, read from any thread."""

    __slots__ = ("_by_len", "_lengths", "exchange_count", "prefix_count")

    def __init__(self, exchanges: Iterable[IxpExchange], prefixes: Iterable[IxpPrefix]) -> None:
        by_ix = {e.ix_id: e for e in exchanges}
        by_len: dict[tuple[int, int], dict[int, gr.IxpEvidence]] = {}
        count = 0
        for item in prefixes:
            exchange = by_ix.get(item.ix_id)
            if exchange is None:
                continue  # prefix of an unknown exchange is never matched
            try:
                network = ipaddress.ip_network(item.prefix, strict=True)
            except ValueError:
                continue
            evidence = gr.IxpEvidence(
                ix_id=exchange.ix_id,
                name=exchange.name,
                prefix=str(network),
                location_class=exchange.location_class,
                city=exchange.ref_city,
                country=exchange.ref_country,
                latitude=exchange.ref_lat,
                longitude=exchange.ref_lon,
                radius_km=exchange.ref_radius_km,
            )
            slot = by_len.setdefault((network.version, network.prefixlen), {})
            if int(network.network_address) in slot:
                continue  # duplicate rows cannot occur through the primary key; first one wins deterministically
            slot[int(network.network_address)] = evidence
            count += 1
        self._by_len = by_len
        self._lengths = {
            v: sorted((plen for (ver, plen) in by_len if ver == v), reverse=True) for v in (4, 6)
        }
        self.exchange_count = len(by_ix)
        self.prefix_count = count

    def lookup(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> gr.IxpEvidence | None:
        bits = 32 if address.version == 4 else 128
        value = int(address)
        for plen in self._lengths[address.version]:
            masked = value >> (bits - plen) << (bits - plen)
            hit = self._by_len[(address.version, plen)].get(masked)
            if hit is not None:
                return hit  # longest prefix first
        return None


_lock = threading.Lock()  # guards swaps and the load state, never held during a DB read
_load_lock = threading.Lock()  # one loader at a time
_index: IxpIndex | None = None
_generation = 0
_dirty = True  # True until a load succeeded or after an invalidation
_last_failure = float("-inf")


def current_generation() -> int:
    """Changes whenever the active index is replaced; caches of rendered geo decisions key on it."""
    return _generation


def install(index: IxpIndex | None) -> None:
    """Make *index* the active one (None = no IXP data) and mark it current."""
    global _index, _generation, _dirty
    with _lock:
        _index = index
        _generation += 1
        _dirty = False


def invalidate() -> None:
    """Request a reload from the database at the next lookup; the active index keeps serving meanwhile."""
    global _dirty, _last_failure
    with _lock:
        _dirty = True
        _last_failure = float("-inf")


def load_from_connection(conn) -> IxpIndex:
    exchanges, prefixes = ixp_prefixes.load_snapshot(conn)
    return IxpIndex(exchanges, prefixes)


def reload_from_connection(conn) -> bool:
    """Rebuild the index from committed data. On failure the active index stays and a lazy retry is queued."""
    try:
        index = load_from_connection(conn)
    except Exception:  # noqa: BLE001 - a reload problem must never break lookups or the import result
        _log.warning("IXP index reload failed; keeping the active index", exc_info=True)
        invalidate()
        return False
    install(index)
    return True


def _lazy_load() -> None:
    """Load from the shared POP connection factory when stale; throttled and single-flight."""
    global _last_failure
    if not _dirty or time.monotonic() - _last_failure < LOAD_RETRY_SECONDS:
        return
    if not _load_lock.acquire(blocking=False):
        return  # another thread is loading; serve the active index
    try:
        from .pop_codes import open_pop_connection

        conn = open_pop_connection()
        if conn is None:
            return  # no database wired (unit tests, early startup): no IXP data, no retry storm
        try:
            index = load_from_connection(conn)
        finally:
            sqlite_db.close_connection(conn)
        install(index)
    except Exception:  # noqa: BLE001 - lookups degrade to "no IXP data"
        _last_failure = time.monotonic()
        _log.warning("IXP index load failed; retrying in %.0f s", LOAD_RETRY_SECONDS, exc_info=True)
    finally:
        _load_lock.release()


def lookup(ip: str) -> gr.IxpEvidence | None:
    """Longest-prefix match of *ip* against the active snapshot; None for no match or invalid input."""
    if _dirty:
        _lazy_load()
    index = _index
    if index is None:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return index.lookup(address)


def reset_for_tests() -> None:
    global _index, _generation, _dirty, _last_failure
    with _lock:
        _index, _generation, _dirty, _last_failure = None, _generation + 1, True, float("-inf")
