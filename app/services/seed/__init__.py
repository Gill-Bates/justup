#!/usr/bin/env python3
#
# app/services/seed/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
POP location database seeders from public data sources.

Available seeders:
- iata_loader: IATA airport codes (~9,500 codes)
- peeringdb_loader: PeeringDB facilities and IXPs (~4,000 entries)

Usage:
    from app.services.seed import seed_all_sources
    seed_all_sources(conn)
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)


def seed_all_sources(
    conn: sqlite3.Connection,
    *,
    skip_network: bool = False,
) -> dict[str, int]:
    """
    Seed POP locations from all available sources.
    
    Order matters: higher-priority sources first (they won't be overwritten
    by INSERT OR IGNORE).
    
    Args:
        conn: SQLite connection
        skip_network: If True, skip sources requiring network access
        
    Returns:
        Dict of source -> count of inserted records
    """
    results: dict[str, int] = {}
    
    # PeeringDB (priority 20-25) - requires network
    if not skip_network:
        try:
            from .peeringdb_loader import seed_peeringdb_facilities, seed_peeringdb_ixps
            
            n = seed_peeringdb_facilities(conn)
            results["peeringdb_facilities"] = n
            _log.info("Seeded %d PeeringDB facilities", n)
            
            n = seed_peeringdb_ixps(conn)
            results["peeringdb_ixps"] = n
            _log.info("Seeded %d PeeringDB IXPs", n)
        except Exception as e:
            _log.warning("PeeringDB seeding failed: %s", e)
    
    # IATA airport codes (priority 90) - requires network
    if not skip_network:
        try:
            from .iata_loader import seed_iata_codes
            
            n = seed_iata_codes(conn)
            results["iata"] = n
            _log.info("Seeded %d IATA airport codes", n)
        except Exception as e:
            _log.warning("IATA seeding failed: %s", e)
    
    return results


__all__ = ["seed_all_sources"]
