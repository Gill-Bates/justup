#!/usr/bin/env python3
#
# app/services/route_model.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tool-independent traceroute route model shared by the worker, the web UI and the PDF report.

A route is a path measurement, not a loss or latency statistic: a hop carries at most ONE round-trip
time of one probe, and a hop that did not answer carries no address, no RTT and no location.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

HopStatus = Literal["ok", "no_reply"]
# What a caller may request (a port probe) differs from what a route may report (Nmap also traces via ICMP).
RequestedProbeProtocol = Literal["tcp", "udp"]
ObservedProbeProtocol = Literal["icmp", "tcp", "udp"]
ProbeProtocol = ObservedProbeProtocol
MAX_HOPS = 64


class RouteHop(BaseModel):
    hop_number: int = Field(ge=1, le=255)
    ip: str | None = None
    hostname: str | None = Field(default=None, max_length=255)
    rtt_ms: float | None = Field(default=None, ge=0, le=60_000)
    status: HopStatus

    def model_post_init(self, __context) -> None:
        if self.status == "no_reply" and (self.ip or self.rtt_ms is not None or self.hostname):
            raise ValueError("A hop without reply carries no address, hostname or RTT")
        if self.status == "ok" and not self.ip:
            raise ValueError("An answering hop needs an address")


class Route(BaseModel):
    destination_ip: str
    address_family: Literal["ipv4", "ipv6"]
    probe_protocol: ObservedProbeProtocol | None = None
    probe_port: int | None = Field(default=None, ge=1, le=65535)
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    destination_reached: bool
    complete: bool
    hops: list[RouteHop] = Field(default_factory=list, max_length=MAX_HOPS)

    @model_validator(mode="after")
    def validate_consistency(self):
        """Reject routes whose parts contradict each other, so a faulty worker response never gets through."""
        try:
            destination = ipaddress.ip_address(self.destination_ip)
        except ValueError as exc:
            raise ValueError("Invalid destination address") from exc
        if self.address_family != ("ipv6" if destination.version == 6 else "ipv4"):
            raise ValueError("Address family does not match the destination")
        previous = 0
        for hop in self.hops:
            if hop.hop_number <= previous:
                raise ValueError("Hop numbers must be unique and strictly ascending")
            previous = hop.hop_number
            if hop.ip is not None and ipaddress.ip_address(hop.ip).version != destination.version:
                raise ValueError("Hop address family does not match the destination")
        try:
            elapsed_ms = (self.completed_at - self.started_at).total_seconds() * 1000
        except TypeError as exc:
            raise ValueError("Start and end time must both be timezone-aware or both naive") from exc
        if elapsed_ms < 0 or abs(elapsed_ms - self.duration_ms) > 1:
            raise ValueError("Inconsistent start, end and duration")
        if (self.probe_protocol in {"tcp", "udp"}) != (self.probe_port is not None):
            raise ValueError("A port belongs to TCP and UDP probes only")
        if self.destination_reached and not (
            self.hops and self.hops[-1].ip and ipaddress.ip_address(self.hops[-1].ip) == destination
        ):
            raise ValueError("A reached destination must be the last answering hop")
        return self
