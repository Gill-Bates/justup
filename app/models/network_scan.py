#!/usr/bin/env python3
#
# app/models/network_scan.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Typed results shared by Nmap scanning and persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Protocol = Literal["tcp", "udp"]
ScanState = Literal["open", "closed", "filtered", "open|filtered", "unfiltered", "closed|filtered", "unknown"]


class NmapServiceInfo(BaseModel):
    name: str | None = None
    product: str | None = None
    version: str | None = None
    method: Literal["probed", "table"] | None = None
    confidence: int | None = Field(default=None, ge=0, le=10)


class NmapPortResult(BaseModel):
    protocol: Protocol
    port: int = Field(ge=1, le=65535)
    state: ScanState
    reason: str | None = None
    service: NmapServiceInfo | None = None


class NmapHostResult(BaseModel):
    address: str
    status: str | None = None
    ports: list[NmapPortResult] = Field(default_factory=list)


class NmapScanResult(BaseModel):
    hosts: list[NmapHostResult] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    complete: bool
