#!/usr/bin/env python3
#
# tests/test_quality_gate.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001
"""Quality gate: 2/3 reachability rule precedes the score decision."""

from __future__ import annotations

import asyncio
import time
from unittest import mock

import pytest

from app.api import quality as quality_api
from app.monitor import quality_probe, scheduler
from app.monitor.scheduler import MonitorScheduler, QualityState, TargetRuntime
from app.utils.constants import QUALITY_MAX_TARGETS
from app.utils.time import utcnow
from tests import _support  # noqa: F401


def _run_probe(rtts: list[float | None], total: int, *, min_score: int = 70, threshold: int = 1000):
    targets = [f"https://t{i}.example" for i in range(total)]
    it = iter(rtts)

    async def fake_measure(client, target, sem):
        return target, next(it)

    with (
        mock.patch.object(quality_probe, "get_quality_targets", return_value=targets),
        mock.patch.object(quality_probe, "get_min_score", return_value=min_score),
        mock.patch.object(quality_probe, "get_rtt_threshold", return_value=threshold),
        mock.patch.object(quality_probe, "_measure_target_rtt", side_effect=fake_measure),
    ):
        return asyncio.run(quality_probe.run_quality_probe())


@pytest.fixture(autouse=True)
def _reset_ema():
    quality_probe._reset_score_state()
    yield
    quality_probe._reset_score_state()


def _seed_score(score: float):
    quality_probe._last_score = score


def test_quality_current_keeps_targets_and_adds_country_details(monkeypatch):
    targets = ["https://1.1.1.1:443/path", "2001:4860:4860::8888", "example.org", "8.8.8.8"]
    monkeypatch.setattr(quality_api, "get_quality_targets", lambda: targets)
    monkeypatch.setattr(quality_api.sqlite_db, "get_monitor_quality_latest", lambda _conn: None)
    monkeypatch.setattr(quality_api, "geolocate_country", {"1.1.1.1": "DE", "203.0.113.1": "FR"}.get)
    monkeypatch.setattr(
        quality_api.socket, "getaddrinfo", lambda _host, _port: [(None, None, None, None, ("203.0.113.1", 0))]
    )
    quality_api._target_country.cache_clear()
    result = quality_api.get_quality_current(conn=object(), _user={})
    assert result["targets"] == targets
    assert result["target_details"] == [
        {"target": targets[0], "country_code": "DE"},
        {"target": targets[1], "country_code": None},
        {"target": targets[2], "country_code": "FR"},
        {"target": targets[3], "country_code": None},
    ]


