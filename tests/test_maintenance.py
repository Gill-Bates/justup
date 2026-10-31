#!/usr/bin/env python3
#
# tests/test_maintenance.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Maintenance state, TSDB snapshot under file locks and background workers holding back during restore."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import sqlite as sqlite_db
from app.db import tsdb
from app.middleware.maintenance import MaintenanceWriteMiddleware
from app.monitor import scheduler as scheduler_module
from app.monitor.scheduler import MonitorScheduler
from app.services.pdf_worker import PDFWorker
from app.utils import maintenance
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture(autouse=True)
def _reset_maintenance():
    maintenance.end_maintenance()
    yield
    maintenance.end_maintenance()


class TestMaintenanceState:
    def test_work_is_refused_while_active_and_allowed_again_afterwards(self):
        assert maintenance.begin_maintenance(timeout=1)
        with pytest.raises(maintenance.MaintenanceActiveError), maintenance.work_section():
            pytest.fail("section must not start during maintenance")
        maintenance.end_maintenance()
        maintenance.end_maintenance()  # idempotent
        with maintenance.work_section():
            pass

    def test_begin_waits_for_running_work_and_refuses_new_work_meanwhile(self, monkeypatch):
        entered, release = threading.Event(), threading.Event()
        drain_waiting = threading.Event()
        real_condition_wait = maintenance._COND.wait

        def observe_drain(timeout=None):
            drain_waiting.set()
            return real_condition_wait(timeout)

        monkeypatch.setattr(maintenance._COND, "wait", observe_drain)

        def worker():
            with maintenance.work_section():
                entered.set()
                release.wait(5)

        t = threading.Thread(target=worker)
        t.start()
        entered.wait(5)
        result: list[bool] = []
        b = threading.Thread(target=lambda: result.append(maintenance.begin_maintenance(timeout=5)))
        b.start()
        assert drain_waiting.wait(5)
        assert maintenance.is_active()
        with pytest.raises(maintenance.MaintenanceActiveError), maintenance.work_section():
            pytest.fail("new work must be refused while draining")
        assert not result  # still waiting for the running section
        release.set()
        b.join(5)
        t.join(5)
        assert result == [True]

    def test_timeout_returns_false_and_lifts_the_refusal(self):
        release = threading.Event()
        entered = threading.Event()

        def worker():
            with maintenance.work_section():
                entered.set()
                release.wait(5)

        t = threading.Thread(target=worker)
        t.start()
        entered.wait(5)
        started = time.monotonic()
        assert not maintenance.begin_maintenance(timeout=0.2)
        assert time.monotonic() - started < 2  # bounded, no hang
        assert not maintenance.is_active()
        release.set()
        t.join(5)
        with maintenance.work_section():
            pass

    def test_section_is_released_when_work_fails(self):
        with pytest.raises(ValueError), maintenance.work_section():
            raise ValueError
        assert maintenance.begin_maintenance(timeout=0.5)

    def test_second_begin_does_not_lift_the_first(self):
        assert maintenance.begin_maintenance(timeout=1)
        assert not maintenance.begin_maintenance(timeout=0.1)
        assert maintenance.is_active()

    def test_confirmed_data_replacement_invalidates_pre_restore_work_snapshots(self):
        old_generation = maintenance.current_generation()
        assert maintenance.begin_maintenance(timeout=1)
        assert maintenance.current_generation() == old_generation  # drain alone does not advance
        new_generation = maintenance.confirm_data_replaced()  # only a confirmed swap advances
        assert new_generation == old_generation + 1
        maintenance.end_maintenance()
        with pytest.raises(maintenance.StaleWorkError), maintenance.work_section(old_generation):
            pytest.fail("pre-restore work must not write into the restored data set")
        with maintenance.work_section(new_generation):
            pass

    def test_confirm_data_replaced_requires_an_active_maintenance_window(self):
        # The generation must only advance from inside a live restore window; a stray call outside
        # maintenance must not bump it.
        before = maintenance.current_generation()
        with pytest.raises(RuntimeError, match="active maintenance window"):
            maintenance.confirm_data_replaced()
        assert maintenance.current_generation() == before

    def test_drained_maintenance_without_data_replacement_does_not_advance_generation(self):
        # A failed restore (or failed quiescence) drains work but replaces no data; pre-restore work
        # computed against the still-current generation must remain valid afterwards.
        old_generation = maintenance.current_generation()
        assert maintenance.begin_maintenance(timeout=1)
        maintenance.end_maintenance()  # window ends without confirm_data_replaced()
        assert maintenance.current_generation() == old_generation
        with maintenance.work_section(old_generation):
            pass

    def test_http_write_requests_are_refused_until_restore_finishes(self):
        app = FastAPI()
        app.add_middleware(MaintenanceWriteMiddleware)
        writes = []

        @app.get("/read")
        def read():
            return {"ok": True}

        @app.post("/write")
        def write():
            writes.append("written")
            return {"ok": True}

        client = TestClient(app)
        assert maintenance.begin_maintenance(timeout=1)
        try:
            assert client.get("/read").status_code == 200
            response = client.post("/write")
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "30"
            assert writes == []
        finally:
            maintenance.end_maintenance()
        assert client.post("/write").status_code == 200
        assert writes == ["written"]

    def test_network_probe_does_not_hold_restore_and_stale_result_is_not_written(self, monkeypatch):
        entered = threading.Event()
        release = threading.Event()
        instance = MonitorScheduler.__new__(MonitorScheduler)
        instance._last_quality_probe_at = None
        instance._quality_state = scheduler_module.QualityState.OK
        instance._connectivity_ok = True
        instance._connectivity_lost_at = None

        async def _slow_probe():
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return SimpleNamespace(score=80, median_latency_ms=20, success_ratio=1.0, state="ok")

        monkeypatch.setattr(scheduler_module, "run_quality_probe", _slow_probe)
        generation = maintenance.current_generation()

        async def _run():
            return await instance._run_quality_probe(utcnow(), generation)

        async def _scenario():
            task = asyncio.create_task(_run())
            assert await asyncio.to_thread(entered.wait, 5)
            try:
                # The network probe is in flight but has not entered a DB write section.
                assert maintenance.begin_maintenance(timeout=0.5)
                maintenance.confirm_data_replaced()  # a restore replaced the data under the probe
                maintenance.end_maintenance()
            finally:
                release.set()
            with pytest.raises(maintenance.StaleWorkError):
                await task

        asyncio.run(_scenario())

    def test_cancelled_to_thread_keeps_maintenance_section_until_writer_exits(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        generation = maintenance.current_generation()

        def _writer():
            entered.set()
            release.wait(5)
            finished.set()

        async def _scenario():
            task = asyncio.create_task(
                asyncio.to_thread(scheduler_module._run_in_maintenance_section, generation, _writer)
            )
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not maintenance.begin_maintenance(timeout=0.05)
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            assert maintenance.begin_maintenance(timeout=1)
            maintenance.end_maintenance()

        asyncio.run(_scenario())

    def test_cancelled_pdf_executor_keeps_maintenance_section_until_callback_exits(self, tmp_path: Path):
        from app.services.pdf_worker import _run_in_maintenance_section

        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        generation = maintenance.current_generation()
        worker = PDFWorker(storage_dir=tmp_path, cpu_workers=1, io_workers=1)

        def _render():
            entered.set()
            try:
                release.wait(5)
            finally:
                finished.set()

        async def _scenario():
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                worker.cpu_executor,
                _run_in_maintenance_section,
                generation,
                _render,
            )
            assert await asyncio.to_thread(entered.wait, 5)
            future.cancel()
            with pytest.raises(asyncio.CancelledError):
                await future
            assert not maintenance.begin_maintenance(timeout=0.05)
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            assert maintenance.begin_maintenance(timeout=1)
            maintenance.end_maintenance()

        try:
            asyncio.run(_scenario())
        finally:
            release.set()
            worker.shutdown()


