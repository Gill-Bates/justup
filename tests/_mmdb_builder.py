#!/usr/bin/env python3
#
# tests/_mmdb_builder.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Smallest possible MaxMind DB writer for tests: a few networks, 24-bit records, IPv6 tree.

Only what the GeoLite2-ASN reader needs: maps of strings and unsigned integers. IPv4 networks live
under ``::/96`` like in real databases. Nothing here touches the network.
"""

from __future__ import annotations

import ipaddress
import struct
from pathlib import Path

_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"


def _control(type_id: int, size: int) -> bytes:
    assert size < 285
    extra = b"" if size < 29 else bytes([size - 29])
    head = min(29, size)
    if type_id <= 7:
        return bytes([(type_id << 5) | head]) + extra
    return bytes([head, type_id - 7]) + extra


def _uint(type_id: int, value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return _control(type_id, len(raw)) + raw


def _string(value: str) -> bytes:
    raw = value.encode()
    return _control(2, len(raw)) + raw


def _map(items: dict[str, bytes]) -> bytes:
    out = _control(7, len(items))
    for key, encoded in items.items():
        out += _string(key) + encoded
    return out


def asn_record(number: int, organisation: str | None) -> bytes:
    items = {"autonomous_system_number": _uint(6, number)}
    if organisation is not None:
        items["autonomous_system_organization"] = _string(organisation)
    return _map(items)


def build_asn_mmdb(
    path: Path, networks: dict[str, tuple[int, str | None]], *, database_type: str = "GeoLite2-ASN"
) -> None:
    """Write a database mapping each network (``"93.184.216.0/24"``, ``"2606:2800::/32"``) to an ASN record."""
    data = b""
    offsets: dict[str, int] = {}
    nodes: list[list[tuple[str, int] | None]] = [[None, None]]
    for cidr, (number, org) in networks.items():
        net = ipaddress.ip_network(cidr)
        value = int(net.network_address)  # an IPv4 value has 96 leading zero bits in the 128-bit tree
        length = net.prefixlen + (96 if net.version == 4 else 0)
        offsets[cidr] = len(data)
        data += asn_record(number, org)
        idx = 0
        for depth in range(length):
            bit = (value >> (127 - depth)) & 1
            if depth == length - 1:
                nodes[idx][bit] = ("data", offsets[cidr])
                break
            child = nodes[idx][bit]
            if child is None or child[0] != "node":
                nodes.append([None, None])
                child = ("node", len(nodes) - 1)
                nodes[idx][bit] = child
            idx = child[1]
    count = len(nodes)

    def pointer(entry: tuple[str, int] | None) -> int:
        if entry is None:
            return count
        return entry[1] if entry[0] == "node" else count + 16 + entry[1]

    tree = b"".join(
        struct.pack(">I", pointer(left))[1:] + struct.pack(">I", pointer(right))[1:] for left, right in nodes
    )
    metadata = _map(
        {
            "node_count": _uint(6, count),
            "record_size": _uint(5, 24),
            "ip_version": _uint(5, 6),
            "database_type": _string(database_type),
            "binary_format_major_version": _uint(5, 2),
            "binary_format_minor_version": _uint(5, 0),
            "build_epoch": _uint(9, 1_700_000_000),
            "languages": _control(11, 0),  # empty array
            "description": _map({"en": _string("test database")}),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tree + b"\x00" * 16 + data + _METADATA_MARKER + metadata)
