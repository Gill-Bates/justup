#!/usr/bin/env python3
#
# tests/test_pop_auto_refresh.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001, ARG005
"""Self-managing POP refresh: state-based scheduling driven by the pop_import_runs history.

Covers the required scenarios without any real external API call: the HTTP client, the clock and
the wait times are simulated in a controlled way. The scheduling decision is a pure function of the
persisted state, so each scenario asserts the decision the scheduler would reach after a (re)start.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.db import pop_locations
from app.db import sqlite as sqlite_db
from app.services import pop_refresh
from app.utils import maintenance
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect

SOURCE = pop_refresh.SOURCE_PEERINGDB


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def conn(monkeypatch) -> sqlite3.Connection:
    # check_same_thread=False mirrors production so a worker-thread refresh can use this connection.
    c = sqlite3.connect(":memory:", check_same_thread=False)
    sqlite_db.init_schema(c)
    c.row_factory = sqlite3.Row

    @contextmanager
    def _same(_get_conn):
        yield c

    monkeypatch.setattr(pop_refresh, "_db_connection", _same)
    monkeypatch.setattr(pop_locations, "MIN_PROCESSED_FOR_RETIRE", 1)
    monkeypatch.setattr(pop_locations, "MAX_RETIRE_PERCENT", 100)
    maintenance.end_maintenance()
    monkeypatch.setattr(maintenance, "_GENERATION", 0, raising=False)
    return c


def _seeded_rng() -> random.Random:
    return random.Random(1234)  # noqa: S311 - deterministic jitter for tests, not security


def _insert_run(conn: sqlite3.Connection, *, status: str, started, finished=None, error=None) -> None:
    """Insert a terminal pop_import_runs row at controlled timestamps."""
    run_id = f"{SOURCE}_{started.isoformat()}_{random.randrange(1 << 30)}"  # noqa: S311 - unique test id, not security
    conn.execute(
        """
        INSERT INTO pop_import_runs (run_id, source, started_at, finished_at, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, SOURCE, started.isoformat(), finished.isoformat() if finished else None, status, error),
    )
    conn.commit()


def _facs(n: int = 2) -> dict:
    city = next(iter(pop_refresh.CITY_TO_POP_CODE))
    return {"data": [{"city": city, "country": "DE", "latitude": 50.1, "longitude": 8.6} for _ in range(n)]}


# --------------------------------------------------------------------------- #
# compute_next_action: pure decision (scenarios 1-7)
# --------------------------------------------------------------------------- #


def test_first_install_without_external_data_attempts_soon():
    """Scenario 1: no successful external import yet -> short startup jitter, not a rigid wait."""
    now = utcnow()
    plan = pop_refresh.compute_next_action(
        now=now, last_success_at=None, last_attempt_at=None, consecutive_failures=0, rng=_seeded_rng()
    )
    assert 0 < plan.delay_seconds <= pop_refresh.POP_STARTUP_JITTER_MAX_SECONDS
    assert plan.delay_seconds >= pop_refresh.POP_STARTUP_JITTER_MIN_SECONDS


def test_fresh_data_waits_until_freshness_window():
    """Scenario 2/3: a recent success means no download; wait roughly until the window expires."""
    now = utcnow()
    last_success = now - timedelta(days=5)
    plan = pop_refresh.compute_next_action(
        now=now, last_success_at=last_success, last_attempt_at=last_success, consecutive_failures=0, rng=_seeded_rng()
    )
    remaining = pop_refresh.POP_FRESHNESS_SECONDS - 5 * 24 * 3600
    assert plan.delay_seconds >= remaining
    assert plan.delay_seconds <= remaining + pop_refresh.POP_SCHEDULE_JITTER_SECONDS


def test_restart_with_still_valid_data_does_not_download():
    """Scenario 3: a restart does not reset the schedule; a still-fresh import stays deferred."""
    now = utcnow()
    last_success = now - timedelta(days=1)  # just restarted, data refreshed yesterday
    plan = pop_refresh.compute_next_action(
        now=now, last_success_at=last_success, last_attempt_at=last_success, consecutive_failures=0, rng=_seeded_rng()
    )
    assert plan.delay_seconds > 0  # not due; no download on restart