class TestTsdbSnapshot:
    def _seed(self, root: Path) -> None:
        for tid in (1, 2):
            for i in range(5):
                tsdb.append_point(root, target_id=tid, metric="ping_ms", value=i, retention_days=30)

    def test_snapshot_copies_series_without_lock_files(self, tmp_path: Path):
        src, dst = tmp_path / "tsdb", tmp_path / "snap"
        self._seed(src)
        assert tsdb.snapshot_tsdb(src, dst) == 2
        assert sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()) == [
            "target_1/ping_ms.jsonl",
            "target_2/ping_ms.jsonl",
        ]
        assert (dst / "target_1" / "ping_ms.jsonl").read_bytes() == (src / "target_1" / "ping_ms.jsonl").read_bytes()

    def test_snapshot_is_never_torn_while_writers_append(self, tmp_path: Path):
        src = tmp_path / "tsdb"
        self._seed(src)
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                tsdb.append_point(src, target_id=1, metric="ping_ms", value="x" * 2000 + str(i), retention_days=30)
                i += 1

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for n in range(10):
                dst = tmp_path / f"snap{n}"
                tsdb.snapshot_tsdb(src, dst)
                text = (dst / "target_1" / "ping_ms.jsonl").read_text(encoding="utf-8")
                assert text.endswith("\n")
                for line in text.splitlines():
                    json.loads(line)  # every line complete
        finally:
            stop.set()
            for t in threads:
                t.join(5)

    def test_writer_is_not_delayed_by_the_copy_and_the_copy_is_a_clean_cut(self, tmp_path: Path, monkeypatch):
        src, dst = tmp_path / "tsdb", tmp_path / "snap"
        self._seed(src)
        copying, release = threading.Event(), threading.Event()
        real_copy = tsdb._copy_exact

        def slow_copy(a, b, size):
            copying.set()
            release.wait(5)
            return real_copy(a, b, size)

        monkeypatch.setattr(tsdb, "_copy_exact", slow_copy)
        snap = threading.Thread(target=tsdb.snapshot_tsdb, args=(src, dst))
        snap.start()
        assert copying.wait(5)
        done = threading.Event()

        def append():
            tsdb.append_point(src, target_id=1, metric="ping_ms", value=99, retention_days=30)
            done.set()

        w = threading.Thread(target=append)
        w.start()
        assert done.wait(5)  # the event-loop writer never waits for a running backup copy
        release.set()
        snap.join(5)
        w.join(5)
        lines = (dst / "target_1" / "ping_ms.jsonl").read_text().splitlines()
        assert len(lines) == 5  # the append that happened during the copy is not part of the snapshot
        assert len((src / "target_1" / "ping_ms.jsonl").read_text().splitlines()) == 6


