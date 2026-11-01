#!/usr/bin/env python3
#
# tests/test_pop_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, S105
"""POP lookup: tie handling by location, determinism, normalization, confidence labels."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from app.db import pop_locations, pop_lookup
from app.db import sqlite as sqlite_db
from tests import _support  # noqa: F401


def _add(conn, canonical, alias, lat, lon, *, confidence=100, priority=1, retired=0):
    conn.execute(
        "INSERT INTO pop_locations (canonical_code, alias_code, city, country, lat, lon, source,"
        " confidence, priority, created_at, updated_at, retired) VALUES (?,?,?,?,?,?,'test',?,?,'x','x',?)",
        (canonical, alias, canonical, "XX", lat, lon, confidence, priority, retired),
    )


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    sqlite_db.init_schema(c)
    return c


def test_tie_same_location_returns_hit_deterministically(conn):
    _add(conn, "fra", "fra1", 50.0, 8.6)
    _add(conn, "fra", "fra2", 50.0, 8.6)
    first = pop_lookup.lkp_t(conn, ["fra1", "fra2"])
    assert first is not None and first["canonical_code"] == "fra"
    assert all(pop_lookup.lkp_t(conn, ["fra2", "fra1"]) == first for _ in range(5))


def test_tie_legacy_alias_same_coordinates_is_same_site(conn):
    _add(conn, "fra", "xx", 50.0, 8.6)
    _add(conn, "frank", "xx2", 50.0, 8.6)
    assert pop_lookup.lkp_t(conn, ["xx", "xx2"]) is not None


def test_tie_conflicting_locations_returns_none_and_logs(conn, caplog):
    _add(conn, "aaa", "dup", 10.0, 10.0)
    _add(conn, "zzz", "dup", -30.0, 100.0)
    with caplog.at_level(logging.DEBUG, logger="app.db.pop_lookup"):
        assert pop_lookup.lkp_t(conn, ["dup"]) is None
    assert any("amb" in r.getMessage() for r in caplog.records)


def test_priority_or_confidence_difference_resolves_conflict(conn):
    _add(conn, "aaa", "dup", 10.0, 10.0, priority=2)
    _add(conn, "zzz", "dup", -30.0, 100.0, priority=1)
    assert pop_lookup.lkp_t(conn, ["dup"])["canonical_code"] == "zzz"
    _add(conn, "mmm", "dup2", 1.0, 1.0, confidence=70)
    _add(conn, "nnn", "dup2", 2.0, 2.0, confidence=100)
    assert pop_lookup.lkp_t(conn, ["dup2"])["canonical_code"] == "nnn"


def test_retired_rows_ignored_in_tie(conn):
    _add(conn, "aaa", "dup", 10.0, 10.0)
    _add(conn, "zzz", "dup", -30.0, 100.0, retired=1)
    assert pop_lookup.lkp_t(conn, ["dup"])["canonical_code"] == "aaa"


def test_normalization_and_empty_inputs(conn):
    _add(conn, "fra", "fra", 50.0, 8.6)
    assert pop_lookup.lkp_t(conn, ["  FRA "])["canonical_code"] == "fra"
    assert pop_lookup.lkp_t(conn, [None, ""]) is None
    assert pop_lookup.lkp_t(conn, []) is None
    assert pop_lookup.lkp_p(conn, ["FRA01"])["matched_token"] == "fra01"
    assert pop_lookup.lkp_h(conn, "Ae1.FRA.example.net.")["match_type"] == "hostname"
    assert pop_locations._normalize_pop_code("  FrA ") == "fra"


@pytest.mark.parametrize(("value", "label"), [(100, "high"), (99, "medium"), (70, "medium"), (69, "low"), (0, "low")])
def test_confidence_labels_and_constants(value, label):
    assert pop_lookup._cl(value) == label
    assert (pop_lookup.CONFIDENCE_HIGH, pop_lookup.CONFIDENCE_MEDIUM) == (100, 70)
    assert (pop_locations.CONFIDENCE_HIGH, pop_locations.CONFIDENCE_MEDIUM, pop_locations.CONFIDENCE_LOW) == (
        100,
        70,
        40,
    )
