#!/usr/bin/env python3
#
# tests/test_pop_codes_resolution.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""POP code hostname resolution: tokenizer contract and CLLI provider coordinates."""

from __future__ import annotations

import pytest

from app.db import sqlite as sqlite_db
from app.services import pop_codes, pop_codes_embedded
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("ae1.cr2.fra6.ip.gtt.net", ["fra6", "cr2", "gtt"]),
        ("loncore1.example.net", ["loncore1", "example"]),
        ("dor1901aihd001.x.de", ["dor19", "dor"]),
        ("192.168.1.1:8080", []),
        ("1.0.0.127.in-addr.arpa", []),
        ("bb1-core.edge9.example.com", ["example"]),
    ],
)
def test_sql_and_embedded_paths_share_one_tokenizer(host, expected) -> None:
    assert pop_codes.extract_pop_tokens(host) == expected
    assert pop_codes_embedded.extract_pop_tokens(host) == expected


def test_clli_hostname_resolves_coordinates_without_a_db_row(tmp_path) -> None:
    conn = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(conn)
    try:
        conn.execute("DELETE FROM pop_locations")
        conn.commit()
        loc = pop_codes.lookup_pop_code("cr1.nycmny01.att.net", conn=conn)
    finally:
        sqlite_db.close_connection(conn)
    assert loc is not None
    assert loc["city"] == "New York"
    assert (loc["lat"], loc["lon"]) == (40.7128, -74.0060)
    assert loc["source"] == "provider_clli"


def test_clli_hostname_resolves_without_any_db_connection(monkeypatch) -> None:
    monkeypatch.setattr(pop_codes, "_pop_get_conn", None)
    loc = pop_codes.lookup_pop_code("cr1.nycmny01.att.net", conn=None)
    assert loc is not None
    assert loc["source"] == "provider_clli"
    assert loc["city"] == "New York"