class TestBackgroundWorkersHoldBack:
    def test_scheduler_starts_no_iteration_during_maintenance_and_resumes(self, tmp_path: Path):
        async def scenario() -> tuple[int, int]:
            sched = MonitorScheduler(get_conn=lambda: None, tsdb_dir=tmp_path)
            runs: list[int] = []
            attempted, resumed = asyncio.Event(), asyncio.Event()

            async def fake_iteration(_loop, generation=None):
                # A real iteration opens a work section; maintenance makes that raise, holding back.
                attempted.set()
                with maintenance.work_section(generation):
                    runs.append(1)
                    resumed.set()

            sched._run_iteration = fake_iteration  # type: ignore[method-assign]
            assert maintenance.begin_maintenance(timeout=1)
            task = asyncio.create_task(sched._run_loop())
            await asyncio.wait_for(attempted.wait(), 5)
            held = len(runs)
            maintenance.end_maintenance()
            await asyncio.wait_for(resumed.wait(), 5)
            sched._stop.set()
            await asyncio.wait_for(task, 5)
            return held, len(runs)

        held, after = asyncio.run(scenario())
        assert held == 0
        assert after > 0

    def test_running_scheduler_iteration_is_awaited_by_maintenance_and_stop(self, tmp_path: Path, monkeypatch):
        async def scenario() -> tuple[bool, bool, bool]:
            sched = MonitorScheduler(get_conn=lambda: None, tsdb_dir=tmp_path)
            finished = asyncio.Event()
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_iteration(_loop, generation=None):
                # A real iteration runs inside a work section, which maintenance must await.
                with maintenance.work_section(generation):
                    started.set()
                    await release.wait()
                    finished.set()

            sched._run_iteration = slow_iteration  # type: ignore[method-assign]

            async def no_idle_sleep():
                return None

            sched._idle_sleep = no_idle_sleep  # type: ignore[method-assign]
            task = asyncio.create_task(sched._run_loop())
            sched._task = task
            await asyncio.wait_for(started.wait(), 5)
            drain_waiting = threading.Event()
            real_condition_wait = maintenance._COND.wait

            def observe_drain(timeout=None):
                drain_waiting.set()
                return real_condition_wait(timeout)

            monkeypatch.setattr(maintenance._COND, "wait", observe_drain)
            drain = asyncio.create_task(asyncio.to_thread(maintenance.begin_maintenance, 5.0))
            assert await asyncio.to_thread(drain_waiting.wait, 5)
            remained_blocked = not drain.done() and not finished.is_set()
            stopping = asyncio.create_task(sched.stop())
            stop_waited_for_iteration = not stopping.done()
            release.set()
            drained = await asyncio.wait_for(drain, 5)
            assert finished.is_set()
            maintenance.end_maintenance()
            await asyncio.wait_for(stopping, 5)
            return drained, remained_blocked, stop_waited_for_iteration

        assert asyncio.run(scenario()) == (True, True, True)

    def test_scheduler_starts_no_iteration_once_stop_is_set(self, tmp_path: Path):
        async def scenario() -> int:
            sched = MonitorScheduler(get_conn=lambda: None, tsdb_dir=tmp_path)
            runs: list[int] = []

            async def fake_iteration(_loop, _generation=None):
                runs.append(1)

            sched._run_iteration = fake_iteration  # type: ignore[method-assign]
            sched._stop.set()  # shutdown requested before the loop body runs
            await sched._run_loop()  # returns immediately, no iteration started
            return len(runs)

        assert asyncio.run(scenario()) == 0

    def test_scheduler_survives_a_failing_iteration_and_runs_the_next(self, tmp_path: Path):
        async def scenario() -> int:
            sched = MonitorScheduler(get_conn=lambda: None, tsdb_dir=tmp_path)
            runs: list[int] = []

            async def flaky_iteration(_loop, _generation=None):
                runs.append(1)
                if len(runs) == 1:
                    raise RuntimeError("boom")
                sched._stop.set()  # second iteration proves recovery, then shut down

            async def no_sleep():
                return None

            sched._run_iteration = flaky_iteration  # type: ignore[method-assign]
            sched._idle_sleep = no_sleep  # type: ignore[method-assign]
            await asyncio.wait_for(sched._run_loop(), 5)
            return len(runs)

        assert asyncio.run(scenario()) == 2

    def test_pdf_job_is_held_back_until_maintenance_ends_or_worker_stops(self, tmp_path: Path, monkeypatch):
        async def scenario() -> tuple[bool, bool]:
            import contextlib

            from app.services import pdf_worker as pdf_worker_module

            worker = PDFWorker(storage_dir=tmp_path, cpu_workers=1, io_workers=1)
            assert maintenance.begin_maintenance(timeout=1)
            stop = asyncio.Event()
            wait_attempted = asyncio.Event()
            real_sleep = asyncio.sleep

            async def observe_wait(_delay):
                wait_attempted.set()
                await real_sleep(0)

            monkeypatch.setattr(pdf_worker_module.asyncio, "sleep", observe_wait)
            waiting = asyncio.create_task(worker._enter_work_section(contextlib.ExitStack(), stop))
            await asyncio.wait_for(wait_attempted.wait(), 5)
            still_waiting = not waiting.done()
            stop.set()
            entered_after_stop = await asyncio.wait_for(waiting, 5)
            worker.shutdown()
            return still_waiting, entered_after_stop

        still_waiting, entered = asyncio.run(scenario())
        assert still_waiting
        assert entered is False


