#!/usr/bin/env python3
#
# tests/test_quality_gate.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001
"""Quality gate: 2/3 reachability rule precedes the score decision."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from app.monitor import quality_probe, scheduler
from app.monitor.scheduler import MonitorScheduler, QualityState, TargetRuntime
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
