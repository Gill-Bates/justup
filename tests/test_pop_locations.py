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
