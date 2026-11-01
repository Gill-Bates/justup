#!/usr/bin/env python3
#
# tools/make-dummy-dbs.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Create throw-away dummy databases with sample rows for tests and smoke runs.

  baseline OUT_DIR       fresh v1.0.0 baseline database (current code) under OUT_DIR/sqlite/justup.db
  legacy OUT_DIR REV     pre-squash database (v6 lineage: textual targets.group_name, user_version 6),
                         built by running the init_schema() of git revision REV (the last pre-squash commit)

Never point it at a real data directory; it refuses to overwrite an existing database file.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
TARGET_COLUMNS = (
    "name, interval_seconds, retention_days, enable_ping, enable_http_check, "
    "enable_cert_expiration, host, created_at, updated_at"
)


def _fill(conn: sqlite3.Connection, *, legacy: bool) -> None:
    """Sample rows: groups, targets (group by id, or by text in the legacy lineage), settings, check results."""
    for name in ("web", "mail"):
        conn.execute("INSERT INTO groups(name, created_at, updated_at) VALUES (?, ?, ?)", (name, NOW, NOW))
    rows = [
        ("example-web", "example.org", "web"),
        ("example-mail", "mail.example.org", "mail"),
        ("loose", "x.example.org", None),
    ]
    for name, host, group in rows:
        cur = conn.execute(
            f"INSERT INTO targets({TARGET_COLUMNS}) VALUES (?, 60, 30, 1, 0, 0, ?, ?, ?)",  # noqa: S608 - constant columns
            (name, host, NOW, NOW),
        )
        target_id = cur.lastrowid
        if group is not None:
            if legacy:
                conn.execute("UPDATE targets SET group_name = ? WHERE id = ?", (group, target_id))
            else:
                conn.execute(
                    "UPDATE targets SET group_id = (SELECT id FROM groups WHERE name = ?) WHERE id = ?",
                    (group, target_id),
                )
        conn.execute(
            "INSERT INTO target_status(target_id, ping_up, ping_latency_ms, ping_checked_at, "
            "overall_status, updated_at) "
            "VALUES (?, 1, 12.5, ?, 'up', ?)",
            (target_id, NOW, NOW),
        )
    settings = {"purge_enabled": "true", "pdf_page_size": '"a4"'}
    if legacy:
        settings["online_geo_enabled"] = "true"
    else:
        settings["online_geo_fallback_enabled"] = "false"
    for key, value in settings.items():
        conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)", (key, value, NOW))
    conn.commit()


def _make_baseline(out_dir: Path) -> Path:
    sys.path.insert(0, str(ROOT))
    from app.db import sqlite as sqlite_db

    db = out_dir / "sqlite" / "justup.db"
    conn = sqlite_db.connect(db)
    try:
        sqlite_db.init_schema(conn)
        _fill(conn, legacy=False)
    finally:
        sqlite_db.close_connection(conn)
    return db


_LEGACY_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from app.db import sqlite as s
conn = s.connect(Path(sys.argv[2]))
s.init_schema(conn)
conn.execute("DROP INDEX IF EXISTS idx_targets_group_id")
conn.execute("ALTER TABLE targets DROP COLUMN group_id")
conn.execute("ALTER TABLE targets ADD COLUMN group_name TEXT")
conn.execute("PRAGMA user_version = 6")
conn.commit()
s.close_connection(conn)
"""


def _make_legacy(out_dir: Path, rev: str) -> Path:
    db = out_dir / "sqlite" / "justup.db"
    with tempfile.TemporaryDirectory(prefix="justup-legacy-") as tmp:
        archive = subprocess.run(["git", "archive", rev, "app"], cwd=ROOT, check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        subprocess.run([sys.executable, "-c", _LEGACY_SCRIPT, tmp, str(db)], check=True)
    conn = sqlite3.connect(db)
    try:
        _fill(conn, legacy=True)
    finally:
        conn.close()
    return db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=("baseline", "legacy"))
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("rev", nargs="?", default=None, help="legacy only: git revision with the pre-squash code")
    args = parser.parse_args()
    target = args.out_dir / "sqlite" / "justup.db"
    if target.exists():
        parser.error(f"{target} already exists; refusing to overwrite")
    if args.kind == "legacy" and not args.rev:
        parser.error("legacy needs the pre-squash git revision (e.g. 085a8f1)")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        db = _make_baseline(args.out_dir) if args.kind == "baseline" else _make_legacy(args.out_dir, args.rev)
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(args.out_dir / "sqlite", ignore_errors=True)
        parser.exit(1, f"failed: {exc}\n")
    print(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