def test_restart_after_freshness_expired_is_due_now():
    """Scenario 4: once the 30-day window passed, a refresh is due immediately (catch up overdue)."""
    now = utcnow()
    last_success = now - timedelta(days=40)
    plan = pop_refresh.compute_next_action(
        now=now, last_success_at=last_success, last_attempt_at=last_success, consecutive_failures=0, rng=_seeded_rng()
    )
    assert plan.delay_seconds == 0
    assert "expired" in plan.reason


def test_backoff_grows_with_consecutive_failures():
    """Scenario 5: after failures, the next attempt waits the exponential backoff from last attempt."""
    now = utcnow()
    last_attempt = now  # attempt just failed
    one = pop_refresh.compute_next_action(
        now=now, last_success_at=None, last_attempt_at=last_attempt, consecutive_failures=1, rng=_seeded_rng()
    )
    three = pop_refresh.compute_next_action(
        now=now, last_success_at=None, last_attempt_at=last_attempt, consecutive_failures=3, rng=_seeded_rng()
    )
    assert one.delay_seconds == pytest.approx(pop_refresh.POP_BACKOFF_BASE_SECONDS, rel=0.01)
    assert three.delay_seconds > one.delay_seconds
    assert "backoff" in one.reason


def test_backoff_is_capped_at_max():
    """Scenario 5: backoff never exceeds the 24h cap even after many failures."""
    now = utcnow()
    plan = pop_refresh.compute_next_action(
        now=now, last_success_at=None, last_attempt_at=now, consecutive_failures=50, rng=_seeded_rng()
    )
    assert plan.delay_seconds <= pop_refresh.POP_BACKOFF_MAX_SECONDS


def test_backoff_elapsed_since_last_attempt_is_due():
    """Scenario 7: repeated restarts during an outage resume the backoff from the persisted attempt.

    After a restart, the wait is backoff minus the time already elapsed since the last attempt, so
    restarts do not reset the timer into a tight download loop, yet an overdue retry fires promptly.
    """
    now = utcnow()
    # One failure, backoff is 15 min, but 20 min already elapsed since the attempt -> retry is due.
    last_attempt = now - timedelta(minutes=20)
    plan = pop_refresh.compute_next_action(
        now=now, last_success_at=None, last_attempt_at=last_attempt, consecutive_failures=1, rng=_seeded_rng()
    )
    assert plan.delay_seconds == 0
    assert "retry" in plan.reason

    # A restart only 5 min into the 15-min backoff must still wait the remaining ~10 min.
    plan2 = pop_refresh.compute_next_action(
        now=now,
        last_success_at=None,
        last_attempt_at=now - timedelta(minutes=5),
        consecutive_failures=1,
        rng=_seeded_rng(),
    )
    assert plan2.delay_seconds == pytest.approx(10 * 60, abs=1)


# --------------------------------------------------------------------------- #
# plan_next_refresh: decision from the persisted DB state (scenarios 1-4, 7)
# --------------------------------------------------------------------------- #


def test_plan_from_db_no_runs_is_first_install(conn):
    """Scenario 1: an empty import history is treated as a first install."""
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert 0 < plan.delay_seconds <= pop_refresh.POP_STARTUP_JITTER_MAX_SECONDS


def test_plan_from_db_recent_success_defers(conn):
    """Scenario 2/3: a recent completed run in the DB defers the next refresh."""
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=2), finished=now - timedelta(days=2))
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert plan.delay_seconds > 0


def test_plan_from_db_old_success_is_due(conn):
    """Scenario 4: an expired completed run makes the refresh due now."""
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=40), finished=now - timedelta(days=40))
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert plan.delay_seconds == 0


def test_plan_from_db_failures_back_off(conn):
    """Scenario 7: a failure streak in the DB drives the scheduler into backoff after a restart."""
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=40), finished=now - timedelta(days=40))
    _insert_run(
        conn, status="failed", started=now - timedelta(minutes=1), finished=now - timedelta(minutes=1), error="net"
    )
    assert pop_locations.count_consecutive_failures(conn, SOURCE) == 1
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert plan.delay_seconds > 0
    assert "backoff" in plan.reason


def test_consecutive_failures_reset_on_success(conn):
    """A later success resets the streak, so the next plan leaves backoff."""
    now = utcnow()
    _insert_run(conn, status="failed", started=now - timedelta(hours=3), finished=now - timedelta(hours=3), error="x")
    _insert_run(conn, status="failed", started=now - timedelta(hours=2), finished=now - timedelta(hours=2), error="x")
    _insert_run(conn, status="completed", started=now - timedelta(hours=1), finished=now - timedelta(hours=1))
    assert pop_locations.count_consecutive_failures(conn, SOURCE) == 0