def test_quality_country_resolution_is_cached_and_dns_failure_is_neutral(monkeypatch):
    calls = []

    def resolve(host, _port):
        calls.append(host)
        if host == "bad.example":
            raise OSError("DNS unavailable")
        return [(None, None, None, None, ("1.2.3.4", 0))]

    monkeypatch.setattr(quality_api.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(quality_api, "geolocate_country", lambda _ip: "US")
    quality_api._target_country.cache_clear()
    assert quality_api._target_country("example.org", 1) == "US"
    assert quality_api._target_country("example.org", 1) == "US"
    assert quality_api._target_country("bad.example", 1) is None
    assert calls == ["example.org", "bad.example"]


@pytest.mark.parametrize(
    ("succeeded", "total", "seed", "expected"),
    [
        (2, 5, 100.0, "down"),  # below 2/3, any score
        (2, 5, 0.0, "down"),
        (3, 5, 100.0, "down"),  # 3/5 < 2/3 even with high score
        (4, 5, 0.0, "degraded"),  # score below threshold but >= 2/3
        (4, 5, 100.0, "ok"),
        (5, 5, 100.0, "ok"),
    ],
)
def test_state_table(succeeded, total, seed, expected):
    _seed_score(seed)
    rtts = [10.0] * succeeded + [None] * (total - succeeded)
    # Fast RTT => high raw score, so the seeded EMA decides low/high scores.
    result = _run_probe(rtts, total)
    assert result.state == expected


def test_three_of_five_with_score_76_is_down():
    # raw = (0.6*0.6 + 0.4*1.0)*100 ~= 76 -> must still be down (3*3 < 5*2)
    result = _run_probe([1.0, 1.0, 1.0, None, None], 5)
    assert result.score is not None and 75 <= result.score <= 77
    assert result.state == "down"


def test_minimum_success_check_still_returns_no_score():
    result = _run_probe([5.0, None, None, None, None, None], 6)
    assert result.state == "down" and result.score is None


def test_transitions_with_ema():
    states = []
    # ok -> (4/5 fast drop) degraded -> down -> recovery
    states.append(_run_probe([1.0] * 5, 5).state)  # raw ~100
    states.append(_run_probe([900.0] * 4 + [None], 5, min_score=95).state)  # 4/5, low score
    states.append(_run_probe([1.0] * 2 + [None] * 3, 5).state)  # 2/5
    states.append(_run_probe([1.0] * 5, 5).state)  # recovered reach
    assert states[0] == "ok"
    assert states[1] == "degraded"
    assert states[2] == "down"
    assert states[3] in {"ok", "degraded"}  # slow EMA recovery may keep it degraded


@pytest.mark.parametrize("state", [QualityState.DEGRADED, QualityState.DOWN])
def test_scheduler_skips_checks_for_degraded_and_down(state):
    sched = MonitorScheduler(get_conn=mock.MagicMock(), tsdb_dir=None)
    sched._rt[1] = TargetRuntime(next_due=utcnow())
    calls = {"conn": 0, "run": 0}

    async def probe(now, generation=None):
        sched._quality_state = state

    async def conn_issue(tid, now, generation=None):
        calls["conn"] += 1

    async def run_target(tid, now, generation=None):
        calls["run"] += 1

    async def stop_after(now, generation=None):
        sched._stop.set()

    async def noop(now, generation=None):
        return None

    async def idle_sleep():
        return None

    async def go():
        with (
            mock.patch.object(sched, "_refresh_targets"),
            mock.patch.object(sched, "_run_quality_probe", probe),
            mock.patch.object(sched, "_handle_connectivity_issue", conn_issue),
            mock.patch.object(sched, "_run_target", run_target),
            mock.patch.object(sched, "_run_housekeeping", stop_after),
            mock.patch.object(sched, "_process_notification_spooler", noop),
            mock.patch.object(sched, "_idle_sleep", idle_sleep),
            mock.patch.object(scheduler.sqlite_db, "update_scheduler_heartbeat"),
        ):
            await sched._run_loop()

    asyncio.run(go())
    assert calls == {"conn": 1, "run": 0}


def test_probe_run_is_capped_in_time_and_counts_unfinished_targets_as_failed():
    targets = [f"https://fast{i}.example" for i in range(2)] + [f"https://slow{i}.example" for i in range(4)]

    async def fake_measure(client, target, sem):
        if "slow" in target:
            await asyncio.sleep(30)
        return target, 5.0

    with (
        mock.patch.object(quality_probe, "get_quality_targets", return_value=targets),
        mock.patch.object(quality_probe, "get_min_score", return_value=70),
        mock.patch.object(quality_probe, "get_rtt_threshold", return_value=1000),
        mock.patch.object(quality_probe, "_measure_target_rtt", side_effect=fake_measure),
        mock.patch.object(quality_probe, "PROBE_TOTAL_TIMEOUT_SECONDS", 0.2),
    ):
        started = time.monotonic()
        result = asyncio.run(quality_probe.run_quality_probe())
    assert time.monotonic() - started < 5
    assert result.state == "down"
    assert result.success_ratio == pytest.approx(2 / 6, abs=0.01)


def test_stored_target_list_above_the_limit_is_truncated_with_a_warning(caplog):
    stored = ",".join(f"host{i}.example" for i in range(QUALITY_MAX_TARGETS + 5))
    with mock.patch.object(quality_probe, "get_runtime_setting", return_value=stored), caplog.at_level("WARNING"):
        targets = quality_probe.get_quality_targets()
    assert targets == [f"host{i}.example" for i in range(QUALITY_MAX_TARGETS)]
    assert "using the first" in caplog.text


def test_cancelled_ping_probe_leaves_no_child_process(tmp_path, monkeypatch):
    fake_ping = tmp_path / "ping"
    fake_ping.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_ping.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    started: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def scenario():
        started_event = asyncio.Event()

        async def recording_exec(*args, **kwargs):
            proc = await real_exec(*args, **kwargs)
            started.append(proc)
            started_event.set()
            return proc

        with mock.patch.object(asyncio, "create_subprocess_exec", recording_exec):
            task = asyncio.ensure_future(quality_probe._measure_ping_rtt("192.0.2.1"))
            try:
                await asyncio.wait_for(started_event.wait(), timeout=3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return started[0]

    proc = asyncio.run(scenario())
    assert proc.returncode is not None  # terminated and reaped, not left running


def test_cancelling_the_whole_probe_run_cancels_every_started_task():
    seen: list[asyncio.Task] = []
    all_started = asyncio.Event()

    async def hanging(client, target, sem):
        seen.append(asyncio.current_task())
        if len(seen) == 3:
            all_started.set()
        await asyncio.sleep(30)

    async def scenario():
        with (
            mock.patch.object(quality_probe, "get_quality_targets", return_value=["https://a.example"] * 3),
            mock.patch.object(quality_probe, "get_min_score", return_value=70),
            mock.patch.object(quality_probe, "get_rtt_threshold", return_value=1000),
            mock.patch.object(quality_probe, "_measure_target_rtt", side_effect=hanging),
        ):
            run = asyncio.ensure_future(quality_probe.run_quality_probe())
            try:
                await asyncio.wait_for(all_started.wait(), timeout=3)
                run.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run
            finally:
                if not run.done():
                    run.cancel()
                for task in seen:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(run, *seen, return_exceptions=True)

    asyncio.run(scenario())
    assert len(seen) == 3 and all(t.cancelled() for t in seen)
