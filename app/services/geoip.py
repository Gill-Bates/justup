#!/usr/bin/env python3
#
# app/services/geoip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The GeoLite2 City and ASN databases of the data directory, served by ``gillbates_geoip``.

The files live at fixed paths inside the geoip data directory; they never come from a setting or a
request. Download, verification, reader lifecycle and lookup caching belong to the package.
"""

from __future__ import annotations

import threading
from pathlib import Path

from gillbates_geoip import GeoIPConfig, GeoIPDatabases

from app.utils.constants import GEOIP_ASN_DB_RELPATH

USER_AGENT = "justUp!"
GEOIP_MAX_AGE_HOURS = 45 * 24  # re-download unconditionally after this age
# Plausibility floors (package defaults: City 10 MB, ASN 1 MB); module values so a test can use tiny files.
MIN_CITY_BYTES = 10_000_000
MIN_ASN_BYTES = 1_000_000

_lock = threading.Lock()
_instances: dict[tuple[Path, Path], GeoIPDatabases] = {}


def databases(city_path: Path) -> GeoIPDatabases:
    """Shared databases for ``city_path``; the ASN file sits beside it. One instance per path pair."""
    key = (city_path, city_path.parent / GEOIP_ASN_DB_RELPATH.name)
    with _lock:
        found = _instances.get(key)
        if found is None:
            found = _instances[key] = GeoIPDatabases(
                GeoIPConfig(
                    city_path=key[0],
                    asn_path=key[1],
                    user_agent=USER_AGENT,
                    max_age_hours=GEOIP_MAX_AGE_HOURS,
                    min_city_size=MIN_CITY_BYTES,
                    min_asn_size=MIN_ASN_BYTES,
                )
            )
        return found


def close_all() -> None:
    """Close every opened reader and forget the instances (tests, shutdown)."""
    with _lock:
        for instance in _instances.values():
            instance.close()
        _instances.clear()
