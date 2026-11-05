#!/usr/bin/env python3
#
# tests/test_pop_locations.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""POP location listing sort contract and welcome mail link rendering."""

from __future__ import annotations

import sqlite3

import pytest

from app.api.targets import _build_welcome_email_html
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


def test_welcome_email_links_http_system_url() -> None:
    html_body = _build_welcome_email_html(
        "Ann", "T", "msg", system_url="https://up.example/?a=1&amp;b=2", github_url="https://g.example"
    )
    assert 'href="https://up.example/?a=1&amp;b=2"' in html_body


@pytest.mark.parametrize("url", ["#", "", "javascript:alert(1)"])
def test_welcome_email_omits_link_for_non_http_system_url(url) -> None:
    html_body = _build_welcome_email_html("Ann", "T", "msg", system_url=url, github_url="https://g.example")
    assert "Open justUp!" not in html_body


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
    assert _row(conn, "fra") == (source, 50.1, 8.6, 0, 100, _row(conn, "fra")[5], "run-1")
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
