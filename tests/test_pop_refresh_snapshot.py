#!/usr/bin/env python3
#
# tests/test_pop_refresh_snapshot.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG001, ARG005
"""PeeringDB refresh: retirement only after a complete source snapshot."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest

from app.db import pop_locations
from app.db import sqlite as sqlite_db
from app.services import pop_refresh
from app.utils import maintenance
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _city() -> tuple[str, str]:
    city = next(iter(pop_refresh.CITY_TO_POP_CODE))
    return city, pop_refresh.CITY_TO_POP_CODE[city]


@pytest.fixture
def conn(monkeypatch) -> sqlite3.Connection:
    # check_same_thread=False mirrors the production connection (app/db/sqlite.py) so the
    # worker-thread test can drive _do_peeringdb_refresh against this shared connection.
    c = sqlite3.connect(":memory:", check_same_thread=False)
    sqlite_db.init_schema(c)
    c.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source, confidence,"
        " priority, created_at, updated_at, import_run_id, retired) VALUES ('ZZZ','ZZZ','Old','DE',1,1,"
        "'peeringdb',50,1,'x','x','old-run',0)"
    )
    c.commit()

    @contextmanager
    def _same(_get_conn):
        yield c

    monkeypatch.setattr(pop_refresh, "_db_connection", _same)
    monkeypatch.setattr(pop_locations, "MIN_PROCESSED_FOR_RETIRE", 1)
    monkeypatch.setattr(pop_locations, "MAX_RETIRE_PERCENT", 100)
    return c


def _facs(n: int = 2) -> dict:
    city, _ = _city()
    return {"data": [{"city": city, "country": "DE", "latitude": 50.1, "longitude": 8.6} for _ in range(n)]}


def _retired(c) -> int:
    return c.execute("SELECT retired FROM pop_locations WHERE alias_code='ZZZ'").fetchone()[0]


def test_full_snapshot_retires_unseen(conn, monkeypatch) -> None:
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs())
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error is None
    assert _retired(conn) == 1


def test_record_limit_skips_retire(conn, monkeypatch) -> None:
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs(3))
    monkeypatch.setattr(pop_refresh, "MAX_RECORDS_PER_RUN", 1)
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error and "partial" in res.error
    assert res.processed == 1
    assert res.retired == 0
    assert _retired(conn) == 0


def test_alias_limit_skips_retire(conn, monkeypatch) -> None:
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs(3))
    monkeypatch.setattr(pop_refresh, "MAX_NEW_ALIASES_PER_RUN", 1)
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error and "partial" in res.error
    assert _retired(conn) == 0


def test_db_error_skips_retire(conn, monkeypatch) -> None:
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs())

    def _boom(*a, **k):
        raise sqlite3.OperationalError("disk")

    monkeypatch.setattr(pop_locations, "upsert_pop_location", _boom)
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error and "partial" in res.error
    assert _retired(conn) == 0


def test_network_fetch_does_not_hold_restore_and_stale_import_is_deferred(conn, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    results = []

    def _fetch(_url):
        entered.set()
        assert release.wait(5)
        return _facs()

    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", _fetch)
    maintenance.end_maintenance()
    worker = threading.Thread(target=lambda: results.append(pop_refresh._do_peeringdb_refresh(lambda: conn)))
    worker.start()
    try:
        assert entered.wait(5)
        # The external fetch is not counted as data-directory work, so a restore can begin while
        # it is still in flight; begin_maintenance() must not block on the fetching worker.
        assert maintenance.begin_maintenance(timeout=0.5)
        # Simulate a successful restore that replaced the data. This advances the data generation,
        # so the snapshot the worker captured before the fetch is now stale.
        maintenance.confirm_data_replaced()
        maintenance.end_maintenance()
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    # The import ran against a pre-restore snapshot, so work_section() rejects it as stale and the
    # refresh defers instead of writing stale rows over the restored data.
    assert results[0].error == "maintenance_in_progress"
    assert conn.execute("SELECT COUNT(*) FROM pop_import_runs").fetchone()[0] == 0


def test_run_full_refresh_returns_result_per_source_on_success(conn, monkeypatch) -> None:
    # Regression: refresh_from_unlocode() used to fall through to None on the no-error
    # path, so run_full_refresh() raised AttributeError reading .processed on that source.
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs())
    maintenance.end_maintenance()
    monkeypatch.setattr(maintenance, "_GENERATION", 0, raising=False)

    results = pop_refresh.run_full_refresh(lambda: conn)

    assert len(results) == 2
    assert all(isinstance(r, pop_refresh.PopRefreshResult) for r in results)

    by_source = {r.source: r for r in results}
    peeringdb = by_source[pop_refresh.SOURCE_PEERINGDB]
    unlocode = by_source[pop_refresh.SOURCE_UNLOCODE]

    assert peeringdb.error is None
    assert peeringdb.processed >= 1

    # UN/LOCODE is a placeholder import: it must still return a valid result object,
    # not None, with a reachable success return.
    assert unlocode.error is None
    assert unlocode.processed == 0
    assert unlocode.run_id
