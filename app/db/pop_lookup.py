#!/usr/bin/env python3
#
# app/db/pop_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# Implementation for POP location lookups.
#
# Design:
# - app/db/pop_locations.py is the public facade and owns DB lifecycle/import logic.
# - This module implements only the hot-path lookup functions.
#

import logging
import sqlite3

_log = logging.getLogger(__name__)

# Single definition; app/db/pop_locations.py re-exports these (it imports this module).
CONFIDENCE_HIGH = 100
CONFIDENCE_MEDIUM = 70


def normalize_pop_code(code):
    """Normalize a POP code for comparison (lowercase, stripped)."""
    return code.lower().strip()


def _rf(conn):
    # Ensure sqlite3.Row so callers can access row["col"].
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row


def _n(code):
    # Lookup-specific: None/empty tokens normalize to "" instead of raising.
    if not code:
        return ""
    return normalize_pop_code(code)


def _same_site(a, b):
    # Same canonical code, or identical coordinates (legacy aliases of one site).
    if _n(a["canonical_code"]) == _n(b["canonical_code"]):
        return True
    if None in (a["lat"], a["lon"], b["lat"], b["lon"]):
        return False
    return round(a["lat"], 4) == round(b["lat"], 4) and round(a["lon"], 4) == round(b["lon"], 4)


def _cl(confidence):
    if confidence >= CONFIDENCE_HIGH:
        return "high"
    if confidence >= CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def lkp_t(conn, tokens):
    _rf(conn)
    if not tokens:
        return None

    tokens_norm = [_n(t) for t in tokens if t]
    if not tokens_norm:
        return None

    placeholders = ",".join("?" * len(tokens_norm))

    rows = conn.execute(
        f"""
        SELECT
            canonical_code, alias_code, city, country, lat, lon,
            source, confidence, priority
        FROM pop_locations
        WHERE alias_code IN ({placeholders})
          AND retired = 0
        ORDER BY priority ASC, confidence DESC
        """,  # noqa: S608 - placeholder count only, tokens bound as params
        tokens_norm,
    ).fetchall()

    if not rows:
        return None

    row = rows[0]

    # Rows tied on priority AND confidence are equivalent only if they denote the
    # same location; alphabetical order must never pick between conflicting sites.
    tied = [r for r in rows if r["priority"] == row["priority"] and r["confidence"] == row["confidence"]]
    if len(tied) > 1 and not all(_same_site(a, b) for a in tied for b in tied):
        _log.debug(
            "amb: conflicting POP locations for tokens %s: %s",
            tokens_norm,
            sorted({_n(r["canonical_code"]) for r in tied}),
        )
        return None

    if len(tied) > 1:
        # Same site: pick deterministically so repeated lookups return identical rows.
        row = min(tied, key=lambda r: (r["canonical_code"], r["alias_code"]))

    return {
        "canonical_code": row["canonical_code"],
        "alias_code": row["alias_code"],
        "city": row["city"],
        "country": row["country"],
        "lat": row["lat"],
        "lon": row["lon"],
        "source": row["source"],
        "confidence": _cl(row["confidence"]),
        "priority": row["priority"],
    }


def lkp_p(conn, tokens):
    if not tokens:
        return None

    for token in tokens:
        token_norm = _n(token)
        if len(token_norm) >= 3 and token_norm[:3].isalpha():
            prefix = token_norm[:3]
            result = lkp_t(conn, [prefix])
            if result:
                result["matched_token"] = token_norm
                return result

    return None


_MAX_ALIAS_LEN = 8  # pop_locations.alias_code CHECK(length <= 8)


def _alias_candidates(hostname_pad):
    # Every alias the substring predicate below can match is a whole label (pattern 1) or a label
    # prefix (pattern 2), so these strings are a superset that lets SQLite use the alias_code index
    # instead of scanning the table with a leading-wildcard LIKE per row.
    candidates = set()
    for label in hostname_pad.split("."):
        if not label:
            continue
        candidates.add(label)
        candidates.update(label[:k] for k in range(1, min(len(label), _MAX_ALIAS_LEN) + 1))
    return sorted(candidates)


def lkp_h(conn, hostname):
    _rf(conn)
    if not hostname:
        return None

    hostname_lower = hostname.lower().strip().strip(".")
    hostname_norm = hostname_lower.replace("-", ".").replace("_", ".")
    hostname_pad = f".{hostname_norm}."
    candidates = _alias_candidates(hostname_pad)
    if not candidates:
        return None
    placeholders = ",".join("?" * len(candidates))

    row = conn.execute(
        f"""
        SELECT
            canonical_code, alias_code, city, country, lat, lon,
            source, confidence, priority
        FROM pop_locations
        WHERE retired = 0
          AND alias_code IN ({placeholders})
          AND (
              ? LIKE '%.' || alias_code || '.%'
              OR (
                  alias_code GLOB '*[0-9]*'
                  AND ? LIKE '%.' || alias_code || '%'
              )
          )
        ORDER BY
            CASE
                WHEN instr(?, '.' || alias_code || '.') > 0 THEN instr(?, '.' || alias_code || '.')
                ELSE instr(?, '.' || alias_code)
            END ASC,
            length(alias_code) DESC,
            priority ASC,
            confidence DESC
        LIMIT 1
        """,  # noqa: S608 - placeholder count only, candidates bound as params
        (*candidates, hostname_pad, hostname_pad, hostname_pad, hostname_pad, hostname_pad),
    ).fetchone()

    if not row:
        return None

    return {
        "canonical_code": row["canonical_code"],
        "alias_code": row["alias_code"],
        "city": row["city"],
        "country": row["country"],
        "lat": row["lat"],
        "lon": row["lon"],
        "source": row["source"],
        "confidence": _cl(row["confidence"]),
        "priority": row["priority"],
        "match_type": "hostname",
    }
