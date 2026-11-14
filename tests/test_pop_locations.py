#!/usr/bin/env python3
#
# tests/test_pop_locations.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""POP location listing sort contract and welcome mail link rendering."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import pop_locations
from app.db import sqlite as sqlite_db
from tests import _support  # noqa: F401 - imported for its sys.path side effect

_ROWS = [
    ("AAA", "Zurich", "CH"),
    ("BBB", "Berlin", "DE"),
    ("CCC", "Amsterdam", "NL"),
]


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    sqlite_db.init_schema(connection)
    for code, city, country in _ROWS:
        connection.execute(
            "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source,"
            " confidence, priority, created_at, updated_at) VALUES (?, ?, ?, ?, 0, 0, 'test', 50, 1, 'x', 'x')",
            (code, code, city, country),
        )
    connection.commit()
    return connection


@pytest.mark.parametrize(
    ("sort_by", "expected"),
    [
        ("canonical_code", ["AAA", "BBB", "CCC"]),
        ("city", ["CCC", "BBB", "AAA"]),
        ("country", ["AAA", "BBB", "CCC"]),
    ],
)
def test_list_pop_locations_sorts_by_whitelisted_column(conn, sort_by, expected) -> None:
    rows = pop_locations.list_pop_locations(conn, sort_by=sort_by)
    assert [r["canonical_code"] for r in rows] == expected


def test_list_pop_locations_default_is_canonical_code(conn) -> None:
    rows = pop_locations.list_pop_locations(conn)
    assert [r["canonical_code"] for r in rows] == ["AAA", "BBB", "CCC"]


def test_list_pop_locations_rejects_unlisted_sort_column(conn) -> None:
    with pytest.raises(ValueError):
        pop_locations.list_pop_locations(conn, sort_by="city; DROP TABLE pop_locations")


def test_count_pop_locations_uses_the_list_filters(conn) -> None:
    conn.execute("UPDATE pop_locations SET retired = 1 WHERE canonical_code = 'AAA'")
    conn.execute("UPDATE pop_locations SET source = 'other' WHERE canonical_code = 'BBB'")
    for source in (None, "test", "other"):
        for include_retired in (False, True):
            listed = pop_locations.list_pop_locations(conn, source=source, include_retired=include_retired)
            assert pop_locations.count_pop_locations(conn, source=source, include_retired=include_retired) == len(
                listed
            )
    assert pop_locations.count_pop_locations(conn) == 2


def _row(conn, alias: str):
    return conn.execute(
        "SELECT source, lat, lon, priority, confidence, last_seen_at, import_run_id FROM pop_locations"
        " WHERE alias_code = ?",
        (alias,),
    ).fetchone()


@pytest.mark.parametrize("source", ["manual", "seed"])
def test_import_upsert_keeps_curated_rows_unchanged(conn, source) -> None:
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source,"
        " confidence, priority, created_at, updated_at) VALUES ('fra', 'fra', 'Frankfurt', 'DE', 50.1, 8.6, ?,"
        " 100, 0, 'x', 'x')",
        (source,),
    )
    changed = pop_locations.upsert_pop_location(
        conn, "fra", "fra", "Frankfurt am Main", "DE", 1.0, 2.0, "peeringdb", 90, 10, import_run_id="run-1"
    )
    assert changed is False
    row = _row(conn, "fra")
    assert row[:5] == (source, 50.1, 8.6, 0, 100)
    assert row[5] is not None
    assert row[6] == "run-1"
    assert conn.execute("SELECT city FROM pop_locations WHERE alias_code = 'fra'").fetchone()[0] == "Frankfurt"


def test_import_upsert_updates_its_own_rows(conn) -> None:
    assert pop_locations.upsert_pop_location(conn, "ams", "ams", "Amsterdam", "NL", 52.3, 4.9, "peeringdb", 100, 10)
    assert pop_locations.upsert_pop_location(conn, "ams", "ams", "Amsterdam", "NL", 52.4, 4.8, "peeringdb", 100, 10)
    assert _row(conn, "ams")[:5] == ("peeringdb", 52.4, 4.8, 10, 100)


def _upsert(conn, lat: float, lon: float, alias: str = "zzz") -> bool:
    return pop_locations.upsert_pop_location(
        conn, "zzz", alias, "Testcity", "DE", lat, lon, pop_locations.SOURCE_PEERINGDB, 70, 10
    )


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (float("nan"), 8.0),
        (50.0, float("nan")),
        (50.0, float("inf")),
        (float("-inf"), 8.0),
        (90.0001, 0.0),
        (-90.0001, 0.0),
        (0.0, 180.0001),
        (0.0, -180.0001),
        (123.0, -999.0),
        (None, 8.0),
        ("50.1", 8.6),
        (True, 8.6),
    ],
)
def test_upsert_rejects_invalid_coordinates_without_touching_the_table(conn, lat, lon) -> None:
    assert _upsert(conn, lat, lon) is False
    assert conn.execute("SELECT COUNT(*) FROM pop_locations WHERE alias_code = 'zzz'").fetchone()[0] == 0


@pytest.mark.parametrize(("lat", "lon"), [(90.0, 180.0), (-90.0, -180.0), (0, 0), (50, 8)])
def test_upsert_accepts_the_boundary_values(conn, lat, lon) -> None:
    assert _upsert(conn, lat, lon) is True
    row = conn.execute("SELECT lat, lon FROM pop_locations WHERE alias_code = 'zzz'").fetchone()
    assert (row[0], row[1]) == (lat, lon)