class TestAtomicPdfWrite:
    def test_atomic_write_produces_full_bytes_and_no_tmp(self, tmp_path: Path):
        worker = PDFWorker(storage_dir=tmp_path, cpu_workers=1, io_workers=1)
        try:
            pdf_path = worker.get_pdf_path("job-a")
            payload = b"%PDF-1.4\n" + b"x" * 4096
            worker._atomic_write_bytes(pdf_path, payload)
            assert pdf_path.read_bytes() == payload
            assert not list(tmp_path.glob("*.tmp"))
        finally:
            worker.shutdown()

    def test_atomic_write_on_replace_failure_leaves_target_untouched_and_no_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        worker = PDFWorker(storage_dir=tmp_path, cpu_workers=1, io_workers=1)
        try:
            pdf_path = worker.get_pdf_path("job-b")

            def _boom(*_args, **_kwargs):
                raise OSError("injected replace failure")

            monkeypatch.setattr(os, "replace", _boom)
            with pytest.raises(OSError, match="injected replace failure"):
                worker._atomic_write_bytes(pdf_path, b"never published")
            # Final path was never created and no temp litter remains.
            assert not pdf_path.exists()
            assert not list(tmp_path.glob("*.tmp"))
        finally:
            worker.shutdown()


class _BlockingCloseConn:
    """Wraps a real connection; its ``close()`` blocks on an Event before delegating.

    ``total_changes`` forwards to the live connection, so ``_prune_closed_connections`` keeps
    the proxy registered until the real close has actually run.
    """

    def __init__(self, real, started: threading.Event, release: threading.Event):
        self._real = real
        self._started = started
        self._release = release

    @property
    def total_changes(self):
        return self._real.total_changes

    def close(self):
        self._started.set()
        self._release.wait(5)
        self._real.close()


