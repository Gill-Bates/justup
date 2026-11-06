#!/usr/bin/env python3
#
# tools/verify-nmap-worker.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Check the Nmap worker's privilege separation and scans inside a running justUp container.

``capabilities``: the worker runs as UID 10001 with CAP_NET_RAW as its only capability, the web
process holds none, both run with no_new_privs.
``binary <arch>``: the shipped nmap is an ELF for that architecture whose libraries resolve.
``status``: the supervisor published state ``ready``.
``trace``: as the web user, traceroutes through the worker socket: the container's default gateway
(a real route or a controlled ``no_trace``) and the documentation address 192.0.2.1 (route or
controlled ``no_trace``/``timeout``). ``permission_denied`` or ``unavailable`` fail the check, which
proves the ``--privileged`` raw-socket path works under NET_RAW only. It does not prove a multi-hop
path; no reproducible multi-hop lab exists on a CI runner.
``scan``: as the web user, TCP, UDP and -sV scans of the container's own address go through
the worker socket and come back as parsed results.
docker-build.yml pipes this script into the container (``python - <mode> < tools/verify-nmap-worker.py``).
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import struct
import subprocess
import sys
import threading
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


ELF_MACHINE = {"amd64": 62, "arm64": 183}


DT_RPATH, DT_RUNPATH, DT_NULL, PT_DYNAMIC = 15, 29, 0, 2


def dynamic_tags(image: bytes) -> set[int]:
    """DT_* tags of an ELF64 little-endian file (the dynamic section, found via the program headers)."""
    phoff, phentsize, phnum = struct.unpack_from("<Q", image, 32)[0], *struct.unpack_from("<HH", image, 54)
    tags: set[int] = set()
    for index in range(phnum):
        kind, _flags, offset, _vaddr, _paddr, filesz, _memsz, _align = struct.unpack_from(
            "<IIQQQQQQ", image, phoff + index * phentsize
        )
        if kind != PT_DYNAMIC:
            continue
        for position in range(offset, offset + filesz, 16):
            tag = struct.unpack_from("<q", image, position)[0]
            if tag == DT_NULL:
                break
            tags.add(tag)
    return tags


def check_binary(arch: str) -> None:
    """Shipped nmap: ELF64 for this architecture, loader present, every library resolvable, probes found."""
    image = Path("/usr/bin/nmap").read_bytes()
    header = image[:20]
    require(header[:4] == b"\x7fELF" and header[4] == 2 and header[5] == 1, "nmap is not a little-endian ELF64")
    require(struct.unpack_from("<H", header, 18)[0] == ELF_MACHINE[arch], f"nmap is not built for {arch}")
    require(not {DT_RPATH, DT_RUNPATH} & dynamic_tags(image), "nmap carries an RPATH/RUNPATH")
    libs = subprocess.run(["ldd", "/usr/bin/nmap"], capture_output=True, text=True, check=False, timeout=30)
    require(libs.returncode == 0 and "not found" not in libs.stdout, libs.stdout + libs.stderr)
    require(not re.search(r"/(tmp|build|out)/", libs.stdout), libs.stdout)
    version = subprocess.run(["/usr/bin/nmap", "--version"], capture_output=True, text=True, check=False, timeout=30)
    require(version.returncode == 0 and "nmap-service-probes" not in version.stderr, version.stderr)
    for data in ("nmap-services", "nmap-service-probes", "nmap-protocols", "nmap-rpc"):
        require(Path("/usr/share/nmap", data).stat().st_size > 0, data)
    print(f"nmap ELF/loader/libraries/data files ok for {arch}")


def check_status(expected: str) -> None:
    status = json.loads(Path("/run/justup-nmap.status").read_text(encoding="utf-8"))
    require(status.get("state") == expected, status)
    print(f"worker status: {status}")


def _answer(connection: socket.socket) -> None:
    with connection:
        connection.settimeout(15)  # nmap waits for a banner before it sends its HTTP probe
        try:
            request = connection.recv(1024)
            if request.startswith(b"GET "):
                body = b"ok"
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nServer: nginx\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 2\r\nConnection: close\r\n\r\n" + body
                )
        except OSError:
            return


def serve_http(listener: socket.socket) -> None:
    """Answer every connection like a small web server, so -sV has a service to identify."""
    while True:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_answer, args=(connection,), daemon=True).start()


async def check_scan() -> None:
    from app.services.nmap_client import request_scan

    address = socket.gethostbyname(socket.gethostname())
    with socket.socket() as listener:
        listener.bind((address, 0))
        listener.listen()
        tcp_port = listener.getsockname()[1]
        threading.Thread(target=serve_http, args=(listener,), daemon=True).start()
        result = await request_scan(address, tcp_ports=[tcp_port], udp_ports=[9], timeout=40)
        states = {(p.protocol, p.port): p.state for host in result.hosts for p in host.ports}
        require(states.get(("tcp", tcp_port)) == "open", states)
        require(states.get(("udp", 9)) in UDP_STATES, states)
        detected = await request_scan(address, tcp_ports=[tcp_port], timeout=60, detect_services=True)
        service = next((p.service for h in detected.hosts for p in h.ports if p.port == tcp_port), None)
        require(service is not None and service.method == "probed" and service.name == "http", service)
    print(f"TCP connect scan, UDP scan and -sV through the worker: {states}, service {service.name}")


def default_gateway() -> str | None:
    """IPv4 default gateway from /proc/net/route, or None."""
    for line in Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))
    return None


async def check_trace() -> None:
    from app.services.nmap_client import NmapScannerError, request_traceroute

    targets = [t for t in (default_gateway(), "192.0.2.1") if t]
    outcomes = []
    for host in targets:
        try:
            route = await request_traceroute(host, timeout=40)
        except NmapScannerError as error:
            require(error.code in ("no_trace", "timeout"), f"{host}: {error.code}: {error}")
            outcomes.append(f"{host}: {error.code}")
            continue
        require(route.destination_ip == host and route.hops, f"{host}: {route}")
        numbers = [hop.hop_number for hop in route.hops]
        require(numbers == sorted(set(numbers)) and numbers[0] >= 1, f"{host}: hop order {numbers}")
        outcomes.append(f"{host}: {len(route.hops)} hops, reached={route.destination_reached}")
    print(f"traceroute through the worker: {'; '.join(outcomes)}")


def main(argv: list[str]) -> int:
    if argv not in (["capabilities"], ["scan"], ["status"], ["trace"]) and not (len(argv) == 2 and argv[0] == "binary"):
        print("Usage: verify-nmap-worker.py capabilities|scan|trace|status|binary <amd64|arm64>", file=sys.stderr)
        return 2
    try:
        if argv == ["capabilities"]:
            check_capabilities()
        elif argv[0] == "binary":
            if argv[1] not in ELF_MACHINE:
                raise CheckError(f"unsupported architecture {argv[1]}")
            check_binary(argv[1])
        elif argv == ["trace"]:
            asyncio.run(check_trace())
        elif argv == ["status"]:
            check_status("ready")
        else:
            asyncio.run(check_scan())
    except (CheckError, OSError, ValueError, KeyError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
