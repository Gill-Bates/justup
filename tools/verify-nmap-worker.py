#!/usr/bin/env python3
#
# tools/verify-nmap-worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Check the Nmap worker's privilege separation and scans inside a running justUp container.

``capabilities``: the worker runs as UID 10001 with CAP_NET_RAW as its only capability, the web
process holds none, both run with no_new_privs. ``scan``: as the web user, a TCP and a UDP scan
of the container's own address go through the worker socket and come back as parsed results.
docker-build.yml pipes this script into the container (``python - <mode> < tools/verify-nmap-worker.py``).
"""

from __future__ import annotations

import asyncio
import re
import socket
import sys
from pathlib import Path

NET_RAW = 1 << 13
WORKER_UID = 10001
UDP_STATES = {"open", "closed", "filtered", "open|filtered"}


class CheckError(Exception):
    """A verification expectation was not met."""


def require(condition: bool, message: object) -> None:
    if not condition:
        raise CheckError(message)


def _status(pid: str) -> dict[str, str]:
    return dict(re.findall(r"^(\w+):\s+(.*)$", Path(f"/proc/{pid}/status").read_text(encoding="utf-8"), re.MULTILINE))


def processes() -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            status = _status(entry.name)
        except OSError:
            continue
        if b"app.services.nmap_worker" in argv:
            found["worker"] = status
        elif argv[:3] == [b"python", b"-m", b"app"]:
            found["web"] = status
    return found


def check_capabilities() -> None:
    seen = processes()
    require({"worker", "web"} <= seen.keys(), f"processes not found: {sorted(seen)}")
    worker, web = seen["worker"], seen["web"]
    require(int(worker["Uid"].split()[0]) == WORKER_UID, worker["Uid"])
    web_uid = int(web["Uid"].split()[0])
    require(web_uid not in (0, WORKER_UID), web["Uid"])
    for key in ("CapEff", "CapPrm", "CapAmb"):
        require(int(worker[key], 16) == NET_RAW, (key, worker[key]))
        require(int(web[key], 16) == 0, (key, web[key]))
    require(worker["NoNewPrivs"] == "1" and web["NoNewPrivs"] == "1", "no_new_privs missing")
    print(f"worker uid={WORKER_UID} caps=only CAP_NET_RAW; web uid={web_uid} caps=none")


async def check_scan() -> None:
    from app.services.nmap_client import request_scan

    address = socket.gethostbyname(socket.gethostname())
    with socket.socket() as listener:
        listener.bind((address, 0))
        listener.listen()
        tcp_port = listener.getsockname()[1]
        result = await request_scan(address, tcp_ports=[tcp_port], udp_ports=[9], timeout=40)
    states = {(p.protocol, p.port): p.state for host in result.hosts for p in host.ports}
    require(states.get(("tcp", tcp_port)) == "open", states)
    require(states.get(("udp", 9)) in UDP_STATES, states)
    print(f"TCP connect scan and UDP scan through the worker: {states}")


def main(argv: list[str]) -> int:
    if argv not in (["capabilities"], ["scan"]):
        print("Usage: verify-nmap-worker.py capabilities|scan", file=sys.stderr)
        return 2
    try:
        check_capabilities() if argv == ["capabilities"] else asyncio.run(check_scan())
    except CheckError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
