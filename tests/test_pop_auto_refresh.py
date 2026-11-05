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

import json
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


def _facility(city: str, country: str, lat: float | None, lon: float | None) -> dict:
    return {"city": city, "country": country, "latitude": lat, "longitude": lon}


def test_peeringdb_city_country_and_facility_order_are_deterministic(conn) -> None:
    pop_locations.upsert_pop_location(conn, "fra", "fra", "Old", "DE", 49, 8, "peeringdb", 70, 10)
    records = [
        _facility("Frankfurt", "US", 40, -70),
        _facility("Frankfurt", "DE", 50.10, 8.60),
        _facility("Frankfurt", "DE", 50.12, 8.62),
    ]
    first = pop_refresh._apply_peeringdb_snapshot(lambda: conn, records)
    row = conn.execute(
        "SELECT city, country, lat, lon, confidence, updated_at FROM pop_locations WHERE alias_code='fra'"
    ).fetchone()
    city = conn.execute("SELECT city, country, lat, lon, facility_count, updated_at FROM pop_city_evidence").fetchone()
    second = pop_refresh._apply_peeringdb_snapshot(lambda: conn, list(reversed(records)))
    assert first.error is None and second.error is None
    assert (first.processed, first.inserted, first.updated) == (2, 1, 0)
    assert (second.processed, second.inserted, second.updated) == (2, 0, 0)
    assert tuple(
        conn.execute(
            "SELECT city, country, lat, lon, confidence, updated_at FROM pop_locations WHERE alias_code='fra'"
        ).fetchone()
    ) == tuple(row)
    assert tuple(
        conn.execute("SELECT city, country, lat, lon, facility_count, updated_at FROM pop_city_evidence").fetchone()
    ) == tuple(city)
    assert tuple(row[:5]) == ("Old", "DE", 49.0, 8.0, 70)
    assert tuple(city[:5]) == ("Frankfurt", "DE", 50.11, 8.61, 2)
    assert conn.execute("SELECT COUNT(*) FROM pop_city_evidence WHERE country='US'").fetchone()[0] == 0
    changed = pop_refresh._apply_peeringdb_snapshot(lambda: conn, [_facility("Frankfurt", "DE", 50.15, 8.65)])
    assert (changed.processed, changed.inserted, changed.updated) == (1, 0, 1)


def test_peeringdb_never_invents_alias_or_overwrites_seed(conn) -> None:
    pop_locations.upsert_pop_location(conn, "fra", "fra", "Frankfurt", "DE", 50.1, 8.6, "seed", 100, 0)
    before = tuple(conn.execute("SELECT * FROM pop_locations WHERE alias_code='fra'").fetchone())
    result = pop_refresh._apply_peeringdb_snapshot(lambda: conn, [_facility("Frankfurt", "DE", 50.2, 8.7)])
    assert result.error is None and result.inserted == 1
    assert tuple(conn.execute("SELECT * FROM pop_locations WHERE alias_code='fra'").fetchone()) == before
    assert conn.execute("SELECT position_source FROM pop_city_evidence WHERE city_code='fra'").fetchone()[0] == "seed"


def test_peeringdb_unseeded_city_is_evidence_not_hostname_alias(conn) -> None:
    result = pop_refresh._apply_peeringdb_snapshot(lambda: conn, [_facility("Lyon", "FR", 45.76, 4.84)])
    assert result.error is None and result.inserted == 1
    assert conn.execute("SELECT COUNT(*) FROM pop_locations").fetchone()[0] == 0
    assert tuple(conn.execute("SELECT city_code, country, position_source FROM pop_city_evidence").fetchone()) == (
        "lyo",
        "FR",
        "facility-centroid",
    )


def test_peeringdb_refresh_preserves_existing_quality_metadata(conn) -> None:
    pop_locations.upsert_pop_location(conn, "fra", "fra", "Old", "DE", 49, 8, "peeringdb", 92, 4)
    pop_refresh._apply_peeringdb_snapshot(lambda: conn, [_facility("Frankfurt", "DE", 50.1, 8.6)])
    assert tuple(
        conn.execute("SELECT source, confidence, priority FROM pop_locations WHERE alias_code='fra'").fetchone()
    ) == ("peeringdb", 92, 4)


