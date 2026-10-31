#!/usr/bin/env python3
#
# tests/test_supervisor.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001 - white-box access to scheduler run-epoch internals

"""Service watchdog: restart with backoff, crash-loop protection, intended stops, log events."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from pathlib import Path

import pytest

from app.utils import maintenance
from app.utils.supervisor import ServiceSpec, ServiceState, ServiceSupervisor, SupervisionPolicy

LOGGER = "app.utils.supervisor"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _make(policy: SupervisionPolicy | None = None, *, on_sleep=None):
    clock = FakeClock()
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        clock.now += delay
        if on_sleep is not None:
            on_sleep(delay)
        await asyncio.sleep(0)

    policy = policy or SupervisionPolicy(jitter=0.0, check_interval=0.01)
    rng = random.Random(1)  # noqa: S311 - deterministic test jitter
    return ServiceSupervisor(policy, clock=clock, sleep=fake_sleep, rng=rng), clock, delays


async def _until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


def _events(caplog, name: str | None = None) -> list[str]:
    return [
        r.service_event
        for r in caplog.records
        if r.name == LOGGER and hasattr(r, "service_event") and (name is None or r.service == name)
    ]


@pytest.fixture(autouse=True)
def _no_maintenance():
    maintenance.end_maintenance()
    yield
    maintenance.end_maintenance()


def test_crash_restarts_with_exponential_backoff(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    async def scenario():
        sup, _clock, delays = _make()
        stop = asyncio.Event()
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise ValueError("boom")
            await stop.wait()

        sup.add(ServiceSpec("svc", flaky, request_stop=stop.set))
        await _until(lambda: calls == 4)
        assert sup.snapshot()["svc"]["crashes_total"] == 3
        assert sup.snapshot()["svc"]["last_exception_type"] == "ValueError"
        await sup.stop()
        return delays, sup

    delays, sup = asyncio.run(scenario())
    assert delays == [1.0, 2.0, 4.0]
    assert sup.snapshot()["svc"]["state"] == ServiceState.STOPPED
    crashed = [r for r in caplog.records if getattr(r, "service_event", "") == "crashed"]
    assert len(crashed) == 3
    assert all(r.exc_info and r.exc_info[0] is ValueError for r in crashed)  # traceback once per crash
    others = [r for r in caplog.records if r.name == LOGGER and getattr(r, "service_event", "") != "crashed"]
    assert all(r.exc_info is None for r in others)
    assert "boom" not in "".join(r.getMessage() for r in caplog.records)  # no exception text in the line
    assert _events(caplog)[:4] == ["started", "crashed", "restarting", "started"]


def test_unexpected_return_counts_as_crash(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    async def scenario():
        sup, _clock, _delays = _make()
        runs = 0

        async def returns():
            nonlocal runs
            runs += 1
            if runs > 1:
                await asyncio.Event().wait()

        sup.add(ServiceSpec("svc", returns))
        await _until(lambda: runs == 2)
        snap = sup.snapshot()["svc"]
        await sup.stop()
        return snap

    snap = asyncio.run(scenario())
    assert snap["last_exception_type"] == "ServiceExitedError"


def test_crash_loop_goes_degraded_without_tight_loop(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    policy = SupervisionPolicy(jitter=0.0, crash_limit=3, degraded_retry=300.0, check_interval=0.01)

    async def scenario():
        sup, _clock, delays = _make(policy)
        starts = 0
        seen: dict = {}

        async def always_fails():
            nonlocal starts
            starts += 1
            if starts == 6:
                seen["state"] = sup.snapshot()["svc"]["state"]
                seen["degraded"] = sup.is_degraded()
                sup.begin_shutdown()  # end the otherwise endless loop after the sixth crash
            raise RuntimeError("x")

        sup.add(ServiceSpec("svc", always_fails))
        await _until(lambda: starts == 6)
        await sup.stop()
        return delays, seen["state"], seen["degraded"]

    delays, state, degraded = asyncio.run(scenario())
    assert state == ServiceState.DEGRADED and degraded
    assert len(delays) == 5  # the sixth crash happened during shutdown: no further restart
    assert delays[:2] == [1.0, 2.0]
    assert all(d == 300.0 for d in delays[2:5])  # slow retry from the third crash on
    degraded_logs = [r for r in caplog.records if getattr(r, "service_event", "") == "degraded"]
    assert len(degraded_logs) == 1 and degraded_logs[0].levelno == logging.ERROR


def test_service_recovers_after_stable_uptime(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    policy = SupervisionPolicy(jitter=0.0, stable_after=60.0, check_interval=0.01)

    async def scenario():
        sup, clock, _delays = _make(policy)
        stop = asyncio.Event()
        calls = 0

        async def once_bad():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("x")
            await stop.wait()

        sup.add(ServiceSpec("svc", once_bad, request_stop=stop.set))
        await _until(lambda: calls == 2)
        clock.now += 61
        await _until(lambda: "recovered" in _events(caplog))
        state = sup.snapshot()["svc"]["state"]
        await sup.stop()
        return state

    assert asyncio.run(scenario()) == ServiceState.RUNNING


def test_clean_shutdown_is_not_a_crash_and_does_not_restart(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    async def scenario():
        sup, _clock, delays = _make()
        stop = asyncio.Event()
        starts = 0

        async def loop():
            nonlocal starts
            starts += 1
            await stop.wait()

        sup.add(ServiceSpec("svc", loop, request_stop=stop.set))
        await _until(lambda: starts == 1)
        sup.begin_shutdown()
        await sup.stop()
        tasks_left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return starts, delays, tasks_left

    starts, delays, tasks_left = asyncio.run(scenario())
    assert starts == 1 and delays == []
    assert "crashed" not in _events(caplog) and _events(caplog)[-1] == "stopped"
    assert tasks_left == []


def test_crash_during_shutdown_is_not_restarted():
    async def scenario():
        sup, _clock, delays = _make()
        starts = 0
        gate = asyncio.Event()

        async def dies_on_release():
            nonlocal starts
            starts += 1
            await gate.wait()
            raise RuntimeError("late")

        sup.add(ServiceSpec("svc", dies_on_release))
        await _until(lambda: starts == 1)
        sup.begin_shutdown()
        gate.set()
        await sup.stop()
        return starts, delays

    assert asyncio.run(scenario()) == (1, [])


def test_stop_cancels_a_service_that_ignores_the_stop_request():
    async def scenario():
        sup, _clock, _delays = _make()
        started = asyncio.Event()

        async def stubborn():
            started.set()
            await asyncio.Event().wait()

        sup.add(ServiceSpec("svc", stubborn))
        await started.wait()
        await sup.stop(timeout=0.01)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []


def test_stop_returns_when_a_service_swallows_cancellation(caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER)
    release = threading.Event()

    async def scenario():
        sup, _clock, _delays = _make()
        started = asyncio.Event()

        async def swallows_cancel():
            started.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue  # ignores cancellation on purpose

        sup.add(ServiceSpec("stuck", swallows_cancel))
        await started.wait()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await sup.stop(timeout=0.05, cancel_timeout=0.1)
        elapsed = loop.time() - t0
        release.set()
        await asyncio.sleep(0.05)
        return elapsed

    assert asyncio.run(scenario()) < 2.0
    assert "service=stuck event=stop_cancel_timeout" in caplog.text


def test_no_restart_while_database_is_quiesced(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    active_at_start: list[bool] = []
    polls = 0

    def release_after_polls(delay: float) -> None:
        nonlocal polls
        if delay == 0.5:  # maintenance poll
            polls += 1
            if polls == 3:
                maintenance.end_maintenance()

    async def scenario():
        sup, _clock, _delays = _make(on_sleep=release_after_polls)
        stop = asyncio.Event()
        calls = 0

        async def svc():
            nonlocal calls
            calls += 1
            active_at_start.append(maintenance.is_active())
            if calls == 1:
                assert maintenance.begin_maintenance()
                raise RuntimeError("died while quiesced")
            await stop.wait()

        sup.add(ServiceSpec("svc", svc, request_stop=stop.set))
        await _until(lambda: calls == 2)
        await sup.stop()

    asyncio.run(scenario())
    assert active_at_start == [False, False]  # second start only after the restore window ended
    assert polls == 3
    assert {"paused", "resumed"} <= set(_events(caplog))


def test_maintenance_refusal_is_not_counted_as_crash():
    async def scenario():
        sup, _clock, _delays = _make()
        stop = asyncio.Event()
        calls = 0

        async def svc():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise maintenance.MaintenanceActiveError("restore")
            await stop.wait()

        sup.add(ServiceSpec("svc", svc, request_stop=stop.set))
        await _until(lambda: calls == 2)
        crashes = sup.snapshot()["svc"]["crashes_total"]
        await sup.stop()
        return crashes

    assert asyncio.run(scenario()) == 0


def test_missing_heartbeat_cancels_and_restarts_the_service(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    async def scenario():
        sup, clock, _delays = _make()
        starts = 0
        cancelled = 0

        async def hangs():
            nonlocal starts, cancelled
            starts += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        sup.add(ServiceSpec("svc", hangs, heartbeat_timeout=10.0))
        await _until(lambda: starts == 1)
        clock.now += 11
        await _until(lambda: starts == 2)
        snap = sup.snapshot()["svc"]
        await sup.stop()
        return cancelled, snap

    cancelled, snap = asyncio.run(scenario())
    assert cancelled >= 1
    assert snap["last_exception_type"] == "ServiceHungError"


def test_repeated_hangs_degrade_and_only_a_long_clean_run_recovers():
    policy = SupervisionPolicy(
        jitter=0.0, check_interval=0.01, stable_after=20.0, hang_recovery_after=60.0, degraded_retry=300.0
    )

    async def scenario():
        sup, clock, delays = _make(policy)
        starts = 0
        mode = "hang"

        async def run():
            nonlocal starts
            starts += 1
            while True:
                if mode == "ok":
                    sup.heartbeat("svc")
                    await asyncio.sleep(0.001)
                else:
                    await asyncio.Event().wait()

        sup.add(ServiceSpec("svc", run, heartbeat_timeout=10.0))
        for n in (1, 2):
            await _until(lambda n=n: starts == n)
            clock.now += 11
        await _until(lambda: starts == 3)
        assert sup.snapshot()["svc"]["state"] != ServiceState.DEGRADED.value  # two hangs are not enough
        mode = "ok"
        clock.now += 11
        await _until(lambda: starts == 4)
        degraded_after_third = sup.snapshot()["svc"]
        # A run longer than stable_after clears the crash state but not the hang history.
        for _ in range(5):
            clock.now += 5
            await asyncio.sleep(0.03)
        after_stable = sup.snapshot()["svc"]
        for _ in range(10):
            clock.now += 5
            await asyncio.sleep(0.03)
        recovered = sup.snapshot()["svc"]
        await sup.stop()
        return delays, degraded_after_third, after_stable, recovered

    delays, degraded, after_stable, recovered = asyncio.run(scenario())
    assert degraded["state"] == ServiceState.DEGRADED.value
    assert degraded["hangs_in_window"] == 3
    assert 300.0 in delays
    assert after_stable["state"] == ServiceState.DEGRADED.value
    assert after_stable["hangs_in_window"] == 3
    assert recovered["state"] == ServiceState.RUNNING.value
    assert recovered["hangs_in_window"] == 0


def test_heartbeat_keeps_a_busy_service_alive():
    async def scenario():
        sup, clock, _delays = _make()
        stop = asyncio.Event()
        starts = 0

        async def beats():
            nonlocal starts
            starts += 1
            await stop.wait()

        sup.add(ServiceSpec("svc", beats, request_stop=stop.set, heartbeat_timeout=10.0))
        await _until(lambda: starts == 1)
        for _ in range(5):
            clock.now += 8
            sup.heartbeat("svc")
            await asyncio.sleep(0.03)
        await sup.stop()
        return starts

    assert asyncio.run(scenario()) == 1


def test_duplicate_service_name_is_rejected():
    async def scenario():
        sup, _clock, _delays = _make()
        stop = asyncio.Event()
        sup.add(ServiceSpec("svc", stop.wait, request_stop=stop.set))
        with pytest.raises(ValueError, match="already registered"):
            sup.add(ServiceSpec("svc", stop.wait))
        await sup.stop()

    asyncio.run(scenario())


def test_app_lifespan_supervises_services_and_leaves_no_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.utils import bootstrap

    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    before = {t.ident for t in threading.enumerate()}

    app = create_app()
    with TestClient(app) as client:
        body = client.get("/healthz").json()
        assert body["ok"] is True and body["degraded"] is False
        assert {"monitor_scheduler", "pdf_worker", "pdf_cleanup", "pop_refresh"} <= set(body["services"])
        assert all(isinstance(v, str) for v in body["services"].values())
    snap = app.state.supervisor.snapshot()
    assert {s["state"] for s in snap.values()} == {"stopped"}
    leaked = [t.name for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert leaked == []


def test_pop_refresh_error_pauses_instead_of_spinning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import time
    from types import SimpleNamespace

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services import pop_refresh
    from app.utils import bootstrap

    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    calls = 0

    def failing(*_a, **_k):
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    # A plan that is always due and a refresh that fails before persisting any run.
    monkeypatch.setattr(pop_refresh, "plan_next_refresh", lambda _c: SimpleNamespace(delay_seconds=0))
    monkeypatch.setattr(pop_refresh, "refresh_external_sources", failing)

    with TestClient(create_app()):
        deadline = time.monotonic() + 5
        while calls == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)
        assert calls == 1  # no immediate retry within the error pause
        started = time.monotonic()
    assert time.monotonic() - started < 5  # the stop event ends the pause at once


def test_crash_while_quiesced_is_not_counted():
    async def scenario():
        sup, _clock, _delays = _make(on_sleep=lambda d: maintenance.end_maintenance() if d == 0.5 else None)
        stop = asyncio.Event()
        calls = 0

        async def svc():
            nonlocal calls
            calls += 1
            if calls == 1:
                assert maintenance.begin_maintenance()
                raise RuntimeError("connection closed by restore")
            await stop.wait()

        sup.add(ServiceSpec("svc", svc, request_stop=stop.set))
        await _until(lambda: calls == 2)
        crashes = sup.snapshot()["svc"]["crashes_total"]
        await sup.stop()
        return crashes

    assert asyncio.run(scenario()) == 0


def _scheduler(tmp_path: Path):
    from app.monitor.scheduler import MonitorScheduler

    return MonitorScheduler(get_conn=lambda: None, tsdb_dir=tmp_path)


def test_thread_work_of_a_replaced_scheduler_run_is_discarded_once(tmp_path: Path, caplog):
    from app.monitor.scheduler import StaleRunError

    sched = _scheduler(tmp_path)
    ran: list[int] = []
    call = sched._thread_call(sched._run_epoch, lambda: ran.append(1))
    call()
    assert ran == [1]
    sched._run_epoch += 1  # the supervisor restarted the scheduler
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        with pytest.raises(StaleRunError):
            call()
    assert ran == [1]
    stale = [r for r in caplog.records if "event=stale_run_discarded" in r.getMessage()]
    assert len(stale) == 1 and "service=monitor_scheduler" in stale[0].getMessage()
    assert sched._threads_active == 0


def test_new_scheduler_run_waits_for_threads_of_the_old_run(tmp_path: Path, caplog):
    sched = _scheduler(tmp_path)
    caplog.set_level(logging.WARNING)

    async def scenario():
        await sched._drain_old_threads(timeout=0.5)  # nothing running: returns at once
        sched._threads_active = 1
        asyncio.get_running_loop().call_later(0.1, lambda: setattr(sched, "_threads_active", 0))
        await sched._drain_old_threads(timeout=2.0)
        assert not caplog.records
        sched._threads_active = 1
        await sched._drain_old_threads(timeout=0.05)

    asyncio.run(scenario())
    assert any("event=old_threads_still_running" in r.getMessage() for r in caplog.records)


def test_scheduler_run_bumps_the_epoch_per_restart(tmp_path: Path):
    sched = _scheduler(tmp_path)

    async def scenario():
        sched._register_run = lambda: None
        sched.request_stop()  # loop ends immediately
        await sched.run()
        await sched.run()

    asyncio.run(scenario())
    assert sched._run_epoch == 2


def test_abandoned_run_cannot_pass_itself_off_as_the_current_run(tmp_path: Path):
    from app.monitor.scheduler import StaleRunError

    sched = _scheduler(tmp_path)
    sched._register_run = lambda: None
    old_epoch_seen: list[int] = []
    outcome: list[str] = []

    async def scenario():
        async def old_run():
            sched.request_stop()
            await sched.run()
            old_epoch_seen.append(sched._epoch_for_thread())
            await release.wait()  # the hung await of the old run returns after the restart
            try:
                await asyncio.to_thread(sched._thread_call(sched._epoch_for_thread(), lambda: outcome.append("ran")))
            except StaleRunError:
                outcome.append("refused")

        release = asyncio.Event()
        old = asyncio.create_task(old_run())
        await asyncio.sleep(0.05)
        await asyncio.create_task(sched.run())  # supervisor restart in a fresh task/context
        release.set()
        await old

    asyncio.run(scenario())
    assert old_epoch_seen == [1] and sched._run_epoch == 2
    assert outcome == ["refused"]
