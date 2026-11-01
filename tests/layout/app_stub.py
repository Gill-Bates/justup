#!/usr/bin/env python3
#
# tests/layout/app_stub.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Layout-check launcher: starts the app with a deterministic, offline traceroute.

The route model is fixed so the target page always renders the map, the route timeline and
the hop table without measuring or geolocating anything on the network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import mtr_run
from app.services import traceroute_geo as tg

_ROUTE = [
    ("10.0.0.1", None),
    ("203.0.113.2", ("Frankfurt", "DE", 50.11, 8.68)),
    ("*", None),
    ("203.0.113.3", ("Amsterdam", "NL", 52.37, 4.90)),
    ("203.0.113.4", ("London", "GB", 51.50, -0.12)),
    ("203.0.113.5", ("New York", "US", 40.71, -74.00)),
    ("203.0.113.6", ("Frankfurt", "DE", 50.11, 8.68)),
    ("203.0.113.7", ("Frankfurt", "DE", 50.11, 8.68)),
]
_SITES = {ip: site for ip, site in _ROUTE if site}


def _run_mtr(_host, **_kwargs):
    hops = []
    for number, (ip, _site) in enumerate(_ROUTE, 1):
        if ip == "*":
            hops.append(
                {
                    "hop": number,
                    "ip": ip,
                    "hostname": None,
                    "hostname_display": None,
                    "latency_ms": None,
                    "loss_pct": 100.0,
                    "status": "timeout",
                }
            )
            continue
        name = f"core{number}.carrier-example.net"
        hops.append(
            {
                "hop": number,
                "ip": ip,
                "hostname": name,
                "hostname_display": name,
                "latency_ms": {"avg": 3.0 * number, "min": 2.0 * number, "max": 4.0 * number},
                "loss_pct": 0.0,
                "status": "ok",
            }
        )
    return hops


def _geolocate(ip, _hostname=None):
    site = _SITES.get(ip)
    if site is None:
        return None
    city, country, lat, lon = site
    return {
        "lat": lat,
        "lon": lon,
        "city": city,
        "position_city": city,
        "country": country,
        "accuracy_radius_km": 5.0,
        "city_is_region": False,
        "accuracy_class": "precise",
        tg.META_CONFIDENCE: "geoip",
    }


mtr_run.run_mtr = _run_mtr
mtr_run.resolve_target_ip = lambda _host: _ROUTE[-1][0]
tg.geolocate_with_hostname_hint = _geolocate

if __name__ == "__main__":
    from app.__main__ import main

    sys.exit(main() or 0)