# --------------------------------------------------------------------------- #
# Network / rate-limit behavior (scenarios 5, 6, 7)
# --------------------------------------------------------------------------- #


def test_network_error_persists_failed_run_and_keeps_data(conn, monkeypatch):
    """Scenario 5: a fetch network error records a failed run (so backoff survives) and loses no data."""
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence,"
        " priority, created_at, updated_at, retired) VALUES ('fra','fra','Frankfurt','DE',50.1,8.6,'seed',100,0,"
        "'x','x',0)"
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM pop_locations WHERE retired = 0").fetchone()[0]

    def _boom(_url):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", _boom)
    res = pop_refresh.refresh_from_peeringdb(lambda: conn)

    assert res.success is False
    assert "network" in (res.error or "")
    # Failed attempt persisted so the scheduler can back off after a restart.
    assert conn.execute("SELECT COUNT(*) FROM pop_import_runs WHERE status='failed'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pop_import_runs WHERE status='completed'").fetchone()[0] == 0
    # Last valid data preserved.
    assert conn.execute("SELECT COUNT(*) FROM pop_locations WHERE retired = 0").fetchone()[0] == before
    assert pop_locations.count_consecutive_failures(conn, SOURCE) == 1


def test_rate_limit_429_is_honored_with_retry_after(monkeypatch):
    """Scenario 6: a 429 response is retried after the Retry-After delay, then succeeds."""
    import httpx

    sleeps: list[float] = []
    monkeypatch.setattr(pop_refresh.time, "sleep", sleeps.append)

    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, headers=None, payload=None):
            self.status_code = status
            self.headers = headers or {}
            self._payload = payload or {}
            self.content = b"{}"
            self.request = None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, _url):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(429, headers={"retry-after": "7"})
            return _Resp(200, payload={"data": []})

    monkeypatch.setattr(httpx, "Client", _Client)
    result = pop_refresh._fetch_with_retry("https://example.invalid/api")
    assert result == {"data": []}
    assert calls["n"] == 2
    # The Retry-After value governed the wait before the retry.
    assert 7 in sleeps


def test_rate_limit_persists_failure_when_exhausted(conn, monkeypatch):
    """Scenario 6: a sustained 429 ends as a failed attempt (not a success), feeding backoff."""

    def _rate_limited(_url):
        raise RuntimeError("Rate limited after 3 retries")

    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", _rate_limited)
    res = pop_refresh.refresh_from_peeringdb(lambda: conn)
    assert res.success is False
    assert pop_locations.count_consecutive_failures(conn, SOURCE) == 1


# --------------------------------------------------------------------------- #
# Concurrency, shutdown, maintenance (scenarios 8, 9, 10)
# --------------------------------------------------------------------------- #


def test_parallel_refresh_is_skipped_not_duplicated(conn, monkeypatch):
    """Scenario 8: a manual refresh while one is running is refused by the per-source lock."""
    lock = pop_refresh._get_source_lock(SOURCE)
    assert lock.acquire(blocking=False)
    try:
        res = pop_refresh.refresh_from_peeringdb(lambda: conn)
    finally:
        lock.release()
    assert res.error == "already_running"
    # The refused attempt wrote no run at all.
    assert conn.execute("SELECT COUNT(*) FROM pop_import_runs").fetchone()[0] == 0


def test_refresh_deferred_while_restore_active(conn, monkeypatch):
    """Scenarios 9/10: with maintenance (restore/backup) active, a refresh defers and writes nothing."""
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs())
    assert maintenance.begin_maintenance(timeout=0.5)
    try:
        res = pop_refresh.refresh_from_peeringdb(lambda: conn)
    finally:
        maintenance.end_maintenance()
    assert res.error == "maintenance_in_progress"
    assert conn.execute("SELECT COUNT(*) FROM pop_import_runs").fetchone()[0] == 0


