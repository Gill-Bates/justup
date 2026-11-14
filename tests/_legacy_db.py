#!/usr/bin/env python3
#
# tests/_legacy_db.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Builds databases in the schema-version-1 layout (also the layout of the former stamp 10).

Every table except ``recipients`` comes from the application's frozen version-1 DDL. The version-1
``recipients`` table is written out here as an independent literal: it must not follow later changes of
the application DDL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.sqlite.schema_v1 import _V1_OTHER_DDL

V1_RECIPIENTS_DDL = """
CREATE TABLE recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    created_at timestamp NOT NULL,
    updated_at timestamp NOT NULL,
    enable_for_all_targets INTEGER NOT NULL DEFAULT 0 CHECK (enable_for_all_targets IN (0, 1)),
    CHECK (phone IS NOT NULL OR email IS NOT NULL)
)
"""
V1_RECIPIENT_INDEXES = (
    "CREATE UNIQUE INDEX idx_recipients_phone ON recipients(phone) WHERE phone IS NOT NULL AND phone != ''",
    "CREATE INDEX idx_recipients_email ON recipients(email)",
    "CREATE INDEX idx_recipients_enabled ON recipients(is_enabled)",
)

STAMP = "2026-01-01T00:00:00Z"


def build_v1_database(path: Path, *, stamp: int = 1) -> None:
    """Create an empty database file in the version-1 layout with ``PRAGMA user_version = stamp``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA journal_mode=WAL")  # as every database the application has opened
        for statement in _V1_OTHER_DDL:
            raw.execute(statement)
        raw.execute(V1_RECIPIENTS_DDL)
        for statement in V1_RECIPIENT_INDEXES:
            raw.execute(statement)
        raw.execute(f"PRAGMA user_version = {stamp}")
        raw.commit()
    finally:
        raw.close()


def seed_v1_data(path: Path) -> dict[str, int]:
    """Fill a version-1 database: users, standalone recipients, targets, assignments. Returns row counts.

    Recipient id 3 was deleted, so the AUTOINCREMENT counter (3) is above the highest id (2).
    """
    raw = sqlite3.connect(path)
    try:
        for name in ("admin", "alice", "bob"):
            raw.execute(
                "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?, 'x', ?, ?)",
                (name, 1 if name == "admin" else 0, STAMP),
            )
        for rid, name, phone, email in ((1, "ops", "+4915112345678", None), (2, "mail only", None, "m@example.org")):
            raw.execute(
                "INSERT INTO recipients(id, name, phone, email, is_enabled, created_at, updated_at, "
                "enable_for_all_targets) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (rid, name, phone, email, STAMP, STAMP, 1 if rid == 2 else 0),
            )
        raw.execute(
            "INSERT INTO recipients(id, name, email, created_at, updated_at) VALUES (3, 'gone', 'g@example.org', ?, ?)",
            (STAMP, STAMP),
        )
        raw.execute("DELETE FROM recipients WHERE id = 3")
        for tid, name in ((1, "t1"), (2, "t2")):
            raw.execute(
                "INSERT INTO targets(id, name, interval_seconds, retention_days, enable_ping, enable_http_check, "
                "enable_cert_expiration, host, created_at, updated_at) VALUES (?, ?, 60, 30, 1, 0, 0, ?, ?, ?)",
                (tid, name, f"{name}.example.org", STAMP, STAMP),
            )
        raw.executemany(
            "INSERT INTO target_recipients(target_id, recipient_id) VALUES (?, ?)", [(1, 1), (1, 2), (2, 2)]
        )
        raw.execute("INSERT INTO target_alert_users(target_id, user_id) VALUES (1, 2)")
        raw.commit()
    finally:
        raw.close()
    return {"users": 3, "recipients": 2, "targets": 2, "target_recipients": 3}