def test_invalid_coordinates_do_not_overwrite_an_existing_imported_row(conn) -> None:
    assert _upsert(conn, 50.0, 8.0) is True
    assert _upsert(conn, 123.0, 8.0) is False
    row = conn.execute("SELECT lat, lon FROM pop_locations WHERE alias_code = 'zzz'").fetchone()
    assert (row[0], row[1]) == (50.0, 8.0)


def _insert_learned(conn, *, retired: int) -> None:
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source,"
        " confidence, priority, created_at, updated_at, retired) VALUES ('fra', 'fra', 'Frankfurt', 'DE', 50.1, 8.6,"
        " 'learned', 90, 5, 'x', 'x', ?)",
        (retired,),
    )


def test_import_upsert_keeps_an_active_learned_row_unchanged(conn) -> None:
    _insert_learned(conn, retired=0)
    changed = pop_locations.upsert_pop_location(conn, "fra", "fra", "Other", "DE", 1.0, 2.0, "peeringdb", 90, 10)
    assert changed is False
    row = conn.execute("SELECT city, source, lat, lon, retired FROM pop_locations WHERE alias_code = 'fra'").fetchone()
    assert tuple(row) == ("Frankfurt", "learned", 50.1, 8.6, 0)


def test_import_reclaims_a_retired_learned_row(conn) -> None:
    _insert_learned(conn, retired=1)
    assert pop_locations.upsert_pop_location(conn, "fra", "fra", "Other", "DE", 1.0, 2.0, "peeringdb", 90, 10)
    row = conn.execute("SELECT city, source, retired FROM pop_locations WHERE alias_code = 'fra'").fetchone()
    assert tuple(row) == ("Other", "peeringdb", 0)


def _seed_retire(conn, total: int, stale: int) -> None:
    for i in range(total):
        run = "old" if i < stale else "new"
        conn.execute(
            "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source,"
            " confidence, priority, created_at, updated_at, import_run_id, retired)"
            " VALUES (?, ?, 'c', 'DE', 0, 0, 'peeringdb', 50, 1, 'x', 'x', ?, 0)",
            (f"r{i}", f"r{i}", run),
        )
    conn.commit()


def test_retire_stale_entries_commits_and_enforces_quota(conn) -> None:
    _seed_retire(conn, 20, 4)
    assert pop_locations.retire_stale_entries(conn, "peeringdb", "new", 20) == 4
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM pop_locations WHERE retired = 1").fetchone()[0] == 4
    conn.execute("UPDATE pop_locations SET import_run_id = 'old' WHERE retired = 0 AND rowid % 2 = 0")
    conn.commit()
    assert pop_locations.retire_stale_entries(conn, "peeringdb", "new", 20) == 0


def test_retire_stale_entries_leaves_a_caller_transaction_open(conn) -> None:
    _seed_retire(conn, 20, 4)
    conn.execute("UPDATE pop_locations SET city = 'touched' WHERE alias_code = 'r19'")
    assert pop_locations.retire_stale_entries(conn, "peeringdb", "new", 20) == 4
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM pop_locations WHERE retired = 1").fetchone()[0] == 0


class _FailingInsertConn:
    """Connection proxy whose INSERT statements raise sqlite3.OperationalError."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def execute(self, sql: str, *args):
        if sql.lstrip().startswith("INSERT"):
            raise sqlite3.OperationalError("disk I/O error")
        return self._inner.execute(sql, *args)


def _fail_upsert(conn) -> bool:
    return pop_locations.upsert_pop_location(
        _FailingInsertConn(conn), "yyy", "yyy", "Failcity", "DE", 1.0, 2.0, pop_locations.SOURCE_PEERINGDB, 70, 10
    )


def test_upsert_write_error_returns_false_and_logs_warning(conn, caplog, monkeypatch) -> None:
    monkeypatch.setattr(pop_locations, "_upsert_error_count", 0)
    with caplog.at_level("DEBUG", logger=pop_locations._log.name):  # noqa: SLF001 - logger name of the module under test
        assert _fail_upsert(conn) is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "yyy/yyy" in warnings[0].getMessage()
    assert conn.execute("SELECT COUNT(*) FROM pop_locations WHERE alias_code='yyy'").fetchone()[0] == 0


def test_upsert_write_errors_are_bounded_and_aggregated(conn, caplog, monkeypatch) -> None:
    monkeypatch.setattr(pop_locations, "_upsert_error_count", 0)
    total = pop_locations._UPSERT_ERROR_SUMMARY_EVERY  # noqa: SLF001 - module tuning constant
    with caplog.at_level("DEBUG", logger=pop_locations._log.name):  # noqa: SLF001 - logger name of the module under test
        results = [_fail_upsert(conn) for _ in range(total)]
    assert not any(results)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == pop_locations._UPSERT_ERROR_DETAIL_LIMIT + 1  # noqa: SLF001
    assert f"{total} POP location upserts failed" in warnings[-1]


def test_bootstrap_counts_only_rows_actually_written(conn) -> None:
    first = pop_locations.bootstrap_pop_locations(conn)
    assert first > 0
    assert conn.execute("SELECT COUNT(*) FROM pop_locations WHERE source='seed'").fetchone()[0] == first
    assert pop_locations.bootstrap_pop_locations(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM pop_locations WHERE source='seed'").fetchone()[0] == first
