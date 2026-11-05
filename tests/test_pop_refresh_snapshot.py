#!/usr/bin/env python3
#
# tests/test_pop_refresh_snapshot.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG005
"""PeeringDB city evidence never retires historical hostname aliases."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import httpx
import pytest

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
    # These tests cover POP city evidence; the IXP snapshot has its own tests (test_ixp_geolocation.py).
    monkeypatch.setattr(pop_refresh, "_fetch_ixp_snapshot", lambda facilities: None)
    return c


def _facs(n: int = 2) -> dict:
    city, _ = _city()
    return {"data": [{"city": city, "country": "DE", "latitude": 50.1, "longitude": 8.6} for _ in range(n)]}


def _retired(c) -> int:
    return c.execute("SELECT retired FROM pop_locations WHERE alias_code='ZZZ'").fetchone()[0]


def test_full_snapshot_keeps_unseen_hostname_alias(conn, monkeypatch) -> None:
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs())
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error is None
    assert _retired(conn) == 0


def test_record_limit_skips_retire(conn, monkeypatch) -> None:
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs(3))
    monkeypatch.setattr(pop_refresh, "MAX_RECORDS_PER_RUN", 1)
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error and "partial" in res.error
    assert res.processed == 0  # oversized snapshots are rejected before order-dependent selection
    assert res.retired == 0
    assert _retired(conn) == 0


def test_city_group_limit_skips_retire(conn, monkeypatch) -> None:
    monkeypatch.setattr(
        pop_refresh,
        "_fetch_with_retry",
        lambda url: {
            "data": [
                {"city": "Frankfurt", "country": "DE", "latitude": 50.1, "longitude": 8.6},
                {"city": "Berlin", "country": "DE", "latitude": 52.5, "longitude": 13.4},
            ]
        },
    )
    monkeypatch.setattr(pop_refresh, "MAX_CITY_GROUPS_PER_RUN", 1)
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error and "partial" in res.error
    assert _retired(conn) == 0


def test_db_error_skips_retire(conn, monkeypatch) -> None:
    monkeypatch.setattr(pop_refresh, "_fetch_with_retry", lambda url: _facs())
    conn.execute("DROP TABLE pop_city_evidence")
    conn.commit()
    res = pop_refresh._do_peeringdb_refresh(lambda: conn)
    assert res.error and "database" in res.error
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


# ---------------------------------------------------------------------------
# validate_pop_record coordinate handling
# ---------------------------------------------------------------------------


def _validate(lat, lon):
    return pop_refresh.validate_pop_record("fra", "fra", "Frankfurt", "DE", lat, lon)


@pytest.mark.parametrize("lat,lon", [(True, 8.0), (50.0, False), (True, True)])
def test_validate_rejects_boolean_coordinates(lat, lon) -> None:
    result = _validate(lat, lon)
    assert not result.valid
    assert "numeric" in result.error


@pytest.mark.parametrize("lat,lon", [("", 8.0), (50.0, "nan"), ("inf", 8.0), (95.0, 8.0)])
def test_validate_rejects_non_finite_or_empty_coordinates(lat, lon) -> None:
    assert not _validate(lat, lon).valid


@pytest.mark.parametrize("lat,lon", [(50.11, 8.68), ("50.11", "8.68"), (0, 0)])
def test_validate_accepts_numbers_and_numeric_strings(lat, lon) -> None:
    result = _validate(lat, lon)
    assert result.valid
    assert result.lat == float(lat) and result.lon == float(lon)


# ---------------------------------------------------------------------------
# _fetch_with_retry: bounded streaming reads
# ---------------------------------------------------------------------------


class _Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, counter):
        self._chunks = chunks
        self._counter = counter

    def __iter__(self):
        for chunk in self._chunks:
            self._counter.append(len(chunk))
            yield chunk


@pytest.fixture
def fetch_env(monkeypatch):
    """Route httpx.Client through a MockTransport and skip real sleeping."""
    state = {"calls": 0, "read": []}
    real_client = httpx.Client

    def install(handler):
        def factory(*args, **kwargs):
            def counted(request):
                state["calls"] += 1
                return handler(request, state)

            kwargs["transport"] = httpx.MockTransport(counted)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)

    monkeypatch.setattr(pop_refresh.time, "sleep", lambda _s: None)
    return install, state


def test_fetch_rejects_oversized_body_without_content_length(fetch_env) -> None:
    install, state = fetch_env
    chunks = [b"x" * 10] * 100
    install(lambda req, st: httpx.Response(200, stream=_Chunks(chunks, st["read"])))
    with pytest.raises(RuntimeError, match="too large"):
        pop_refresh._fetch_with_retry("https://example.test/x", max_response_bytes=25)
    assert sum(state["read"]) < 1000, "reading stopped at the limit instead of buffering everything"


def test_fetch_rejects_oversized_content_length_before_reading(fetch_env) -> None:
    install, state = fetch_env
    install(
        lambda req, st: httpx.Response(200, headers={"content-length": "9999"}, stream=_Chunks([b"{}"], st["read"]))
    )
    with pytest.raises(RuntimeError, match="too large"):
        pop_refresh._fetch_with_retry("https://example.test/x", max_response_bytes=100)
    assert state["read"] == []


def test_fetch_accepts_body_exactly_at_limit(fetch_env) -> None:
    install, _ = fetch_env
    body = b'{"a": 1}'
    install(lambda req, st: httpx.Response(200, content=body))
    assert pop_refresh._fetch_with_retry("https://example.test/x", max_response_bytes=len(body)) == {"a": 1}


def test_fetch_4xx_reports_only_a_bounded_excerpt(fetch_env) -> None:
    install, state = fetch_env
    chunks = [b"e" * 100] * 1000
    install(lambda req, st: httpx.Response(404, stream=_Chunks(chunks, st["read"])))
    with pytest.raises(RuntimeError, match="HTTP 404") as err:
        pop_refresh._fetch_with_retry("https://example.test/x", max_retries=2)
    assert len(str(err.value)) < 300
    assert sum(state["read"]) < 1000
    assert state["calls"] == 1, "4xx is not retried"


@pytest.mark.parametrize("status", [429, 503])
def test_fetch_retries_transient_status_then_succeeds(fetch_env, status) -> None:
    install, state = fetch_env

    def handler(_req, st):
        if st["calls"] == 1:
            return httpx.Response(status, headers={"retry-after": "1"})
        return httpx.Response(200, json={"ok": True})

    install(handler)
    assert pop_refresh._fetch_with_retry("https://example.test/x", max_retries=2) == {"ok": True}
    assert state["calls"] == 2


@pytest.mark.parametrize("status", [429, 503])
def test_fetch_gives_up_after_retries_on_transient_status(fetch_env, status) -> None:
    install, state = fetch_env
    install(lambda req, st: httpx.Response(status))
    with pytest.raises(RuntimeError):
        pop_refresh._fetch_with_retry("https://example.test/x", max_retries=2)
    assert state["calls"] == 3


@pytest.mark.parametrize(
    ("header", "expected"),
    [("0", 0), ("5", 5), ("9999", 60), ("-5", None), ("abc", None), ("1_0", None), ("", None), ("٣", None)],
)
def test_parse_retry_after_delta_seconds(header, expected) -> None:
    assert pop_refresh._parse_retry_after(header) == expected


def test_parse_retry_after_http_date() -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    now = datetime.now(UTC)
    assert 0 < pop_refresh._parse_retry_after(format_datetime(now + timedelta(seconds=20), usegmt=True)) <= 21
    assert pop_refresh._parse_retry_after(format_datetime(now - timedelta(seconds=20), usegmt=True)) is None


@pytest.mark.parametrize("header", ["-1", "-9999", "garbage"])
def test_fetch_invalid_retry_after_falls_back_to_backoff(fetch_env, monkeypatch, header) -> None:
    install, state = fetch_env
    sleeps: list[float] = []
    monkeypatch.setattr(pop_refresh.time, "sleep", sleeps.append)
    install(lambda req, st: httpx.Response(429, headers={"retry-after": header}))
    with pytest.raises(RuntimeError, match="Rate limited"):
        pop_refresh._fetch_with_retry("https://example.test/x", max_retries=2)
    assert state["calls"] == 3
    assert len(sleeps) == 2
    assert all(s >= 1.0 for s in sleeps), "invalid Retry-After must not cause immediate retries"


def test_fetch_explicit_zero_retry_after_is_honoured(fetch_env, monkeypatch) -> None:
    install, _ = fetch_env
    sleeps: list[float] = []
    monkeypatch.setattr(pop_refresh.time, "sleep", sleeps.append)
    install(lambda req, st: httpx.Response(429, headers={"retry-after": "0"}))
    with pytest.raises(RuntimeError, match="Rate limited"):
        pop_refresh._fetch_with_retry("https://example.test/x", max_retries=1)
    assert sleeps == [0]
