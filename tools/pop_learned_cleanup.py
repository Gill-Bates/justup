#!/usr/bin/env python3
"""Inspect and remove pop_locations rows with source='learned'.

Default mode is a read-only report. Deletion needs --apply and an explicit
--ids list taken from that report, so nothing is removed implicitly.
Only rows with source='learned' are ever touched (never manual/seed/imports).

Usage:
    pop_learned_cleanup.py /path/to/justup.db
    pop_learned_cleanup.py /path/to/justup.db --apply --ids 12,13 --backup /path/pre.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)


def report(db: Path) -> int:
    con = _ro(db)
    print("source/retired counts:")
    for row in con.execute("SELECT source, retired, COUNT(*) FROM pop_locations GROUP BY 1, 2 ORDER BY 1, 2"):
        print("  ", row)
    rows = con.execute(
        "SELECT id, alias_code, canonical_code, city, country, retired FROM pop_locations "
        "WHERE source = 'learned' ORDER BY alias_code, id"
    ).fetchall()
    print(f"learned rows: {len(rows)}")
    for r in rows:
        print("  ", r)
        for o in con.execute(
            "SELECT id, canonical_code, city, source FROM pop_locations WHERE alias_code = ? AND id <> ?",
            (r[1], r[0]),
        ):
            print("      same alias:", o)
    return 0


def apply(db: Path, ids: list[int], backup: Path) -> int:
    if backup.exists():
        print(f"Error: backup target {backup} already exists", file=sys.stderr)
        return 1
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(backup))
    with dst:
        src.backup(dst)  # consistent copy even in WAL mode
    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        print("Error: backup failed integrity_check", file=sys.stderr)
        return 1
    dst.close()
    print(f"backup written: {backup}")

    src.isolation_level = None
    marks = ",".join("?" * len(ids))
    src.execute("BEGIN IMMEDIATE")
    cur = src.execute(
        f"DELETE FROM pop_locations WHERE source = 'learned' AND id IN ({marks})",  # noqa: S608 - marks holds only "?" placeholders
        ids,
    )
    if cur.rowcount != len(ids):
        src.execute("ROLLBACK")
        print(
            f"Error: expected {len(ids)} learned rows, matched {cur.rowcount}; rolled back",
            file=sys.stderr,
        )
        return 1
    src.execute("COMMIT")
    print(f"deleted {cur.rowcount} rows")
    print("integrity_check:", src.execute("PRAGMA integrity_check").fetchone()[0])
    print("foreign_key_check rows:", len(src.execute("PRAGMA foreign_key_check").fetchall()))
    left = src.execute("SELECT COUNT(*) FROM pop_locations WHERE source = 'learned'").fetchone()[0]
    print(f"remaining learned rows: {left}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("db", type=Path)
    p.add_argument("--apply", action="store_true", help="delete the rows given by --ids")
    p.add_argument("--ids", help="comma-separated pop_locations ids (source must be 'learned')")
    p.add_argument("--backup", type=Path, help="backup file path (required with --apply)")
    a = p.parse_args()
    if not a.db.is_file():
        print(f"Error: {a.db} not found", file=sys.stderr)
        return 1
    if not a.apply:
        return report(a.db)
    if not a.ids or not a.backup:
        print("Error: --apply needs --ids and --backup", file=sys.stderr)
        return 1
    try:
        ids = sorted({int(x) for x in a.ids.split(",") if x.strip()})
    except ValueError:
        print("Error: --ids must be integers", file=sys.stderr)
        return 1
    if not ids:
        print("Error: --ids is empty", file=sys.stderr)
        return 1
    return apply(a.db, ids, a.backup)


if __name__ == "__main__":
    sys.exit(main())
