#!/usr/bin/env python3
#
# tests/test_traceroute_characterisation.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Characterisation of the web and PDF traceroute pipelines (T8): pins today's behaviour.

Covers the invariants that are shared today and not yet asserted in test_traceroute_pipeline.py:
IP family choice for MTR, the argument MTR is run with, and the latency of the synthetic destination.
They guard a later extraction of shared code; a deliberate behaviour change updates them on purpose.
"""

# ruff: noqa: SLF001
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.api import frontend
from app.services import mtr_run, pdf_report, traceroute_geo
from tests.test_traceroute_pipeline import HOST, ROUTES, TARGET_IP, _geo, _patch_sources


def _record_mtr(monkeypatch: pytest.MonkeyPatch, hops: list[dict], resolved: str | None) -> list[tuple[Any, Any]]:
    """Patch both pipelines to the same MTR result and record (host argument, force_ipv6) per call."""
    _patch_sources(monkeypatch, hops)
    for module in (traceroute_geo, pdf_report):  # the shared stub geolocates IPv4 only
        monkeypatch.setattr(
            module, "geolocate_with_hostname_hint", lambda ip, h=None: None if ":" in ip else _geo(ip, h), raising=False
        )
    calls: list[tuple[Any, Any]] = []

    def run(host, *_a, force_ipv6=None, **_k):
        calls.append((host, force_ipv6))
        return [dict(h) for h in hops]

    for module in (mtr_run, pdf_report):
        monkeypatch.setattr(module, "resolve_target_ip", lambda _h: resolved, raising=False)
        monkeypatch.setattr(module, "run_mtr", run, raising=False)
    return calls


@pytest.mark.parametrize(
    ("resolved", "host", "force_ipv6"),
    [
        (TARGET_IP, HOST, False),
        ("2001:db8::9", HOST, True),
        (None, "2001:db8::1", True),
        (None, HOST, None),
    ],
    ids=["resolved-v4", "resolved-v6", "unresolved-v6-literal", "unresolved-name"],
)
def test_both_pipelines_choose_the_same_ip_family(
    monkeypatch: pytest.MonkeyPatch, resolved: str | None, host: str, force_ipv6: bool | None
) -> None:
    calls = _record_mtr(monkeypatch, ROUTES["reached"], resolved)
    pdf_report._run_mtr_with_interpolation(host)
    _web_context_for(monkeypatch, host)
    (_pdf_host, pdf_family), (_web_host, web_family) = calls
    assert pdf_family is force_ipv6
    assert web_family is force_ipv6


def test_mtr_is_run_with_the_resolved_ip_on_the_web_and_in_the_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_mtr(monkeypatch, ROUTES["reached"], TARGET_IP)
    pdf_report._run_mtr_with_interpolation(HOST)
    _web_context_for(monkeypatch, HOST)
    assert [host for host, _family in calls] == [TARGET_IP, TARGET_IP]


def test_synthetic_destination_keeps_the_last_responding_hop_latency_only_as_an_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_mtr(monkeypatch, ROUTES["trailing_timeout"], TARGET_IP)
    pdf_hops, _points, _interpolated = pdf_report._run_mtr_with_interpolation(HOST)
    web_hops = _web_context_for(monkeypatch, HOST)["hops"]
    for hops in (pdf_hops, web_hops):
        synthetic = next(h for h in hops if h.get("_synthetic_destination"))
        assert synthetic["latency_ms"] is None  # never claims a measurement for the unreached target
        assert synthetic["_estimated_latency_ms"] == 5.0  # avg of hop 2, the last one that answered


def _web_context_for(monkeypatch: pytest.MonkeyPatch, host: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def render(_request, _name, ctx):
        captured.update(ctx)
        return SimpleNamespace(body=b"", headers={})

    monkeypatch.setattr(frontend, "templates", SimpleNamespace(TemplateResponse=render))
    monkeypatch.setattr(frontend, "_cache_traceroute_fragment", lambda *_a, **_k: None)
    asyncio.run(frontend._run_traceroute_fragment(SimpleNamespace(), 1, host, "ns"))
    return captured