class TestCloseBeforeDeregister:
    def test_begin_quiesce_waits_until_real_close_completes(self, tmp_path: Path):
        db_path = tmp_path / "justup.db"
        real = sqlite_db.connect(db_path)
        started = threading.Event()
        release = threading.Event()
        proxy = _BlockingCloseConn(real, started, release)

        # Register the proxy as a tracked connection and drop the real handle's registration,
        # so the proxy is the sole open connection the quiesce must wait for.
        sqlite_db._OPEN_CONNECTIONS.discard(real)
        sqlite_db._OPEN_CONNECTIONS.add(proxy)

        quiesce_result: list[bool] = []

        try:
            closer = threading.Thread(target=lambda: sqlite_db.close_connection(proxy))
            closer.start()
            assert started.wait(5)

            quiescer = threading.Thread(target=lambda: quiesce_result.append(sqlite_db.begin_quiesce(timeout=5)))
            quiescer.start()

            # While the close is blocked, the proxy stays registered and begin_quiesce cannot return.
            assert sqlite_db.get_connection_stats()["currently_open"] >= 1
            assert quiesce_result == []

            release.set()
            closer.join(5)
            quiescer.join(5)
            assert quiesce_result == [True]
        finally:
            release.set()
            sqlite_db.end_quiesce()
            sqlite_db._OPEN_CONNECTIONS.discard(proxy)
            with contextlib.suppress(Exception):
                real.close()


class TestSchedulerRestoreGeneration:
    """Scheduler periodic work is bound to its originating data generation (finding 4b)."""

    def _sched(self, db_path: Path, tmp_path: Path) -> MonitorScheduler:
        conn = sqlite_db.connect(db_path)
        sqlite_db.init_schema(conn)
        sqlite_db.close_connection(conn)
        return MonitorScheduler(get_conn=lambda: sqlite_db.connect(db_path), tsdb_dir=tmp_path / "tsdb")

    def test_iteration_threads_generation_into_periodic_work_without_type_error(self, tmp_path: Path):
        # Regression: _run_iteration passes generation into the periodic helpers; their signatures
        # must accept it, or every real iteration raises TypeError and the loop silently stalls.
        sched = self._sched(tmp_path / "justup.db", tmp_path)

        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            await sched._run_iteration(loop, maintenance.current_generation())

        asyncio.run(scenario())

    def test_spooler_pass_from_a_superseded_generation_does_not_process(self, tmp_path: Path):
        from app.services.notifications.spooler import NotificationSpooler

        db_path = tmp_path / "justup.db"
        sched = self._sched(db_path, tmp_path)
        spooler = NotificationSpooler(lambda: sqlite_db.connect(db_path))
        # immediate=True makes the item due now, so a current-generation pass would claim and attempt
        # it (SMTP not configured -> mark_retry -> retry_count 1). A stale pass must do neither.
        qid = spooler.enqueue("alert", "email", 1, "R", "r@example.com", {"subject": "s", "body": "b"}, immediate=True)

        stale = maintenance.current_generation()
        assert maintenance.begin_maintenance(timeout=1)
        maintenance.confirm_data_replaced()  # a restore replaced the data, advancing the generation
        maintenance.end_maintenance()

        async def scenario() -> None:
            await sched._process_notification_spooler(utcnow(), stale)

        asyncio.run(scenario())
        conn = sqlite_db.connect(db_path)
        try:
            status, retry_count = conn.execute(
                "SELECT status, retry_count FROM notification_queue WHERE id = ?", (qid,)
            ).fetchone()
        finally:
            sqlite_db.close_connection(conn)
        assert status == "pending" and retry_count == 0  # untouched by the superseded pass

    def test_db_maintenance_from_a_superseded_generation_is_deferred(self, tmp_path: Path):
        sched = self._sched(tmp_path / "justup.db", tmp_path)
        stale = maintenance.current_generation()
        assert maintenance.begin_maintenance(timeout=1)
        maintenance.confirm_data_replaced()  # a restore replaced the data, advancing the generation
        maintenance.end_maintenance()

        async def scenario():
            await sched._run_db_maintenance(utcnow(), stale)
            return sched._last_db_maintenance

        # Deferred (retry_at in the past), never completed under the superseded generation.
        last = asyncio.run(scenario())
        assert last is not None and last < utcnow()
