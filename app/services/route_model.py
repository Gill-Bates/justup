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

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

HopStatus = Literal["ok", "no_reply"]
ProbeProtocol = Literal["icmp", "tcp", "udp"]
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
    probe_protocol: ProbeProtocol | None = None
    probe_port: int | None = Field(default=None, ge=1, le=65535)
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    destination_reached: bool
    complete: bool
    hops: list[RouteHop] = Field(default_factory=list, max_length=MAX_HOPS)
