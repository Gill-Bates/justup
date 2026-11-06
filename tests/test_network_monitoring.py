#!/usr/bin/env python3
#
# tests/test_network_monitoring.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001
"""TCP/UDP/ICMP monitoring: per-port status, Nmap state mapping, scan-engine failures, identity, alerts."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest

from app.db import sqlite as sqlite_db
from app.db import tsdb
from app.db.sqlite.network_ports import SERVICE_NAME_MAX, SERVICE_TEXT_MAX
from app.monitor import checks, scheduler
from app.monitor.scheduler import MonitorScheduler, TargetRuntime, TargetStatus
from app.services import nmap_client
from app.services.nmap_scanner import (
    NmapHostResult,
    NmapPortResult,
    NmapScannerError,
    NmapScanResult,
    NmapServiceInfo,
)
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _sched(db_path: Path, tmp_path: Path) -> MonitorScheduler:
    return MonitorScheduler(get_conn=lambda: sqlite_db.connect(db_path), tsdb_dir=tmp_path / "tsdb")


def _target(db_path: Path, **flags) -> int:
    values = {
        "name": "t",
        "interval_seconds": 60,
        "retention_days": 30,
        "host": "t.example",
        "enable_http_check": False,
        "enable_ping": False,
        "enable_tcp_connect": False,
        "enable_cert_expiration": False,
        "http_url": "https://t.example/",
        "ping_host": "t.example",
        "tcp_host": "t.example",
        "tcp_ports": "80",
    }
    values.update(flags)
    conn = sqlite_db.connect(db_path)
    try:
        tid = sqlite_db.create_target(conn, values)
        tcp = [int(p) for p in str(values["tcp_ports"]).split(",") if values["enable_tcp_connect"]]
        udp = [int(p) for p in str(values.get("udp_ports") or "").split(",") if p]
        sqlite_db.sync_network_ports(conn, tid, tcp_ports=tcp, udp_ports=udp)
        conn.commit()
        return tid
    finally:
        sqlite_db.close_connection(conn)


def _ports(db_path: Path, tid: int) -> dict[tuple[str, int], dict]:
    conn = sqlite_db.connect(db_path)
    try:
        return {(p["protocol"], p["port"]): p for p in sqlite_db.list_network_ports(conn, tid)}
    finally:
        sqlite_db.close_connection(conn)


def _alert_ids(db_path: Path) -> list[str]:
    conn = sqlite_db.connect(db_path)
    try:
        return [r["failed_service"] for r in conn.execute("SELECT failed_service FROM alerts")]
    finally:
        sqlite_db.close_connection(conn)


def _status(db_path: Path, tid: int) -> str:
    conn = sqlite_db.connect(db_path)
    try:
        return sqlite_db.get_target_status(conn, tid)["overall_status"]
    finally:
        sqlite_db.close_connection(conn)


def _values(tmp_path: Path, tid: int, metric: str) -> list[float]:
    return [p.value for p in tsdb.query(tmp_path / "tsdb", target_id=tid, metric=metric, limit=1000)]


def _scan_result(states: dict[int, str], service: NmapServiceInfo | None = None, *, tcp=False) -> NmapScanResult:
    now = utcnow()
    protocol = "tcp" if tcp else "udp"
    ports = [NmapPortResult(protocol=protocol, port=p, state=s, service=service) for p, s in states.items()]
    return NmapScanResult(
        hosts=[NmapHostResult(address="192.0.2.1", status="up", ports=ports)],
        started_at=now,
        completed_at=now,
        complete=True,
    )


def _fake_scan(monkeypatch, outcome):
    """Replace the Nmap worker client; ``outcome`` is a result, an exception or a callable."""
    calls: list[dict] = []

    async def request_scan(host, **kwargs):
        calls.append({"host": host, **kwargs})
        result = outcome() if callable(outcome) else outcome
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(nmap_client, "request_scan", request_scan)
    return calls


async def _cycle(sched: MonitorScheduler, tid: int) -> None:
    """One check cycle; a started UDP background scan is awaited so the next cycle sees its result."""
    await sched._run_target(tid, utcnow())
    task = sched._rt[tid].udp_task
    if task is not None:
        await task


def _run_cycles(sched: MonitorScheduler, tid: int, count: int = 2) -> None:
    async def scenario():
        for _ in range(count):
            await _cycle(sched, tid)

    asyncio.run(scenario())


def _run_confirmed_scans(sched: MonitorScheduler, tid: int, scans: int = 2) -> None:
    """Run ``scans`` full UDP scans (each due again) and one more cycle that consumes the last result."""

    async def scenario():
        for _ in range(scans):
            sched._rt[tid].udp_last_started = None
            await _cycle(sched, tid)
        await sched._run_target(tid, utcnow())

    asyncio.run(scenario())


def _tcp_connect(results: dict[int, tuple[bool, float | None, str | None]]):
    async def tcp_connect(host, port, *a, **k):
        ok, latency, detail = results[port]
        return checks.CheckResult(ok=ok, latency_ms=latency, detail=detail)

    return tcp_connect


# ── Mapping and engine failures (unit level) ─────────────────────────────────


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("open", "up"),
        ("closed", "down"),
        ("filtered", "unknown"),
        ("open|filtered", "unknown"),
        ("closed|filtered", "unknown"),
        ("unfiltered", "unknown"),
        ("unknown", "unknown"),
        (None, "unknown"),
    ],
)
def test_udp_state_mapping(state, status):
    assert checks.udp_state_status(state) == status


def test_udp_scan_keeps_the_nmap_state_and_judges_each_port_separately():
    result = _scan_result({53: "open", 123: "closed", 161: "filtered", 500: "open|filtered"})

    async def scan(host, **kwargs):
        assert kwargs["detect_services"] is False and kwargs["udp_ports"] == [53, 123, 161, 500, 4500]
        return result

    out = asyncio.run(checks.udp_scan_ports("t.example", [53, 123, 161, 500, 4500], scan=scan))
    assert {p: (c.status, c.raw_state) for p, c in out.items()} == {
        53: ("up", "open"),
        123: ("down", "closed"),
        161: ("unknown", "filtered"),
        500: ("unknown", "open|filtered"),
        4500: ("unknown", None),  # missing from the result
    }


@pytest.mark.parametrize(
    "error",
    [
        NmapScannerError("unavailable", "worker down"),
        NmapScannerError("permission_denied", "no raw socket"),
        NmapScannerError("timeout", "slow"),
        NmapScannerError("process_failed", "crashed"),
        NmapScannerError("invalid_xml", "garbage"),
        NmapScannerError("output_limit", "big"),
        NmapScannerError("busy", "busy"),
        RuntimeError("boom"),
        TimeoutError(),
    ],
)
def test_scan_engine_failure_is_unknown_never_down(error):
    async def scan(host, **kwargs):
        raise error

    out = asyncio.run(checks.udp_scan_ports("t.example", [53, 123], scan=scan))
    assert {c.status for c in out.values()} == {"unknown"}
    assert all(c.raw_state is None for c in out.values())


def test_incomplete_result_is_unknown():
    incomplete = _scan_result({53: "closed"}).model_copy(update={"complete": False})

    async def scan(host, **kwargs):
        return incomplete

    assert asyncio.run(checks.udp_scan_ports("t.example", [53], scan=scan))[53].status == "unknown"


def _svc(name="smtp", product="Postfix", version="3.8", method="probed"):
    return NmapServiceInfo(name=name, product=product, version=version, method=method, confidence=10)


def _stored(**kw):
    base = {
        "service_name": "smtp",
        "service_product": "Postfix",
        "service_version": "3.7",
        "service_identity": "identified",
    }
    return {**base, **kw}


def test_changed_version_is_a_note_not_a_change():
    verdict = checks.assess_service_identity(_stored(), _svc(version="3.8"))
    assert verdict.identity == "identified" and "3.7 -> 3.8" in verdict.detail and verdict.version == "3.8"


def test_version_change_is_flagged_by_a_typed_field_and_unchanged_is_not():
    assert checks.assess_service_identity(_stored(), _svc(version="3.8")).version_changed is True
    assert checks.assess_service_identity(_stored(service_version="3.8"), _svc(version="3.8")).version_changed is False


def test_overlong_service_values_match_their_truncated_stored_form():
    name, product, version = "n" * 130, "p" * 200, "v" * 200
    stored = _stored(
        service_name=name[:SERVICE_NAME_MAX],
        service_product=product[:SERVICE_TEXT_MAX],
        service_version=version[:SERVICE_TEXT_MAX],
    )
    verdict = checks.assess_service_identity(stored, _svc(name=name, product=product, version=version))
    assert (verdict.identity, verdict.detail, verdict.version_changed) == ("identified", None, False)


def test_different_service_or_product_is_service_changed():
    assert checks.assess_service_identity(_stored(), _svc(product="Exim")).identity == "changed"
    assert checks.assess_service_identity(_stored(), _svc(name="http", product="nginx")).identity == "changed"
    assert checks.assess_service_identity(_stored(), _svc(product="")).identity == "changed"
    assert checks.assess_service_identity(_stored(service_product=None), _svc()).identity == "changed"


def test_first_probed_identification_becomes_the_base_and_table_hits_are_ignored():
    first = checks.assess_service_identity(_stored(service_name=None, service_identity="unknown"), _svc())
    assert first.identity == "identified" and first.adopt
    assert checks.assess_service_identity(_stored(), _svc(method="table")).identity is None
    assert checks.assess_service_identity(_stored(), None).identity is None


# ── Scheduler: TCP, ICMP, UDP ─────────────────────────────────────────────────


def test_tcp_open_and_closed_ports_get_their_own_status_latency_and_series(db_path, tmp_path):
    tid = _target(db_path, enable_tcp_connect=True, tcp_ports="25,587")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    tcp = _tcp_connect({25: (True, 3.5, None), 587: (False, None, "connection refused")})
    with mock.patch.object(scheduler.checks, "tcp_connect", tcp):
        _run_cycles(sched, tid, 1)
    ports = _ports(db_path, tid)
    assert (ports[("tcp", 25)]["status"], ports[("tcp", 25)]["raw_state"], ports[("tcp", 25)]["latency_ms"]) == (
        "up",
        "open",
        3.5,
    )
    assert (ports[("tcp", 587)]["status"], ports[("tcp", 587)]["raw_state"]) == ("down", "closed")
    assert ports[("tcp", 25)]["service_identity"] == "unknown"  # an open port proves reachability only
    assert _values(tmp_path, tid, "tcp_up_25") == [1] and _values(tmp_path, tid, "tcp_up_587") == [0]
    assert _values(tmp_path, tid, "tcp_up") == [0]
    assert _status(db_path, tid) == TargetStatus.DOWN


@pytest.mark.parametrize(("ok", "expected", "status"), [(True, 1, "up"), (False, 0, "down")])
def test_icmp_keeps_the_ping_implementation_and_is_a_series_of_the_shared_model(
    db_path, tmp_path, ok, expected, status
):
    tid = _target(db_path, enable_ping=True)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    ping = mock.AsyncMock(return_value=checks.CheckResult(ok=ok, latency_ms=1.0 if ok else None, detail="x"))
    with mock.patch.object(scheduler.checks, "ping", ping):
        _run_cycles(sched, tid, 1)
    assert ping.await_count >= 1
    assert _values(tmp_path, tid, "ping_up") == [expected]
    assert _status(db_path, tid) == status
    from app.utils.availability_rules import downtime_series

    assert ("ping_up", "Ping") in downtime_series({"enable_ping": 1})


def test_udp_states_are_mapped_stored_unchanged_and_kept_apart_per_port(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53,123,161,500")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    calls = _fake_scan(monkeypatch, _scan_result({53: "open", 123: "closed", 161: "filtered", 500: "open|filtered"}))
    _run_confirmed_scans(sched, tid)
    assert len(calls) == 2 and calls[0]["detect_services"] is False  # lightweight scan, once per UDP interval
    assert calls[0]["udp_ports"] == [53, 123, 161, 500] and calls[0].get("tcp_ports") is None
    ports = _ports(db_path, tid)
    assert {p: (ports[("udp", p)]["status"], ports[("udp", p)]["raw_state"]) for p in (53, 123, 161, 500)} == {
        53: ("up", "open"),
        123: ("down", "closed"),
        161: ("unknown", "filtered"),
        500: ("unknown", "open|filtered"),
    }
    # one point per finished scan (never a cached result again); the first closed scan is only a pending UNKNOWN
    assert _values(tmp_path, tid, "udp_up_53") == [1, 1] and _values(tmp_path, tid, "udp_up_123") == [0]
    assert _values(tmp_path, tid, "udp_unknown_123") == [1]
    assert _values(tmp_path, tid, "udp_up_161") == [] and _values(tmp_path, tid, "udp_up_500") == []
    assert _values(tmp_path, tid, "udp_unknown_161")[-1] == 1 and _values(tmp_path, tid, "udp_unknown_500")[-1] == 1
    assert _values(tmp_path, tid, "udp_up")[-1] == 0  # aggregate of the confirmed ports: 123 is down
    assert _status(db_path, tid) == TargetStatus.DOWN


def test_udp_with_only_unclear_states_is_unknown_without_downtime_or_alert(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="161,500")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({161: "filtered", 500: "open|filtered"}))
    _run_cycles(sched, tid, 3)
    assert _status(db_path, tid) == TargetStatus.UNKNOWN
    assert _values(tmp_path, tid, "udp_up") == [] and _values(tmp_path, tid, "udp_up_500") == []
    assert sched._rt[tid].down_since_by_service["udp"] is None
    assert _alert_ids(db_path) == []


def test_nmap_outage_is_unknown_the_scheduler_keeps_running_and_nothing_goes_down(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, NmapScannerError("unavailable", "Nmap scan worker is restarting"))
    _run_cycles(sched, tid, 3)
    port = _ports(db_path, tid)[("udp", 53)]
    assert port["status"] == "unknown" and "unavailable" in port["detail"]
    assert _values(tmp_path, tid, "udp_up_53") == [] and _values(tmp_path, tid, "udp_up") == []
    assert _status(db_path, tid) == TargetStatus.UNKNOWN
    assert _alert_ids(db_path) == []
    # the worker comes back: the next due scan measures again
    _fake_scan(monkeypatch, _scan_result({53: "open"}))
    sched._rt[tid].udp_last_started = utcnow() - timedelta(seconds=400)
    _run_cycles(sched, tid, 2)
    assert _status(db_path, tid) == TargetStatus.UP


def _edit_target(db_path: Path, tid: int, patch: dict) -> None:
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.update_target(conn, target_id=tid, patch=patch)
    finally:
        sqlite_db.close_connection(conn)


@pytest.mark.parametrize("cancel_on_change", [True, False], ids=["task-cancelled", "late-guard-only"])
@pytest.mark.parametrize(
    "patch",
    [{"host": "other.example"}, {"enable_udp_scan": False}, {"udp_ports": "53,123"}],
    ids=["host-changed", "udp-disabled", "ports-changed"],
)
def test_udp_scan_finishing_after_a_target_change_is_discarded(db_path, tmp_path, monkeypatch, patch, cancel_on_change):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    if not cancel_on_change:
        # Isolate the completion guard from the cancellation done on refresh
        monkeypatch.setattr(MonitorScheduler, "_drop_udp_scan", staticmethod(lambda _rt: None))

    async def scenario():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return _scan_result({53: "open"})

        _fake_scan(monkeypatch, slow)
        await sched._run_target(tid, utcnow())
        rt = sched._rt[tid]
        task = rt.udp_task
        assert task is not None and not task.done()
        _edit_target(db_path, tid, patch)
        sched._refresh_targets(utcnow())
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        assert rt.udp_result == {} and rt.udp_result_at is None  # the old result is never adopted
        assert _ports(db_path, tid)[("udp", 53)]["status"] != "up"

    asyncio.run(scenario())


def test_scanner_timeout_path_ends_the_slot_and_reports_unknown(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())

    async def hang(*_a, **_k):
        await asyncio.sleep(60)

    monkeypatch.setattr(scheduler, "UDP_HARD_TIMEOUT_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(scheduler.checks, "UDP_SCAN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(scheduler.checks, "udp_scan_ports", hang)
    _run_cycles(sched, tid, 2)
    assert _ports(db_path, tid)[("udp", 53)]["status"] == "unknown"
    assert sched._rt[tid].udp_task is None  # the scan slot is free again
    assert _status(db_path, tid) == TargetStatus.UNKNOWN


def test_slow_scan_never_blocks_the_cycle_and_overlapping_scans_are_skipped(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    started: list[int] = []
    release = asyncio.Event

    async def scenario():
        gate = release()

        async def slow():
            started.append(1)
            await gate.wait()
            return _scan_result({53: "open"})

        _fake_scan(monkeypatch, slow)
        await sched._run_target(tid, utcnow())  # returns although the scan is still running
        rt = sched._rt[tid]
        assert rt.udp_task is not None and not rt.udp_task.done()
        rt.udp_last_started = utcnow() - timedelta(seconds=400)  # the next UDP slot is due, the scan is not over
        await sched._run_target(tid, utcnow())
        await sched._run_target(tid, utcnow())
        assert rt.udp_skipped_cycles == 2 and len(started) == 1  # skipped and counted, never overlapping
        assert _status(db_path, tid) == TargetStatus.UNKNOWN  # no fresh result: neither up nor down
        gate.set()
        await rt.udp_task
        await sched._run_target(tid, utcnow())
        assert _status(db_path, tid) == TargetStatus.UP  # the late scan is still adopted

    asyncio.run(scenario())


def test_late_result_after_a_port_change_is_not_adopted(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())

    async def scenario():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return _scan_result({53: "closed"})

        _fake_scan(monkeypatch, slow)
        await sched._run_target(tid, utcnow())
        sched._targets[tid]["udp_ports"] = [53, 123]
        gate.set()
        await sched._rt[tid].udp_task
        assert sched._rt[tid].udp_result == {}

    asyncio.run(scenario())


def test_stale_result_turns_unknown_and_is_never_down(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_confirmed_scans(sched, tid)
    assert _status(db_path, tid) == TargetStatus.DOWN
    rt = sched._rt[tid]
    rt.udp_result_at = utcnow() - timedelta(seconds=1000)  # older than two UDP intervals
    rt.udp_last_started = utcnow()  # not due: no new scan, so the old result stays stale
    asyncio.run(sched._run_target(tid, utcnow()))
    assert _status(db_path, tid) == TargetStatus.UNKNOWN


def test_scheduler_restart_reuses_a_fresh_persisted_result_and_ignores_a_stale_one(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    conn = sqlite_db.connect(db_path)
    sqlite_db.update_network_port_status(conn, tid, "udp", 53, "down", None, raw_state="closed")
    conn.commit()
    sqlite_db.close_connection(conn)
    calls = _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    restarted = _sched(db_path, tmp_path)
    restarted._refresh_targets(utcnow())
    asyncio.run(restarted._run_target(tid, utcnow()))
    assert calls == []  # the persisted result is fresh: no scan right after the restart
    assert _status(db_path, tid) == TargetStatus.DOWN
    conn = sqlite_db.connect(db_path)
    conn.execute("UPDATE target_network_ports SET checked_at = ?", ((utcnow() - timedelta(hours=2)).isoformat(),))
    conn.commit()
    sqlite_db.close_connection(conn)
    later = _sched(db_path, tmp_path)
    later._refresh_targets(utcnow())
    _run_cycles(later, tid, 1)
    assert len(calls) == 1  # stale: scanned right away
    assert _status(db_path, tid) == TargetStatus.UNKNOWN  # until the new result is in


def test_tcp_down_wins_over_udp_unknown(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_tcp_connect=True, tcp_ports="25", enable_udp_scan=True, udp_ports="161")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({161: "filtered"}))
    with mock.patch.object(scheduler.checks, "tcp_connect", _tcp_connect({25: (False, None, "connection refused")})):
        _run_cycles(sched, tid, 2)
    assert _status(db_path, tid) == TargetStatus.DOWN


# ── Ternary UDP aggregate: UP / DOWN / UNKNOWN across ports and checks ────────


@pytest.mark.parametrize(
    ("states", "expected_udp_up", "expected_status"),
    [
        ({53: "open", 161: "filtered"}, [], TargetStatus.UNKNOWN),
        ({53: "open", 123: "open"}, [1], TargetStatus.UP),
        ({161: "filtered", 500: "open|filtered"}, [], TargetStatus.UNKNOWN),
    ],
    ids=["up+unknown", "up+up", "unknown+unknown"],
)
def test_udp_aggregate_is_written_only_when_known(
    db_path, tmp_path, monkeypatch, states, expected_udp_up, expected_status
):
    tid = _target(db_path, enable_udp_scan=True, udp_ports=",".join(str(p) for p in states))
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result(states))
    _run_cycles(sched, tid, 2)
    assert _values(tmp_path, tid, "udp_up")[:1] == expected_udp_up
    assert _status(db_path, tid) == expected_status


def test_udp_up_and_confirmed_down_aggregate_to_down(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53,123")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "open", 123: "closed"}))
    _run_confirmed_scans(sched, tid)
    assert _values(tmp_path, tid, "udp_up")[-1] == 0
    assert _status(db_path, tid) == TargetStatus.DOWN


def test_quality_gate_unknown_overrides_a_confirmed_udp_down_without_new_points(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_confirmed_scans(sched, tid)
    assert _status(db_path, tid) == TargetStatus.DOWN
    before = _values(tmp_path, tid, "udp_up")
    asyncio.run(sched._handle_connectivity_issue(tid, utcnow(), None))
    assert _status(db_path, tid) == TargetStatus.UNKNOWN
    assert _values(tmp_path, tid, "udp_up") == before  # a skipped cycle invents neither downtime nor uptime


_ENABLED = {"enable_http_check": True, "enable_ping": True, "enable_tcp_connect": True, "enable_udp_scan": True}
_UP, _DOWN, _UNK = 1, 0, None


@pytest.mark.parametrize(
    ("http", "ping", "tcp", "udp", "expected"),
    [
        (_UP, _UP, _UP, _UP, TargetStatus.UP),
        (_UP, _UP, _UP, _UNK, TargetStatus.UNKNOWN),
        (_UP, _UP, _UP, _DOWN, TargetStatus.DOWN),
        (_UNK, _UP, _UP, _UP, TargetStatus.UNKNOWN),
        (_UNK, _UP, _UP, _UNK, TargetStatus.UNKNOWN),
        # A confirmed outage wins over any unknown result, whichever check reports it.
        (_UNK, _UP, _UP, _DOWN, TargetStatus.DOWN),
        (_UP, _UNK, _UP, _DOWN, TargetStatus.DOWN),
        (_UP, _UP, _UNK, _DOWN, TargetStatus.DOWN),
        (_UNK, _UP, _DOWN, _UNK, TargetStatus.DOWN),
        (_UP, _UNK, _DOWN, _UP, TargetStatus.DOWN),
        (_DOWN, _UNK, _UP, _UNK, TargetStatus.DOWN),
        (_UP, _DOWN, _UNK, _UP, TargetStatus.DOWN),
        (_DOWN, _DOWN, _DOWN, _DOWN, TargetStatus.DOWN),
    ],
)
def test_overall_status_confirmed_down_wins_over_unknown(http, ping, tcp, udp, expected):
    cfg = {**_ENABLED, "udp_ports": [53]}
    snapshot = {"http_up": http, "ping_up": ping, "tcp_up": tcp, "udp_up": udp}
    assert MonitorScheduler._overall_status(cfg, snapshot) == expected


def test_overall_status_without_enabled_checks_is_disabled():
    cfg = {"enable_http_check": False, "enable_ping": False, "enable_tcp_connect": False, "enable_udp_scan": False}
    assert MonitorScheduler._overall_status(cfg, {}) == TargetStatus.DISABLED


# ── Service identity ──────────────────────────────────────────────────────────


def _redetect(db_path, tmp_path, monkeypatch, service):
    tid = _target(db_path, enable_tcp_connect=True, tcp_ports="25")
    conn = sqlite_db.connect(db_path)
    sqlite_db.sync_network_ports(
        conn, tid, tcp_ports=[25], udp_ports=[], scan_result=_scan_result({25: "open"}, _svc(), tcp=True)
    )
    sqlite_db.update_network_port_status(conn, tid, "tcp", 25, "up", None, raw_state="open")
    conn.commit()
    sqlite_db.close_connection(conn)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    calls = _fake_scan(monkeypatch, _scan_result({25: "open"}, service, tcp=True))
    preview = asyncio.run(sched.preview_service_detection(tid, owner_id=1))
    return tid, sched, preview, calls


def test_changed_version_after_an_update_changes_neither_status_nor_alerts(db_path, tmp_path, monkeypatch):
    tid, sched, preview, calls = _redetect(db_path, tmp_path, monkeypatch, _svc(version="3.9"))
    assert preview["changes"][0]["kind"] == "version_changed"
    assert _ports(db_path, tid)[("tcp", 25)]["service_version"] == "3.8"
    sched.confirm_service_detection(tid, 1, preview["token"])
    port = _ports(db_path, tid)[("tcp", 25)]
    assert calls[0]["detect_services"] is True
    assert port["status"] == "up" and port["service_identity"] == "identified"
    assert port["service_version"] == "3.9" and "version changed" in port["detail"]
    assert _alert_ids(db_path) == []


def test_confirmed_service_change_adopts_the_new_identity_without_a_port_outage(db_path, tmp_path, monkeypatch):
    tid, sched, preview, _calls = _redetect(
        db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx", version="1")
    )
    assert preview["changes"][0]["kind"] == "service_changed"
    declined = _ports(db_path, tid)[("tcp", 25)]  # a preview alone stores nothing
    assert declined["service_name"] == "smtp" and declined["service_identity"] == "identified"
    assert declined["identity_changed_at"] is None
    sched.confirm_service_detection(tid, 1, preview["token"])
    port = _ports(db_path, tid)[("tcp", 25)]
    assert port["service_identity"] == "identified"
    assert port["status"] == "up"  # identity is not availability
    assert port["service_name"] == "http"
    assert _alert_ids(db_path) == []


def test_failed_redetection_changes_nothing(db_path, tmp_path, monkeypatch):
    tid, _sched_instance, _preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc())
    _fake_scan(monkeypatch, NmapScannerError("unavailable", "down"))
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    with pytest.raises(NmapScannerError):
        asyncio.run(sched.preview_service_detection(tid, owner_id=1))
    assert _ports(db_path, tid)[("tcp", 25)]["service_identity"] == "identified"


def test_service_detection_confirmation_is_owner_bound_and_single_use(db_path, tmp_path, monkeypatch):
    tid, sched, preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))
    with pytest.raises(LookupError):
        sched.confirm_service_detection(tid, 2, preview["token"])
    assert _ports(db_path, tid)[("tcp", 25)]["service_name"] == "smtp"
    sched.confirm_service_detection(tid, 1, preview["token"])
    with pytest.raises(LookupError):
        sched.confirm_service_detection(tid, 1, preview["token"])


def test_service_detection_rejects_stale_target_before_confirmation(db_path, tmp_path, monkeypatch):
    tid, sched, preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))
    conn = sqlite_db.connect(db_path)
    conn.execute("UPDATE targets SET updated_at = '2099-01-01T00:00:00' WHERE id = ?", (tid,))
    conn.commit()
    sqlite_db.close_connection(conn)
    with pytest.raises(ValueError, match="Target changed"):
        sched.confirm_service_detection(tid, 1, preview["token"])
    assert _ports(db_path, tid)[("tcp", 25)]["service_name"] == "smtp"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("service_name", "http"),
        ("service_product", "Exim"),
        ("service_version", "9.9"),
        ("service_identity", "unknown"),
    ],
)
def test_confirmation_fails_when_any_baseline_column_changed_after_the_preview(
    db_path, tmp_path, monkeypatch, column, value
):
    tid, sched, preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))
    conn = sqlite_db.connect(db_path)
    conn.execute(f"UPDATE target_network_ports SET {column} = ? WHERE target_id = ?", (value, tid))  # noqa: S608
    conn.commit()
    sqlite_db.close_connection(conn)
    with pytest.raises(ValueError, match="Service metadata changed"):
        sched.confirm_service_detection(tid, 1, preview["token"])
    assert _ports(db_path, tid)[("tcp", 25)][column] == value


def test_expired_service_detection_preview_cannot_be_confirmed(db_path, tmp_path, monkeypatch):
    tid, sched, preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))
    monkeypatch.setattr(scheduler, "utcnow", lambda: utcnow() + timedelta(minutes=5))
    with pytest.raises(ValueError, match="expired"):
        sched.confirm_service_detection(tid, 1, preview["token"])
    assert _ports(db_path, tid)[("tcp", 25)]["service_name"] == "smtp"


def test_the_65th_service_detection_preview_evicts_the_oldest(db_path, tmp_path, monkeypatch):
    tid, sched, first, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))
    tokens = [first["token"]]
    tokens.extend(asyncio.run(sched.preview_service_detection(tid, owner_id=1))["token"] for _ in range(64))
    with pytest.raises(LookupError):
        sched.confirm_service_detection(tid, 1, tokens[0])
    sched.confirm_service_detection(tid, 1, tokens[-1])
    assert _ports(db_path, tid)[("tcp", 25)]["service_name"] == "http"


def test_udp_detection_uses_central_host_when_tcp_host_differs(db_path, tmp_path, monkeypatch):
    tid = _target(
        db_path,
        host="udp.example",
        tcp_host="tcp.example",
        enable_tcp_connect=True,
        tcp_ports="25",
        enable_udp_scan=True,
        udp_ports="53",
    )
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    calls = []

    async def fake_request_scan(host, **kwargs):
        calls.append((host, kwargs))
        if kwargs.get("tcp_ports"):
            return _scan_result({25: "open"}, _svc(), tcp=True)
        return _scan_result({53: "open"}, _svc(), tcp=False)

    monkeypatch.setattr(nmap_client, "request_scan", fake_request_scan)
    preview = asyncio.run(sched.preview_service_detection(tid, owner_id=1))
    assert [(host, kwargs.get("tcp_ports"), kwargs.get("udp_ports")) for host, kwargs in calls] == [
        ("tcp.example", [25], None),
        ("udp.example", None, [53]),
    ]
    assert {change["protocol"] for change in preview["changes"]} == {"tcp", "udp"}
    assert sched._udp_scan_target(sched._targets[tid]) == ("udp.example", True)

    scheduled_hosts = []

    async def fake_udp_scan(_target_id, host, _ports, _interval, _rt, _generation):
        scheduled_hosts.append(host)

    monkeypatch.setattr(sched, "_run_udp_scan", fake_udp_scan)

    async def cycle():
        rt = TargetRuntime(next_due=utcnow(), udp_seeded=True)
        await sched._udp_cycle(tid, sched._targets[tid], rt, utcnow(), None)
        await rt.udp_task

    asyncio.run(cycle())
    assert scheduled_hosts == ["udp.example"]


@pytest.mark.parametrize(
    ("interval", "elapsed", "due"),
    [
        (200, 200, False),  # below the 300 s UDP minimum the scan never starts one target cycle later
        (200, 299.9, False),
        (200, 300, True),
        (60, 120, False),
        (60, 300, True),
        (300, 149, False),  # at or above the minimum the target cycle is the cadence
        (300, 299, True),  # timing drift must not skip a cycle (would make the result stale)
        (600, 450, True),
    ],
)
def test_udp_scan_is_not_due_before_the_udp_minimum_interval(db_path, tmp_path, monkeypatch, interval, elapsed, due):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53", interval_seconds=interval)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    started = []

    async def fake_udp_scan(*args):
        started.append(args[1])

    monkeypatch.setattr(sched, "_run_udp_scan", fake_udp_scan)

    async def cycle():
        now = utcnow()
        rt = TargetRuntime(next_due=now, udp_seeded=True, udp_last_started=now - timedelta(seconds=elapsed))
        await sched._udp_cycle(tid, sched._targets[tid], rt, now, None)
        if rt.udp_task is not None:
            await rt.udp_task

    asyncio.run(cycle())
    assert bool(started) is due


# ── Alerts ─────────────────────────────────────────────────────────────────────


def test_confirmed_udp_closed_alerts_only_after_the_timeout_and_names_the_endpoint(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53,123")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "open", 123: "closed"}))
    _run_confirmed_scans(sched, tid)
    rt = sched._rt[tid]
    assert rt.down_since_by_service["udp"] is not None and _alert_ids(db_path) == []  # still being confirmed
    rt.down_since_by_service["udp"] = utcnow() - timedelta(minutes=10)
    asyncio.run(sched._run_target(tid, utcnow()))
    assert _alert_ids(db_path) == ["udp:123"]  # per-endpoint cause, UDP:53 is up and not named


def test_single_closed_scan_is_pending_not_down_and_never_alerts(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_cycles(sched, tid, 4)  # one scan, its result is seen by several cycles
    rt = sched._rt[tid]
    assert _values(tmp_path, tid, "udp_up_53") == [] and _values(tmp_path, tid, "udp_up") == []
    assert _values(tmp_path, tid, "udp_unknown_53") == [1]  # exactly one point for one scan
    assert _status(db_path, tid) == TargetStatus.UNKNOWN
    assert rt.down_since_by_service["udp"] is None and _alert_ids(db_path) == []
    port = _ports(db_path, tid)[("udp", 53)]
    assert port["status"] == "unknown" and port["raw_state"] == "closed"


def test_two_consecutive_closed_scans_confirm_down_with_one_point_each_scan(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_confirmed_scans(sched, tid)
    assert _values(tmp_path, tid, "udp_up_53") == [0] and _status(db_path, tid) == TargetStatus.DOWN
    asyncio.run(sched._run_target(tid, utcnow()))  # the cached result is not re-emitted as a measurement
    assert _values(tmp_path, tid, "udp_up_53") == [0]


def test_closed_then_open_filtered_resets_the_confirmation(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_confirmed_scans(sched, tid, 1)
    _fake_scan(monkeypatch, _scan_result({53: "open|filtered"}))
    _run_confirmed_scans(sched, tid, 1)
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_confirmed_scans(sched, tid, 1)
    assert _values(tmp_path, tid, "udp_up_53") == []  # closed, unclear, closed: never two in a row
    assert _status(db_path, tid) == TargetStatus.UNKNOWN and _alert_ids(db_path) == []


def test_unknown_never_alerts_and_restarts_the_confirmation(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_cycles(sched, tid, 2)
    rt = sched._rt[tid]
    rt.down_since_by_service["udp"] = utcnow() - timedelta(minutes=10)
    rt.udp_result = {53: checks.PortCheck("udp", 53, "unknown", None, "scan timeout")}  # single UDP timeout
    asyncio.run(sched._run_target(tid, utcnow()))
    assert rt.down_since_by_service["udp"] is None and _alert_ids(db_path) == []
    assert _status(db_path, tid) == TargetStatus.UNKNOWN


def test_recovery_resolves_the_udp_alert_and_unknown_does_not(db_path, tmp_path, monkeypatch):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    _fake_scan(monkeypatch, _scan_result({53: "closed"}))
    _run_confirmed_scans(sched, tid)
    rt = sched._rt[tid]
    rt.down_since_by_service["udp"] = utcnow() - timedelta(minutes=10)
    asyncio.run(sched._run_target(tid, utcnow()))
    assert rt.current_alert_id
    rt.udp_result = {53: checks.PortCheck("udp", 53, "unknown", "filtered", None)}
    asyncio.run(sched._run_target(tid, utcnow()))
    assert rt.current_alert_id  # UNKNOWN is not a recovery
    rt.udp_result = {53: checks.PortCheck("udp", 53, "up", "open", None)}
    asyncio.run(sched._run_target(tid, utcnow()))
    assert rt.current_alert_id is None
    conn = sqlite_db.connect(db_path)
    try:
        assert [r["status"] for r in conn.execute("SELECT status FROM alerts")] == ["resolved"]
    finally:
        sqlite_db.close_connection(conn)


def test_restored_alert_with_endpoint_tokens_marks_the_service_down_again():
    cfg = {"enable_udp_scan": True, "udp_ports": [123], "enable_tcp_connect": True, "enable_ping": True}
    assert scheduler._restorable_services(cfg, "udp:123, tcp:25") == ["tcp", "udp"]
    assert scheduler._restorable_services(cfg, "udp:123") == ["udp"]


def test_restore_falls_back_to_the_enabled_checks_when_the_failed_service_is_disabled():
    cfg = {"enable_ping": True, "enable_tcp_connect": False}
    assert scheduler._restorable_services(cfg, "tcp:25") == ["ping"]


_SINGLE_CHECKS = [
    ("enable_http_check", "http_up", {}),
    ("enable_ping", "ping_up", {}),
    ("enable_tcp_connect", "tcp_up", {}),
    ("enable_udp_scan", "udp_up", {"udp_ports": [53]}),
]


@pytest.mark.parametrize(("flag", "metric", "extra"), _SINGLE_CHECKS, ids=[c[1] for c in _SINGLE_CHECKS])
def test_each_availability_check_alone_drives_status_metrics_and_restore(flag, metric, extra):
    from app.utils.uptime_metric import enabled_availability_metrics, select_uptime_metric

    cfg = {flag: True, **extra}
    service = metric.removesuffix("_up")
    assert enabled_availability_metrics(cfg) == (metric,)
    assert select_uptime_metric(cfg) == metric
    assert scheduler._restorable_services(cfg, "") == [service]
    assert scheduler.MonitorScheduler._overall_status(cfg, {metric: 1}) == scheduler.TargetStatus.UP
    assert scheduler.MonitorScheduler._overall_status(cfg, {metric: 0}) == scheduler.TargetStatus.DOWN
    assert scheduler.MonitorScheduler._overall_status(cfg, {metric: None}) == scheduler.TargetStatus.UNKNOWN


@pytest.mark.parametrize("ports", [None, "", []])
def test_udp_scan_without_ports_counts_nowhere(ports):
    from app.utils.uptime_metric import enabled_availability_metrics, select_uptime_metric

    cfg = {"enable_udp_scan": True, "udp_ports": ports, "enable_ping": True}
    assert enabled_availability_metrics(cfg) == ("ping_up",)
    assert select_uptime_metric({"enable_udp_scan": True, "udp_ports": ports}) is None
    assert scheduler._restorable_services(cfg, "udp:123") == ["ping"]  # udp is not a restorable service
    assert scheduler.MonitorScheduler._overall_status(cfg, {"ping_up": 1, "udp_up": 0}) == scheduler.TargetStatus.UP
    only_udp = {"enable_udp_scan": True, "udp_ports": ports}
    assert scheduler.MonitorScheduler._overall_status(only_udp, {"udp_up": 0}) == scheduler.TargetStatus.DISABLED


def test_port_status_constants_match_the_contract():
    assert (scheduler.PortStatus.UP, scheduler.PortStatus.DOWN, scheduler.PortStatus.UNKNOWN) == (
        "up",
        "down",
        "unknown",
    )
    assert (checks.SERVICE_IDENTIFIED, checks.SERVICE_UNKNOWN, checks.SERVICE_CHANGED) == (
        "identified",
        "unknown",
        "changed",
    )


@pytest.mark.parametrize("change", ["host", "disable_udp"])
def test_running_udp_scan_is_dropped_when_the_target_host_or_udp_scan_changes(db_path, tmp_path, monkeypatch, change):
    tid = _target(db_path, enable_udp_scan=True, udp_ports="53")
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())

    async def scenario():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return _scan_result({53: "open"})

        _fake_scan(monkeypatch, slow)
        await sched._run_target(tid, utcnow())
        rt = sched._rt[tid]
        task = rt.udp_task
        conn = sqlite_db.connect(db_path)
        column, value = ("host", "other.example") if change == "host" else ("enable_udp_scan", 0)
        conn.execute(f"UPDATE targets SET {column} = ? WHERE id = ?", (value, tid))  # noqa: S608
        conn.commit()
        sqlite_db.close_connection(conn)
        sched._refresh_targets(utcnow())
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        assert sched._rt[tid] is rt and rt.udp_result == {} and rt.udp_result_at is None
        assert _ports(db_path, tid)[("udp", 53)]["status"] != "up"

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["edited", "deleted"])
def test_redetection_result_is_discarded_when_the_target_changed_meanwhile(db_path, tmp_path, monkeypatch, change):
    tid = _target(db_path, enable_tcp_connect=True, tcp_ports="25")
    conn = sqlite_db.connect(db_path)
    sqlite_db.sync_network_ports(
        conn, tid, tcp_ports=[25], udp_ports=[], scan_result=_scan_result({25: "open"}, _svc(), tcp=True)
    )
    conn.commit()
    sqlite_db.close_connection(conn)
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    before = _ports(db_path, tid)[("tcp", 25)]["service_identity"]

    def change_target():
        conn = sqlite_db.connect(db_path)
        if change == "edited":
            conn.execute(
                "UPDATE targets SET host = 'other.example', updated_at = '2099-01-01T00:00:00' WHERE id = ?", (tid,)
            )
        else:
            conn.execute("DELETE FROM targets WHERE id = ?", (tid,))
        conn.commit()
        sqlite_db.close_connection(conn)
        return _scan_result({25: "open"}, _svc(name="http", product="nginx", version="1"), tcp=True)

    _fake_scan(monkeypatch, change_target)
    with pytest.raises(ValueError):
        asyncio.run(sched.preview_service_detection(tid, owner_id=1))
    if change == "edited":
        assert _ports(db_path, tid)[("tcp", 25)]["service_identity"] == before


# ── Service re-detection: trigger, guards and races ───────────────────────────


def _tcp_and_udp_target(db_path):
    tid = _target(db_path, enable_tcp_connect=True, tcp_ports="25", enable_udp_scan=True, udp_ports="53")
    conn = sqlite_db.connect(db_path)
    sqlite_db.sync_network_ports(
        conn, tid, tcp_ports=[25], udp_ports=[53], scan_result=_scan_result({25: "open"}, _svc(), tcp=True)
    )
    conn.commit()
    sqlite_db.close_connection(conn)
    return tid


def test_periodic_cycles_never_redetect_services_or_flag_a_change(db_path, tmp_path, monkeypatch):
    tid = _tcp_and_udp_target(db_path)
    before = _ports(db_path, tid)[("tcp", 25)]
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    changed = _svc(name="http", product="nginx", version="1")
    calls = _fake_scan(monkeypatch, _scan_result({53: "open"}, changed))

    async def forbidden(*args, **kwargs):
        raise AssertionError("periodic monitoring must not start a service detection")

    monkeypatch.setattr(sched, "preview_service_detection", forbidden)
    monkeypatch.setattr(sched, "confirm_service_detection", forbidden)
    with mock.patch.object(scheduler.checks, "tcp_connect", _tcp_connect({25: (True, 1.0, None)})):
        _run_confirmed_scans(sched, tid, scans=3)
    after = _ports(db_path, tid)[("tcp", 25)]
    assert calls and all(call["detect_services"] is False for call in calls)
    for column in ("service_name", "service_product", "service_version", "service_identity", "identity_changed_at"):
        assert after[column] == before[column], column
    assert sched._service_previews == {}


def test_preview_of_an_unknown_target_is_a_lookup_error_and_scans_nothing(db_path, tmp_path, monkeypatch):
    sched = _sched(db_path, tmp_path)
    sched._refresh_targets(utcnow())
    calls = _fake_scan(monkeypatch, _scan_result({25: "open"}, _svc(), tcp=True))
    with pytest.raises(LookupError):
        asyncio.run(sched.preview_service_detection(4242, owner_id=1))
    assert calls == []


def test_preview_without_scan_findings_issues_no_token_and_stores_nothing(db_path, tmp_path, monkeypatch):
    tid, sched, _preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc())
    before = _ports(db_path, tid)[("tcp", 25)]
    _fake_scan(monkeypatch, NmapScanResult(hosts=[], started_at=utcnow(), completed_at=utcnow(), complete=True))
    result = asyncio.run(sched.preview_service_detection(tid, owner_id=1))
    assert result == {"token": None, "changes": []}
    assert sched._service_previews == {}
    assert _ports(db_path, tid)[("tcp", 25)] == before
    with pytest.raises(LookupError):
        sched.confirm_service_detection(tid, 1, "0" * 32)


def test_confirmation_for_another_target_is_refused_and_keeps_the_preview(db_path, tmp_path, monkeypatch):
    tid, sched, preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))
    with pytest.raises(LookupError):
        sched.confirm_service_detection(tid + 1, 1, preview["token"])
    assert _ports(db_path, tid)[("tcp", 25)]["service_name"] == "smtp"


def test_concurrent_confirmations_apply_exactly_once(db_path, tmp_path, monkeypatch):
    import threading

    tid, sched, preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def confirm():
        barrier.wait()
        try:
            sched.confirm_service_detection(tid, 1, preview["token"])
            outcomes.append("applied")
        except LookupError:
            outcomes.append("refused")

    threads = [threading.Thread(target=confirm) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["applied"] + ["refused"] * 5
    assert _ports(db_path, tid)[("tcp", 25)]["service_name"] == "http"


def test_a_second_preview_of_the_same_target_is_refused_while_one_runs(db_path, tmp_path, monkeypatch):
    tid, sched, _preview, _calls = _redetect(db_path, tmp_path, monkeypatch, _svc(name="http", product="nginx"))

    async def scenario():
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow():
            started.set()
            await gate.wait()
            return _scan_result({25: "open"}, _svc(name="http", product="nginx"), tcp=True)

        calls = _fake_scan(monkeypatch, slow)
        first = asyncio.create_task(sched.preview_service_detection(tid, owner_id=1))
        await started.wait()
        with pytest.raises(ValueError, match="already running"):
            await asyncio.wait_for(sched.preview_service_detection(tid, owner_id=2), timeout=2)
        assert len(calls) == 1
        gate.set()
        assert (await first)["token"]
        # The guard is released afterwards, also after a failed scan.
        _fake_scan(monkeypatch, NmapScannerError("unavailable", "down"))
        with pytest.raises(NmapScannerError):
            await sched.preview_service_detection(tid, owner_id=1)
        _fake_scan(monkeypatch, _scan_result({25: "open"}, _svc(name="http", product="nginx"), tcp=True))
        assert (await sched.preview_service_detection(tid, owner_id=1))["token"]

    asyncio.run(scenario())