def test_peeringdb_skips_missing_invalid_and_conflicting_positions(conn) -> None:
    pop_locations.upsert_pop_location(conn, "fra", "fra", "Old", "DE", 49, 8, "peeringdb", 70, 10)
    result = pop_refresh._apply_peeringdb_snapshot(
        lambda: conn,
        [
            _facility("Frankfurt", "DE", None, 8.6),
            _facility("Frankfurt", "DE", 999, 8.6),
            _facility("Frankfurt", "DE", 50.1, 8.6),
            _facility("Frankfurt", "DE", 51.1, 8.6),
        ],
    )
    assert result.validation_errors == 2
    assert result.skipped >= 2
    assert conn.execute("SELECT lat FROM pop_locations WHERE alias_code='fra'").fetchone()[0] == 49
    assert conn.execute("SELECT COUNT(*) FROM pop_city_evidence").fetchone()[0] == 0


def test_peeringdb_skips_facilities_far_from_curated_city(conn) -> None:
    pop_locations.upsert_pop_location(conn, "fra", "fra", "Frankfurt", "DE", 50.1, 8.6, "seed", 100, 0)
    result = pop_refresh._apply_peeringdb_snapshot(lambda: conn, [_facility("Frankfurt", "DE", 51.5, 8.6)])
    assert result.error is None and result.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM pop_city_evidence").fetchone()[0] == 0


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
    _insert_ixp_snapshot(conn)  # an existing snapshot isolates the freshness-window expiry
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
            self.request = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            yield json.dumps(self._payload).encode()

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, _method, _url):
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


# --------------------------------------------------------------------------- #
# IXP snapshot after an upgrade: a fresh facility import must not defer the first IXP import
# --------------------------------------------------------------------------- #


def _insert_ixp_snapshot(conn: sqlite3.Connection, *, prefixes: int = 3, completed_at=None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ixp_snapshot (id, import_run_id, completed_at, exchange_count, prefix_count, source)"
        " VALUES (1, 'run', ?, 2, ?, 'peeringdb_ixp')",
        ((completed_at or utcnow()).isoformat(), prefixes),
    )
    conn.commit()


def test_upgrade_without_ixp_snapshot_schedules_the_first_ixp_import_soon(conn):
    """A facility import from yesterday is fresh, but the new IXP tables are still empty."""
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=1), finished=now - timedelta(days=1))
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert plan.delay_seconds <= pop_refresh.POP_STARTUP_JITTER_MAX_SECONDS
    assert "IXP" in plan.reason


@pytest.mark.parametrize("prefix_count", [0, None])
def test_empty_or_absent_ixp_snapshot_counts_as_missing(conn, prefix_count):
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=1), finished=now - timedelta(days=1))
    if prefix_count is not None:
        _insert_ixp_snapshot(conn, prefixes=prefix_count)
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert plan.delay_seconds <= pop_refresh.POP_STARTUP_JITTER_MAX_SECONDS
    assert "IXP" in plan.reason


def test_present_ixp_snapshot_keeps_the_normal_freshness_window(conn):
    """No repeated refresh while both data sets are inside their interval."""
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=1), finished=now - timedelta(days=1))
    _insert_ixp_snapshot(conn)
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert plan.delay_seconds > 20 * 24 * 3600
    assert "IXP" not in plan.reason


def test_missing_ixp_snapshot_does_not_bypass_the_failure_backoff(conn):
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=1), finished=now - timedelta(days=1))
    _insert_run(
        conn, status="failed", started=now - timedelta(minutes=1), finished=now - timedelta(minutes=1), error="x"
    )
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert "backoff" in plan.reason
    assert plan.delay_seconds > pop_refresh.POP_BACKOFF_BASE_SECONDS - 120


def test_missing_ixp_snapshot_never_retries_faster_than_the_base_backoff(conn):
    """A run that ends without an error but without a snapshot must not become a download loop."""
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(minutes=2), finished=now - timedelta(minutes=2))
    plan = pop_refresh.plan_next_refresh(conn, rng=_seeded_rng())
    assert plan.delay_seconds >= pop_refresh.POP_BACKOFF_BASE_SECONDS - 3 * 60
    assert "IXP" in plan.reason


def test_status_reports_the_ixp_snapshot_and_the_ixp_reason(conn):
    now = utcnow()
    _insert_run(conn, status="completed", started=now - timedelta(days=1), finished=now - timedelta(days=1))
    status = pop_refresh.get_refresh_status(lambda: conn)
    assert status["ixp_snapshot"] is None
    assert "IXP" in status["auto_refresh"]["next_attempt_reason"]
    _insert_ixp_snapshot(conn)
    status = pop_refresh.get_refresh_status(lambda: conn)
    assert status["ixp_snapshot"]["prefix_count"] == 3
    assert "IXP" not in status["auto_refresh"]["next_attempt_reason"]
