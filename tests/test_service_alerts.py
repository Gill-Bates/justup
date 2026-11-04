#!/usr/bin/env python3
#
# tests/test_service_alerts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001 - white-box access to the sink queue and the scheduler loop
"""Supervisor -> admin inbox: crash, degraded and recovery alerts, dedupe, retry and isolation.

Time is a controlled clock (no real waiting); the alert path uses its own database connection and
needs neither the monitoring scheduler nor the SMTP spooler.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import notifications as inbox
from app.db import sqlite as sqlite_db
from app.monitor.scheduler import MonitorScheduler
from app.services.release_check import ReleaseChecker
from app.services.service_alerts import ServiceAlertSink
from app.utils import maintenance
from app.utils.supervisor import ServiceAlert, ServiceSpec, ServiceSupervisor, SupervisionPolicy
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests.test_supervisor import FakeClock, _make, _until

SECRET = "hunter2-SECRET-token"  # noqa: S105 - dummy value that must never reach the inbox


@pytest.fixture(autouse=True)
def _no_maintenance():
    maintenance.end_maintenance()
    yield
    maintenance.end_maintenance()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "alerts.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    return path


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite_db.connect(db_path)
    try:
        return conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    finally:
        sqlite_db.close_connection(conn)


def _setup(db_path: Path, policy: SupervisionPolicy | None = None, **sink_kwargs):
    """Supervisor on a fake clock whose notifier writes to the real (temporary) inbox."""
    sink = ServiceAlertSink(lambda: sqlite_db.connect(db_path), **sink_kwargs)
    clock = FakeClock()
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        clock.now += delay
        await asyncio.sleep(0)

    sup = ServiceSupervisor(
        policy or SupervisionPolicy(jitter=0.0, check_interval=0.01), clock=clock, sleep=fake_sleep, notifier=sink
    )
    return sup, sink, clock


def _alert(kind: str = "crashed", **kw) -> ServiceAlert:
    base = {"service": "svc", "label": "Test service", "kind": kind, "at": datetime.now(UTC), "exc_type": "ValueError"}
    return ServiceAlert(**{**base, **kw})


# ─── Supervisor transitions (collected alerts) ───────────────────────────────


def _collecting(policy: SupervisionPolicy | None = None):
    sup, clock, _delays = _make(policy)
    alerts: list[ServiceAlert] = []
    sup._notifier = alerts.append
    return sup, clock, alerts


def test_crash_restarts_and_raises_exactly_one_warning():
    async def scenario():
        sup, _clock, alerts = _collecting()
        stop = asyncio.Event()
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError(SECRET)
            await stop.wait()

        sup.add(ServiceSpec("svc", flaky, request_stop=stop.set, label="Svc"))
        await _until(lambda: calls == 2)
        await sup.stop()
        return alerts

    alerts = asyncio.run(scenario())
    assert [a.kind for a in alerts] == ["crashed"]
    assert alerts[0].exc_type == "ValueError"
    assert SECRET not in repr(alerts)


def test_crash_loop_in_window_degrades_with_error_alert():
    async def scenario():
        sup, _clock, alerts = _collecting()
        starts = 0

        async def always_fails():
            nonlocal starts
            starts += 1
            if starts > 5:
                await asyncio.Event().wait()
            raise RuntimeError("x")

        sup.add(ServiceSpec("svc", always_fails))
        await _until(lambda: starts == 6)
        snap = sup.snapshot()["svc"]["state"]
        await sup.stop()
        return alerts, snap

    alerts, state = asyncio.run(scenario())
    assert state == "degraded"
    assert [a.kind for a in alerts] == ["crashed"] * 5 + ["degraded"]
    assert alerts[-1].reason == "crash_loop"


def test_failure_series_inside_a_running_task_degrades_after_threshold_and_span():
    async def scenario():
        sup, clock, alerts = _collecting()
        stop = asyncio.Event()

        async def runs():
            await stop.wait()

        sup.add(ServiceSpec("svc", runs, request_stop=stop.set))
        await _until(lambda: sup.snapshot()["svc"]["state"] == "running")
        for _ in range(6):  # burst: enough failures but not long enough
            sup.report_failure("svc", "OSError")
        assert alerts == []
        clock.now += 130
        sup.report_failure("svc", "OSError")
        sup.report_failure("svc", "OSError")  # further failures do not alert again
        snap = sup.snapshot()["svc"]
        await sup.stop()
        return alerts, snap

    alerts, snap = asyncio.run(scenario())
    assert [(a.kind, a.reason) for a in alerts] == [("degraded", "failure_series")]
    assert snap["state"] == "degraded"
    assert snap["failure_streak"] == 8


def test_recovery_only_after_stability_window_and_only_once():
    async def scenario():
        sup, clock, alerts = _collecting()
        stop = asyncio.Event()
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("x")
            await stop.wait()

        sup.add(ServiceSpec("svc", flaky, request_stop=stop.set))
        await _until(lambda: calls == 2)
        await asyncio.sleep(0.05)
        assert [a.kind for a in alerts] == ["crashed"]  # a plain restart is no recovery
        clock.now += 59
        await asyncio.sleep(0.05)
        assert [a.kind for a in alerts] == ["crashed"]
        clock.now += 2
        await _until(lambda: [a.kind for a in alerts] == ["crashed", "recovered"])
        clock.now += 500
        await asyncio.sleep(0.05)
        await sup.stop()
        return alerts

    assert [a.kind for a in asyncio.run(scenario())] == ["crashed", "recovered"]


def test_failure_series_recovery_needs_a_success_and_a_stability_window():
    async def scenario():
        sup, clock, alerts = _collecting()
        stop = asyncio.Event()

        async def runs():
            await stop.wait()

        sup.add(ServiceSpec("svc", runs, request_stop=stop.set))
        await _until(lambda: sup.snapshot()["svc"]["state"] == "running")
        sup.report_failure("svc", "OSError")
        clock.now += 130
        sup.report_failure("svc", "OSError")
        for _ in range(4):
            sup.report_failure("svc", "OSError")
        assert [a.kind for a in alerts] == ["degraded"]
        clock.now += 500  # time alone, without a successful iteration, proves nothing
        await asyncio.sleep(0.05)
        assert [a.kind for a in alerts] == ["degraded"]
        sup.report_success("svc")
        clock.now += 30
        await asyncio.sleep(0.05)
        assert [a.kind for a in alerts] == ["degraded"]
        clock.now += 40
        await _until(lambda: [a.kind for a in alerts] == ["degraded", "recovered"])
        await sup.stop()
        return sup

    asyncio.run(scenario())


def test_shutdown_and_maintenance_raise_no_alert():
    async def scenario():
        sup, _clock, alerts = _collecting()
        stop = asyncio.Event()
        started = 0

        async def runs():
            nonlocal started
            started += 1
            await stop.wait()

        sup.add(ServiceSpec("svc", runs, request_stop=stop.set))
        await _until(lambda: started == 1)
        assert maintenance.begin_maintenance()
        sup.report_failure("svc", "OSError")  # failures during a restore are intended pauses
        sup.report_failure("svc", "OSError")
        assert sup.snapshot()["svc"]["failure_streak"] == 0
        maintenance.end_maintenance()
        sup.begin_shutdown()
        await sup.stop()  # the cancelled task is an orderly stop, not a crash
        return alerts

    assert asyncio.run(scenario()) == []


def test_failing_notifier_never_prevents_the_restart():
    async def scenario():
        sup, _clock, _alerts = _collecting()

        def broken(_alert):
            raise sqlite3.OperationalError("database is down")

        sup._notifier = broken
        stop = asyncio.Event()
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("x")
            await stop.wait()

        sup.add(ServiceSpec("svc", flaky, request_stop=stop.set))
        await _until(lambda: calls == 2)
        await sup.stop()
        return calls

    assert asyncio.run(scenario()) == 2


# ─── Sink and inbox ──────────────────────────────────────────────────────────


def test_crash_alert_reaches_the_admin_inbox_without_scheduler_or_spooler(db_path):
    async def scenario():
        sup, sink, _clock = _setup(db_path)
        stop = asyncio.Event()
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError(f"password={SECRET} /etc/secret/path")
            await stop.wait()

        sup.add(ServiceSpec("monitor_scheduler", flaky, request_stop=stop.set, label="Monitoring scheduler"))
        await _until(lambda: len(_rows(db_path)) == 1)
        await sup.stop()
        await sink.aclose()

    asyncio.run(scenario())
    (row,) = _rows(db_path)
    assert (row["category"], row["severity"], row["audience"]) == ("activity", "warning", "admins")
    assert row["title"] == "Monitoring scheduler crashed"
    assert "ValueError" in row["body"]
    assert SECRET not in f"{row['title']} {row['body']}" and "/etc" not in row["body"]


def test_identical_alerts_do_not_spam_and_recovery_is_a_single_row(db_path):
    sink = ServiceAlertSink(lambda: sqlite_db.connect(db_path))
    for _ in range(10):
        sink.write(_alert("crashed"))
    assert len(_rows(db_path)) == 1  # dedupe: one open incident per service
    conn = sqlite_db.connect(db_path)
    assert inbox.get_incident(conn, "supervisor.svc")["count"] == 10  # repeat counter
    sqlite_db.close_connection(conn)
    sink.write(_alert("recovered"))
    sink.write(_alert("recovered"))  # nothing open any more: no second recovery
    rows = _rows(db_path)
    assert [r["severity"] for r in rows] == ["warning", "success"]
    sink.write(_alert("crashed", at=datetime.now(UTC) + timedelta(seconds=5)))  # a new disturbance, a new row
    assert len(_rows(db_path)) == 3


def test_degraded_escalates_the_open_row_and_marks_it_unread_again(db_path):
    sink = ServiceAlertSink(lambda: sqlite_db.connect(db_path))
    conn = sqlite_db.connect(db_path)
    admin_id = sqlite_db.create_user(conn, username="root", password_hash="x", is_admin=True)  # noqa: S106
    admin = {"id": admin_id, "username": "root", "is_admin": 1}
    sink.write(_alert("crashed"))
    nid = _rows(db_path)[0]["id"]
    assert inbox.mark_read(conn, admin, nid)
    assert inbox.count_unread(conn, admin) == 0
    sink.write(_alert("degraded", reason="crash_loop", state="degraded"))
    (row,) = _rows(db_path)
    assert (row["severity"], row["title"]) == ("error", "Test service degraded")
    assert "degraded" in row["body"]
    assert inbox.count_unread(conn, admin) == 1
    sink.write(_alert("crashed", state="degraded"))  # later crashes keep the degraded wording
    assert _rows(db_path)[0]["title"] == "Test service degraded"
    sqlite_db.close_connection(conn)


def test_only_admins_see_system_alerts(db_path):
    sink = ServiceAlertSink(lambda: sqlite_db.connect(db_path))
    conn = sqlite_db.connect(db_path)
    admin = {"id": sqlite_db.create_user(conn, username="root", password_hash="x", is_admin=True), "is_admin": 1}  # noqa: S106
    user = {"id": sqlite_db.create_user(conn, username="bob", password_hash="x", is_admin=False), "is_admin": 0}  # noqa: S106
    sink.write(_alert("crashed"))
    (row,) = _rows(db_path)
    assert inbox.count_unread(conn, admin) == 1
    assert inbox.count_unread(conn, user) == 0
    admin_items, admin_total = inbox.list_notifications(conn, admin)
    user_items, user_total = inbox.list_notifications(conn, user)
    assert admin_total == 1 and admin_items[0]["id"] == row["id"]
    assert user_items == [] and user_total == 0
    assert inbox.get_notification(conn, user, row["id"]) is None
    sqlite_db.close_connection(conn)


def test_exception_type_is_sanitised(db_path):
    sink = ServiceAlertSink(lambda: sqlite_db.connect(db_path))
    sink.write(_alert("crashed", exc_type=f"/srv/app?token={SECRET}"))
    (row,) = _rows(db_path)
    assert SECRET not in row["body"] and "/srv" not in row["body"]


def test_alert_is_stored_after_the_database_returns(db_path):
    async def scenario():
        failures = 3
        slept: list[float] = []

        def connect():
            nonlocal failures
            if failures:
                failures -= 1
                raise sqlite3.OperationalError("unable to open database file")
            return sqlite_db.connect(db_path)

        async def no_sleep(delay: float) -> None:
            slept.append(delay)

        sink = ServiceAlertSink(connect, sleep=no_sleep)
        sink(_alert("crashed"))
        await _until(lambda: len(_rows(db_path)) == 1)
        await sink.aclose()
        return slept

    assert asyncio.run(scenario()) == [1.0, 2.0, 4.0]  # bounded exponential backoff


def test_pending_alerts_stay_bounded_and_are_dropped_after_the_attempt_limit():
    async def scenario():
        attempts: list[None] = []

        async def no_sleep(_delay: float) -> None:
            await asyncio.sleep(0)

        def down():
            attempts.append(None)
            raise sqlite3.OperationalError("down")

        sink = ServiceAlertSink(down, sleep=no_sleep, max_attempts=3)
        for i in range(80):
            sink(_alert("crashed", crashes_total=i))
        assert len(sink._pending) == sink._pending.maxlen
        expected_attempts = len(sink._pending) * 3
        await _until(lambda: not sink._pending, timeout=5)
        await sink.aclose()
        return len(attempts), expected_attempts

    attempts, expected_attempts = asyncio.run(scenario())
    assert attempts == expected_attempts


def test_stale_incident_of_a_previous_process_is_closed_after_the_first_stable_run(db_path):
    sink = ServiceAlertSink(lambda: sqlite_db.connect(db_path))
    sink.write(_alert("crashed", service="svc"))

    async def scenario():
        sup, sink2, clock = _setup(db_path)
        stop = asyncio.Event()

        async def runs():
            await stop.wait()

        sup.add(ServiceSpec("svc", runs, request_stop=stop.set, label="Test service"))
        await asyncio.sleep(0.05)
        assert len(_rows(db_path)) == 1  # not before the stability window
        sup.report_failure("svc", "RuntimeError")
        clock.now += 61
        await asyncio.sleep(0.05)
        assert len(_rows(db_path)) == 1  # elapsed time cannot recover an active failure series
        sup.report_success("svc")
        await _until(lambda: len(_rows(db_path)) == 2)
        await sup.stop()
        await sink2.aclose()

    asyncio.run(scenario())
    assert [r["severity"] for r in _rows(db_path)] == ["warning", "success"]


def test_failed_degraded_incident_update_is_not_acknowledged(db_path, monkeypatch):
    sink = ServiceAlertSink(lambda: sqlite_db.connect(db_path))
    sink.write(_alert("crashed"))
    monkeypatch.setattr(inbox, "update_incident", lambda *_args, **_kwargs: False)

    with pytest.raises(sqlite3.OperationalError, match="service alert was not persisted"):
        sink.write(_alert("degraded", reason="crash_loop", state="degraded"))

    assert _rows(db_path)[0]["severity"] == "warning"


# ─── Services reporting failures from inside their own loops ─────────────────


def test_scheduler_heartbeat_continues_while_iterations_fail(tmp_path, monkeypatch):
    async def scenario():
        sched = MonitorScheduler(get_conn=lambda: None, tsdb_dir=tmp_path, refresh_seconds=30)
        beats: list[int] = []
        failures: list[str] = []
        oks: list[int] = []
        loop_turns = 0
        outcomes = iter([RuntimeError(SECRET), RuntimeError(SECRET), None, None])

        async def iteration(_loop, _generation=None):
            outcome = next(outcomes, None)
            if isinstance(outcome, Exception):
                raise outcome
            return True

        async def idle():
            if len(beats) + len(failures) + len(oks) > 12:
                sched._stop.set()
            await asyncio.sleep(0)

        async def watch_loop_turns():
            nonlocal loop_turns
            while not sched._stop.is_set():
                loop_turns += 1
                await asyncio.sleep(0)

        sched.on_heartbeat = lambda: beats.append(1)
        sched.on_iteration_failed = failures.append
        sched.on_iteration_ok = lambda: oks.append(1)
        monkeypatch.setattr(sched, "_run_iteration", iteration)
        monkeypatch.setattr(sched, "_idle_sleep", idle)
        watcher = asyncio.create_task(watch_loop_turns())
        try:
            await asyncio.wait_for(sched._run_loop(), timeout=5)
        finally:
            sched._stop.set()
            await asyncio.wait_for(watcher, timeout=1)
        return beats, failures, oks, loop_turns

    beats, failures, oks, loop_turns = asyncio.run(scenario())
    assert failures == ["RuntimeError", "RuntimeError"]  # class name only
    assert oks  # a successful iteration ends the series
    assert len(beats) == len(failures) + len(oks)
    assert loop_turns > 0  # idle yields so watchdogs and bounded waits can run


def test_release_checker_reports_iteration_outcomes():
    seen: list[tuple[str, str | None]] = []
    rc = ReleaseChecker(
        lambda: None,
        on_failure=lambda error: seen.append(("failure", error)),
        on_success=lambda: seen.append(("success", None)),
    )
    outcomes = [
        ("release", "ReleaseCheckError", [("success", None)]),
        ("none", "ReleaseCheckError", [("success", None)]),
        ("not_modified", "ReleaseCheckError", [("success", None)]),
        ("rate_limited", "ReleaseCheckError", []),
        ("error", "ReleaseCheckError", [("failure", "ReleaseCheckError")]),
        ("skipped", "ReleaseCheckError", []),
        ("busy", "ReleaseCheckError", []),
        ("maintenance", "ReleaseCheckError", []),
        ("stopped", "ReleaseCheckError", []),
        ("crashed", "OperationalError", [("failure", "OperationalError")]),
    ]
    for status, exc_type, expected in outcomes:
        seen.clear()
        rc._report(status, exc_type)
        assert seen == expected, f"unexpected supervisor outcome for {status}"
