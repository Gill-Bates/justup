#!/usr/bin/env python3
#
# tools/pop_candidate_review.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Inspect and explicitly review unknown PoP candidates in a local justUp database.

Examples:
  python tools/pop_candidate_review.py /path/justup.db inspect
  python tools/pop_candidate_review.py /path/justup.db approve abc --actor admin \\
    --evidence 'Independent route review ticket 123' --canonical fra \\
    --city Frankfurt --country DE --lat 50.1 --lon 8.6
  python tools/pop_candidate_review.py /path/justup.db revoke abc --actor admin --evidence 'Contradicted by new route'

Approvals require two distinct public network contexts, a non-conflicting GeoLite2
country proposal, and explicit reviewer-supplied city coordinates. The resulting
``learned`` row has confidence 70 and alias priority 10. GeoLite2 coordinates are
coarse evidence, not proof of a router's exact position. No automatic promotion.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import pop_candidates
from app.db import sqlite as sqlite_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("inspect", help="read-only candidate and historical learned inventory")
    for action in ("approve", "reject", "revoke"):
        command = sub.add_parser(action)
        command.add_argument("code")
        command.add_argument("--actor", required=True)
        command.add_argument("--evidence", required=True)
        if action == "approve":
            command.add_argument("--canonical", help="explicit reviewed canonical code (default: candidate code)")
            for field in ("city", "country"):
                command.add_argument(f"--{field}", required=True)
            for field in ("lat", "lon"):
                command.add_argument(f"--{field}", required=True, type=float)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("database file does not exist")
    mode = "ro" if args.action == "inspect" else "rw"
    conn = sqlite3.connect(f"file:{args.db.resolve()}?mode={mode}", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if sqlite_db.get_schema_version(conn) != sqlite_db.SCHEMA_VERSION:
            parser.error("database schema is not current; start justUp to apply its versioned migration first")
        if args.action == "inspect":
            for row in conn.execute(
                "SELECT city_code, city, country, lat, lon, facility_count, position_source, updated_at "
                "FROM pop_city_evidence ORDER BY country, city_code LIMIT 5000"
            ):
                print("city-evidence:", dict(row))
            for row in conn.execute(
                "SELECT alias_code, status, seen_count, independent_count, conflict_count, quality_score, "
                "hostname, public_ip, parser_context, canonical_guess, city_guess, country_guess, "
                "proposal_lat, proposal_lon, "
                "proposal_source, created_at, last_seen, reviewed_by, reviewed_at, "
                "review_evidence FROM pop_alias_candidates ORDER BY status, alias_code LIMIT 5000"
            ):
                print(dict(row))
            for row in conn.execute(
                """SELECT p.id, p.alias_code, p.city, p.country, p.source, p.confidence, p.retired,
                   c.status AS review_status FROM pop_locations p
                   LEFT JOIN pop_alias_candidates c ON c.approved_location_id = p.id
                   WHERE p.source = 'learned' ORDER BY p.id"""
            ):
                print("learned:", dict(row))
            for row in conn.execute(
                "SELECT c.alias_code, r.action, r.actor, r.evidence, r.reviewed_at, r.location_id "
                "FROM pop_candidate_reviews r JOIN pop_alias_candidates c ON c.id = r.candidate_id "
                "ORDER BY r.id LIMIT 10000"
            ):
                print("review:", dict(row))
        else:
            pop_candidates.review(
                conn,
                args.code,
                action=args.action,
                actor=args.actor,
                evidence=args.evidence,
                canonical=getattr(args, "canonical", None),
                city=getattr(args, "city", None),
                country=getattr(args, "country", None),
                lat=getattr(args, "lat", None),
                lon=getattr(args, "lon", None),
            )
            print(f"{args.action}: {args.code}")
    except (sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