def test_shutdown_during_running_refresh_defers_via_stale_generation(conn, monkeypatch):
    """Scenario 9: a restore that completes mid-fetch makes the in-flight import defer as stale."""
    entered = threading.Event()
    release = threading.Event()
    results = []

    def _fetch(_url):
        entered.set()
        assert release.wait(5)
        return _facs()

    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", _fetch)
    worker = threading.Thread(target=lambda: results.append(pop_refresh.refresh_from_peeringdb(lambda: conn)))
    worker.start()
    try:
        assert entered.wait(5)
        assert maintenance.begin_maintenance(timeout=0.5)
        maintenance.confirm_data_replaced()
        maintenance.end_maintenance()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0].error == "maintenance_in_progress"
    assert conn.execute("SELECT COUNT(*) FROM pop_import_runs").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Incomplete import, upgrade, UN/LOCODE (scenarios 11, 12, 13)
# --------------------------------------------------------------------------- #


def test_incomplete_import_flags_partial_and_keeps_data(conn, monkeypatch):
    """Scenario 11: a truncated/corrupted snapshot is flagged partial and does not retire data."""
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence,"
        " priority, created_at, updated_at, import_run_id, retired) VALUES ('zzz','zzz','Old','DE',1,1,"
        "'peeringdb',50,1,'x','x','old-run',0)"
    )
    conn.commit()
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs(3))
    monkeypatch.setattr(pop_refresh, "MAX_RECORDS_PER_RUN", 1)
    res = pop_refresh.refresh_from_peeringdb(lambda: conn)
    assert res.error and "partial" in res.error
    assert res.retired == 0
    # The pre-existing row is not retired because the snapshot was incomplete.
    assert conn.execute("SELECT retired FROM pop_locations WHERE alias_code='zzz'").fetchone()[0] == 0


def test_upgrade_removes_old_stored_settings(tmp_path, monkeypatch):
    """Scenario 12: upgrading a DB that still holds the old POP refresh settings cleans them up.

    An older database is simulated at the pre-v3 schema version with the two obsolete setting rows;
    init_schema's migration path must delete them so they have no effect after the upgrade.
    """
    db_path = tmp_path / "old.db"
    c = sqlite_db.connect(db_path)
    sqlite_db.init_schema(c)
    # Simulate a pre-removal install: stored values plus an older schema stamp.
    sqlite_db.set_setting(c, "pop_refresh_interval_seconds", 604800)
    sqlite_db.set_setting(c, "pop_refresh_initial_delay_seconds", 60)
    c.execute("PRAGMA user_version = 2")
    c.commit()
    sqlite_db.close_connection(c)

    # Reopen: init_schema applies the v3 migration which removes the obsolete rows.
    c2 = sqlite_db.connect(db_path)
    sqlite_db.init_schema(c2)
    try:
        assert sqlite_db.get_schema_version(c2) >= 3
        assert sqlite_db.get_setting(c2, "pop_refresh_interval_seconds", None) is None
        assert sqlite_db.get_setting(c2, "pop_refresh_initial_delay_seconds", None) is None
    finally:
        sqlite_db.close_connection(c2)


def test_fresh_install_has_no_obsolete_settings(conn):
    """Scenario 12: a fresh database never carries the obsolete settings."""
    assert sqlite_db.get_setting(conn, "pop_refresh_interval_seconds", None) is None
    assert sqlite_db.get_setting(conn, "pop_refresh_initial_delay_seconds", None) is None


def test_auto_scheduler_does_not_run_unlocode_placeholder(conn, monkeypatch):
    """Scenario 13: the automatic scheduler refreshes PeeringDB only, never the UN/LOCODE placeholder."""
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs())
    results = pop_refresh.refresh_external_sources(lambda: conn)
    sources = {r.source for r in results}
    assert sources == {SOURCE}
    assert pop_refresh.SOURCE_UNLOCODE not in sources
    # No UN/LOCODE run was created by the automatic path.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM pop_import_runs WHERE source = ?", (pop_refresh.SOURCE_UNLOCODE,)
        ).fetchone()[0]
        == 0
    )


def test_unlocode_placeholder_not_counted_as_external_success(conn, monkeypatch):
    """Scenario 13: even a manual UN/LOCODE run does not become the scheduler's 'last success'."""
    # A manual UN/LOCODE refresh records a completed placeholder run with zero records.
    pop_refresh.refresh_from_unlocode(lambda: conn)
    # The scheduler keys off PeeringDB only, so its last-success state is still empty.
    state = pop_refresh.get_scheduler_state(conn)
    assert state["last_success_at"] is None
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert "first install" in plan.reason
