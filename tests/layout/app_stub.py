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
from datetime import UTC, datetime
from pathlib import Path



def _exit_with_parent() -> None:
    """Ask Linux to SIGTERM this app when the launching test worker dies (e.g. SIGKILL on timeout)."""
    if sys.platform != "linux":
        return
    import ctypes
    import os
    import signal

    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG = 1; the parent may already be gone before the call took effect.
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) == 0 and os.getppid() != parent:
        sys.exit(0)


if __name__ == "__main__":
    _exit_with_parent()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services import nmap_client, traceroute_service
from app.services import traceroute_geo as tg
from app.services.route_model import Route, RouteHop

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


async def _request_traceroute(host, **_kwargs):
    """Offline stand-in for the Nmap worker: the fixed route above as a Route, no network."""
    hops = []
    for number, (ip, _site) in enumerate(_ROUTE, 1):
        if ip == "*":
            hops.append(RouteHop(hop_number=number, status="no_reply"))
        else:
            hops.append(
                RouteHop(
                    hop_number=number,
                    ip=ip,
                    hostname=f"core{number}.carrier-example.net",
                    rtt_ms=3.0 * number,
                    status="ok",
                )
            )
    now = datetime.now(UTC)
    return Route(
        destination_ip=host,
        address_family="ipv4",
        started_at=now,
        completed_at=now,
        duration_ms=0,
        destination_reached=True,
        complete=True,
        hops=hops,
    )


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


nmap_client.request_traceroute = _request_traceroute
traceroute_service.resolve_target_ip = lambda _host: _ROUTE[-1][0]
traceroute_service._add_reverse_dns = lambda _hops: None  # noqa: SLF001
tg.geolocate_with_hostname_hint = _geolocate

if __name__ == "__main__":
    from app.__main__ import main

    sys.exit(main() or 0)
