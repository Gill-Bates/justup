#!/usr/bin/env python3
#
# tests/test_group_target_atomicity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Group check-and-write atomicity and target-delete failure ordering (OPEN-TASKS review residue)."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import groups, targets
from app.api.auth import get_current_user
from app.db import sqlite as sqlite_db
from app.db import tsdb
from app.utils.deps import get_conn
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "justup.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    sqlite_db.close_connection(conn)
    return path


@pytest.fixture
def conn(db_path: Path):
    connection = sqlite_db.connect(db_path)
    yield connection
    sqlite_db.close_connection(connection)


def _values(name: str = "t", group: str | None = None) -> dict:
    values = {"name": name, "host": f"{name}.example.com", "interval_seconds": 60, "retention_days": 30}
    if group is not None:
        values["group"] = group
    return values


def _client(get_connection, *routers, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    app.state.tsdb_dir = Path(get_connection().execute("PRAGMA database_list").fetchone()[2]).parent
    for router in routers:
        app.include_router(router)
    app.dependency_overrides[get_conn] = get_connection
    app.dependency_overrides[get_current_user] = lambda: {"username": "root", "is_admin": 1}
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


# ─── group delete rule: refuse non-empty groups (409), unknown is 404 ─────────


def test_group_delete_status_codes(conn):
    client = _client(lambda: conn, groups.router)
    gid = client.post("/groups", json={"name": "core"}).json()["id"]
    tid = sqlite_db.create_target(conn, _values(group="core"))

    resp = client.delete(f"/groups/{gid}")
    assert resp.status_code == 409
    assert "1 target(s)" in resp.json()["detail"]
    assert sqlite_db.get_group(conn, gid) is not None
    # Targets are never moved to "Ungrouped" implicitly.
    assert sqlite_db.get_target(conn, tid)["group"] == "core"
    assert conn.execute("SELECT group_id FROM targets WHERE id = ?", (tid,)).fetchone()[0] == gid

    assert client.delete("/groups/9999").status_code == 404

    conn.execute("UPDATE targets SET group_id = NULL")
    conn.commit()
    assert client.delete(f"/groups/{gid}").status_code == 204
    assert sqlite_db.get_group(conn, gid) is None


def test_group_duplicate_and_unknown_statuses(conn):
    client = _client(lambda: conn, groups.router)
    a = client.post("/groups", json={"name": "a"}).json()["id"]
    client.post("/groups", json={"name": "b"})
    assert client.post("/groups", json={"name": "a"}).status_code == 409
    assert client.patch(f"/groups/{a}", json={"name": "b"}).status_code == 409
    assert client.patch("/groups/9999", json={"name": "zzz"}).status_code == 404
    assert client.patch(f"/groups/{a}", json={"description": "x"}).status_code == 200


# ─── concurrency: separate connections, one writer wins per race ──────────────


def _race(db_path: Path, funcs: list) -> list:
    """Run each ``func(conn)`` on its own connection and thread, released together."""
    barrier = threading.Barrier(len(funcs))
    results: list = [None] * len(funcs)

    def run(i: int, func) -> None:
        own = sqlite_db.connect(db_path)
        try:
            barrier.wait(timeout=10)
            try:
                results[i] = ("ok", func(own))
            except Exception as exc:  # noqa: BLE001 - outcome is asserted by the caller
                results[i] = ("err", exc)
        finally:
            sqlite_db.close_connection(own)

    threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(funcs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_target_assignment_racing_group_delete_stays_consistent(db_path: Path):
    for round_no in range(15):
        setup = sqlite_db.connect(db_path)
        gid = sqlite_db.create_group(setup, name="g")
        sqlite_db.close_connection(setup)

        results = _race(
            db_path,
            [
                lambda c, n=round_no: sqlite_db.create_target(c, _values(f"t{n}", group="g")),
                lambda c, g=gid: sqlite_db.delete_group_if_empty(c, g),
            ],
        )
        check = sqlite_db.connect(db_path)
        try:
            assert check.execute("PRAGMA foreign_key_check").fetchall() == []
            target = check.execute(_target_sql(), (f"t{round_no}",)).fetchone()
            assert results[0][0] == "ok", results[0]
            # The target always ends up in a group that exists; a lost race never leaves a dangling reference.
            assert target["group"] == "g"
            if results[1][0] == "err":
                assert isinstance(results[1][1], sqlite_db.GroupNotEmptyError)
            check.execute("DELETE FROM targets")
            check.execute("DELETE FROM groups")
            check.commit()
        finally:
            sqlite_db.close_connection(check)


def _target_sql() -> str:
    return 'SELECT t.id, g.name AS "group" FROM targets t LEFT JOIN groups g ON g.id = t.group_id WHERE t.name = ?'


def test_duplicate_group_create_race_has_exactly_one_winner(db_path: Path):
    def create(own):
        client = _client(lambda: own, groups.router)
        return client.post("/groups", json={"name": "same"}).status_code

    results = _race(db_path, [create] * 6)
    codes = sorted(r[1] for r in results if r[0] == "ok")
    assert codes == [201] + [409] * 5, results
    check = sqlite_db.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM groups WHERE name = 'same'").fetchone()[0] == 1
    sqlite_db.close_connection(check)


def test_concurrent_target_creates_share_one_new_group(db_path: Path):
    results = _race(
        db_path, [lambda c, n=n: sqlite_db.create_target(c, _values(f"n{n}", group="fresh")) for n in range(5)]
    )
    assert all(r[0] == "ok" for r in results), results
    check = sqlite_db.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM groups WHERE name = 'fresh'").fetchone()[0] == 1
    assert check.execute("SELECT COUNT(DISTINCT group_id) FROM targets").fetchone()[0] == 1
    sqlite_db.close_connection(check)


def test_target_update_to_group_racing_rename_keeps_assignment(db_path: Path):
    setup = sqlite_db.connect(db_path)
    gid = sqlite_db.create_group(setup, name="old")
    tid = sqlite_db.create_target(setup, _values("t"))
    sqlite_db.close_connection(setup)

    results = _race(
        db_path,
        [
            lambda c: sqlite_db.update_target(c, target_id=tid, patch={"group": "old"}),
            lambda c: sqlite_db.update_group(c, group_id=gid, patch={"name": "new"}),
        ],
    )
    assert all(r[0] == "ok" for r in results), results
    check = sqlite_db.connect(db_path)
    row = sqlite_db.get_target(check, tid)
    assert row["group"] in {"old", "new"}  # follows the group when renamed first, else a fresh "old" group
    assert check.execute("PRAGMA foreign_key_check").fetchall() == []
    sqlite_db.close_connection(check)


# ─── target delete: failure ordering (files are removed before the commit) ────


class _FailingCommit:
    """Connection proxy whose commit can be made to fail (sqlite3.Connection.commit is read-only)."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.fail = True

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def commit(self) -> None:
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        self._real.commit()


def _series(tsdb_dir: Path, tid: int) -> Path:
    path = tsdb_dir / f"target_{tid}" / "ping_ms.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": "2026-01-01T00:00:00+00:00", "value": 1}\n', encoding="utf-8")
    return path


def test_delete_commit_failure_after_file_removal_is_retryable(conn, caplog):
    tid = sqlite_db.create_target(conn, _values("t"))
    proxy = _FailingCommit(conn)
    client = _client(lambda: proxy, targets.router, raise_server_exceptions=False)
    series = _series(client.app.state.tsdb_dir, tid)

    with caplog.at_level(logging.ERROR):
        resp = client.delete(f"/targets/{tid}")

    assert resp.status_code == 500
    assert "Repeat" in resp.json()["detail"]
    assert sqlite_db.get_target(conn, tid) is not None  # commit failed: row remains
    assert not series.exists()  # history already gone (the user asked for deletion anyway)
    assert "delete failed at commit" in caplog.text

    proxy.fail = False
    assert client.delete(f"/targets/{tid}").status_code == 204
    assert sqlite_db.get_target(conn, tid) is None
    assert not series.parent.exists()


def test_delete_with_remaining_files_rolls_back_row_and_logs(conn, monkeypatch, caplog):
    tid = sqlite_db.create_target(conn, _values("t"))
    client = _client(lambda: conn, targets.router, raise_server_exceptions=False)
    series = _series(client.app.state.tsdb_dir, tid)
    real_rmtree, real_walk = tsdb.shutil.rmtree, tsdb.os.walk

    def deny(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(tsdb.shutil, "rmtree", deny)
    monkeypatch.setattr(tsdb.os, "walk", lambda *_a, **_k: iter(()))
    with caplog.at_level(logging.WARNING):
        resp = client.delete(f"/targets/{tid}")

    assert resp.status_code == 500
    assert "repeated" in resp.json()["detail"]
    assert sqlite_db.get_target(conn, tid) is not None  # rolled back: no row without a way to retry
    assert series.exists()  # nothing was removed
    assert "TSDB removal incomplete" in caplog.text

    monkeypatch.setattr(tsdb.shutil, "rmtree", real_rmtree)
    monkeypatch.setattr(tsdb.os, "walk", real_walk)
    assert client.delete(f"/targets/{tid}").status_code == 204
    assert sqlite_db.get_target(conn, tid) is None
    assert not series.parent.exists()


def test_delete_with_raising_file_removal_keeps_row_and_converges(conn, monkeypatch):
    tid = sqlite_db.create_target(conn, _values("t"))
    client = _client(lambda: conn, targets.router, raise_server_exceptions=False)
    series = _series(client.app.state.tsdb_dir, tid)
    real_delete = tsdb.delete_target

    def boom(*_a, **_k):
        raise OSError("io error")

    monkeypatch.setattr(tsdb, "delete_target", boom)
    assert client.delete(f"/targets/{tid}").status_code == 500
    assert sqlite_db.get_target(conn, tid) is not None
    assert series.exists()

    monkeypatch.setattr(tsdb, "delete_target", real_delete)
    assert client.delete(f"/targets/{tid}").status_code == 204
    assert sqlite_db.get_target(conn, tid) is None
