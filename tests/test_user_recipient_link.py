#!/usr/bin/env python3
#
# tests/test_user_recipient_link.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""User <-> recipient 1:1 link in the data layer: lifecycle, delete protection, atomicity, concurrency."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import sqlite as sqlite_db
from app.utils import bootstrap_credentials, crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect

PHONE = "+4915112345678"
PHONE_2 = "+4915100000002"


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)


@pytest.fixture
def conn(db_path: Path):
    connection = sqlite_db.connect(db_path)
    yield connection
    sqlite_db.close_connection(connection)


def _user(conn: sqlite3.Connection, name: str, **kwargs) -> int:
    return sqlite_db.create_user(conn, username=name, password_hash="stub", is_admin=False, **kwargs)  # noqa: S106


def _target(conn: sqlite3.Connection, name: str) -> int:
    return sqlite_db.create_target(
        conn, {"name": name, "host": f"{name}.example.com", "interval_seconds": 60, "retention_days": 30}
    )


def _snapshot(conn: sqlite3.Connection, where: str = "1 = 1") -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT * FROM recipients WHERE {where} ORDER BY id")]  # noqa: S608


def assert_one_to_one(conn: sqlite3.Connection) -> None:
    """The invariant: every user has exactly one recipient and no recipient points at a missing user."""
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM users u WHERE (SELECT COUNT(*) FROM recipients r WHERE r.user_id = u.id) != 1"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM recipients WHERE user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM users)"
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ─── E1 / E2 / E7 ───────────────────────────────────────────────────────────


def test_create_user_creates_exactly_one_linked_recipient(conn):
    plain = _user(conn, "Plain")
    full = _user(conn, "full", phone=PHONE, email="f@example.org", enable_for_all_targets=True)
    for uid in (plain, full):
        rows = conn.execute("SELECT * FROM recipients WHERE user_id = ?", (uid,)).fetchall()
        assert len(rows) == 1
    assert_one_to_one(conn)
    r_plain, r_full = sqlite_db.get_user_recipient(conn, plain), sqlite_db.get_user_recipient(conn, full)
    assert (r_plain["name"], r_plain["phone"], r_plain["email"], r_plain["is_enabled"]) == ("plain", None, None, 0)
    assert (r_full["phone"], r_full["email"], r_full["is_enabled"], r_full["enable_for_all_targets"]) == (
        PHONE,
        "f@example.org",
        1,
        1,
    )


def test_schema_allows_one_recipient_per_user_and_no_dangling_link(conn):
    uid = _user(conn, "owner")
    insert = "INSERT INTO recipients(name, email, created_at, updated_at, user_id) VALUES ('x', 'x@e.org', 't', 't', ?)"
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(insert, (uid,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(insert, (9999,))
    conn.rollback()
    assert_one_to_one(conn)


def test_standalone_recipient_has_no_user_and_still_needs_a_channel(conn):
    rid = sqlite_db.create_recipient(conn, name="ops", email="ops@example.org")
    assert sqlite_db.get_recipient(conn, rid)["user_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    with pytest.raises(ValueError, match="phone number or an email"):
        sqlite_db.create_recipient(conn, name="empty")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # also enforced by the database itself
        conn.execute("INSERT INTO recipients(name, created_at, updated_at) VALUES ('raw', 't', 't')")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        sqlite_db.update_recipient(conn, rid, clear_email=True)


def test_bootstrap_admin_gets_a_disabled_channelless_editable_recipient(conn, tmp_path: Path):
    bootstrap_credentials.configure(tmp_path)
    try:
        assert sqlite_db.ensure_default_admin(conn) is True
    finally:
        bootstrap_credentials.configure(None)
    admin = sqlite_db.get_user_by_username(conn, "admin")
    recipient = sqlite_db.get_user_recipient(conn, admin["id"])
    assert (recipient["name"], recipient["phone"], recipient["email"], recipient["is_enabled"]) == (
        "admin",
        None,
        None,
        0,
    )
    sqlite_db.update_recipient(conn, recipient["id"], email="admin@example.org", is_enabled=True)
    edited = sqlite_db.get_recipient(conn, recipient["id"])
    assert (edited["email"], edited["is_enabled"], edited["user_id"]) == ("admin@example.org", 1, admin["id"])
    assert_one_to_one(conn)


def test_user_created_by_raw_sql_gets_a_disabled_recipient_from_the_trigger(conn):
    conn.execute("INSERT INTO users(username, password_hash, created_at) VALUES ('legacy', 'x', 't')")
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE username = 'legacy'").fetchone()[0]
    recipient = sqlite_db.get_user_recipient(conn, uid)
    assert (recipient["name"], recipient["phone"], recipient["email"], recipient["is_enabled"]) == (
        "legacy",
        None,
        None,
        0,
    )
    assert_one_to_one(conn)


def test_database_guards_protect_the_link_against_direct_sql(conn):
    uid = _user(conn, "guarded", email="g@example.org")
    rid = sqlite_db.get_user_recipient(conn, uid)["id"]
    with pytest.raises(sqlite3.DatabaseError, match="belongs to a user"):
        conn.execute("DELETE FROM recipients WHERE id = ?", (rid,))
    conn.rollback()
    other = sqlite_db.create_recipient(conn, name="free", email="f@example.org")
    with pytest.raises(sqlite3.DatabaseError, match="cannot change"):
        conn.execute("UPDATE recipients SET user_id = ? WHERE id = ?", (uid, other))
    with pytest.raises(sqlite3.DatabaseError, match="cannot change"):
        conn.execute("UPDATE recipients SET user_id = NULL WHERE id = ?", (rid,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # enabled needs a channel
        conn.execute("UPDATE recipients SET phone = NULL, email = NULL, is_enabled = 1 WHERE id = ?", (rid,))
    conn.rollback()
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))  # the cascade is not blocked by the guard
    conn.commit()
    assert sqlite_db.get_recipient(conn, rid) is None
    assert_one_to_one(conn)


def test_init_schema_restores_dropped_triggers_and_repairs_users_without_recipient(db_path: Path):
    conn = sqlite_db.connect(db_path)
    try:
        orphan = _user(conn, "orphan")
        for name in ("trg_users_create_recipient", "trg_recipients_guard_delete", "trg_recipients_user_id_immutable"):
            conn.execute(f"DROP TRIGGER {name}")
        conn.commit()
        conn.execute("DELETE FROM recipients WHERE user_id = ?", (orphan,))
        conn.execute("INSERT INTO users(username, password_hash, created_at) VALUES ('raw', 'x', 't')")
        conn.commit()
        assert sqlite_db.get_user_recipient(conn, orphan) is None
        sqlite_db.init_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 3
        assert sqlite_db.get_user_recipient(conn, orphan)["is_enabled"] == 0
        assert_one_to_one(conn)
    finally:
        sqlite_db.close_connection(conn)


# ─── E3: update ─────────────────────────────────────────────────────────────


def test_user_recipient_is_updated_in_place_and_follows_the_rules(conn):
    uid = _user(conn, "alice")
    first = sqlite_db.get_user_recipient(conn, uid)["id"]
    rid = sqlite_db.update_user_recipient(
        conn, uid, phone=PHONE, email="a@example.org", is_enabled=True, enable_for_all_targets=False
    )
    assert rid == first
    # Enabling without any channel is refused; unset means "enabled only with a channel".
    with pytest.raises(ValueError, match="cannot be enabled"):
        sqlite_db.update_user_recipient(
            conn, uid, phone=None, email=None, is_enabled=True, enable_for_all_targets=False
        )
    assert sqlite_db.get_recipient(conn, first)["phone"] == PHONE
    sqlite_db.update_user_recipient(conn, uid, phone=None, email=None, is_enabled=None, enable_for_all_targets=False)
    row = sqlite_db.get_recipient(conn, first)
    assert (row["phone"], row["email"], row["is_enabled"], row["user_id"]) == (None, None, 0, uid)
    assert conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 1


def test_recipient_name_follows_a_rename_only_while_it_equals_the_old_username(conn):
    followed, custom = _user(conn, "old-a"), _user(conn, "old-b")
    sqlite_db.update_recipient(conn, sqlite_db.get_user_recipient(conn, custom)["id"], name="Night shift")
    for uid, new in ((followed, "new-a"), (custom, "new-b")):
        sqlite_db.update_user(conn, uid, username=new, is_admin=False)
    assert sqlite_db.get_user_recipient(conn, followed)["name"] == "new-a"
    assert sqlite_db.get_user_recipient(conn, custom)["name"] == "Night shift"


def test_enable_for_all_targets_of_a_user_recipient_assigns_and_unassigns(conn):
    t1, t2 = _target(conn, "a"), _target(conn, "b")
    uid = _user(conn, "carol")
    rid = sqlite_db.get_user_recipient(conn, uid)["id"]
    sqlite_db.update_user_recipient(
        conn, uid, phone=None, email="c@e.org", is_enabled=True, enable_for_all_targets=True
    )
    assert sorted(sqlite_db.get_target_recipient_ids(conn, t1) + sqlite_db.get_target_recipient_ids(conn, t2)) == [
        rid,
        rid,
    ]
    sqlite_db.update_user_recipient(
        conn, uid, phone=None, email="c@e.org", is_enabled=True, enable_for_all_targets=False
    )
    assert sqlite_db.get_recipient_targets(conn, rid) == []


# ─── E4: delete protection in the data layer ────────────────────────────────


def test_delete_recipient_refuses_a_user_bound_recipient_and_removes_a_standalone_one(conn):
    uid = _user(conn, "dave")
    bound = sqlite_db.get_user_recipient(conn, uid)["id"]
    free = sqlite_db.create_recipient(conn, name="free", email="f@e.org")
    with pytest.raises(sqlite_db.UserBoundRecipientError):
        sqlite_db.delete_recipient(conn, bound)
    assert sqlite_db.get_recipient(conn, bound)["user_id"] == uid
    sqlite_db.delete_recipient(conn, free)
    assert sqlite_db.get_recipient(conn, free) is None


# ─── E5 / E6: delete a user ─────────────────────────────────────────────────


def test_delete_user_removes_recipient_and_assignments_but_nothing_else(conn):
    t1, t2 = _target(conn, "a"), _target(conn, "b")
    keep_user = _user(conn, "keeper", email="k@e.org", enable_for_all_targets=True)
    gone = _user(conn, "goner", phone=PHONE, enable_for_all_targets=True)
    standalone = sqlite_db.create_recipient(conn, name="ops", email="ops@e.org")
    sqlite_db.set_target_recipients(conn, t1, [*sqlite_db.get_target_recipient_ids(conn, t1), standalone])
    sqlite_db.set_target_alert_users(conn, t1, [gone, keep_user])
    gone_recipient = sqlite_db.get_user_recipient(conn, gone)["id"]
    assert gone_recipient in sqlite_db.get_target_recipient_ids(conn, t1)

    others_before = _snapshot(conn, f"id != {gone_recipient}")
    users_before = [tuple(r) for r in conn.execute("SELECT * FROM users WHERE id != ?", (gone,))]
    links_before = [
        tuple(r)
        for r in conn.execute(
            "SELECT * FROM target_recipients WHERE recipient_id != ? ORDER BY 1, 2", (gone_recipient,)
        )
    ]

    assert sqlite_db.delete_user(conn, gone) is True
    assert sqlite_db.get_user(conn, gone) is None
    assert sqlite_db.get_recipient(conn, gone_recipient) is None
    assert (
        conn.execute("SELECT COUNT(*) FROM target_recipients WHERE recipient_id = ?", (gone_recipient,)).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM target_alert_users WHERE user_id = ?", (gone,)).fetchone()[0] == 0
    assert _snapshot(conn) == others_before
    assert [tuple(r) for r in conn.execute("SELECT * FROM users")] == users_before
    assert [tuple(r) for r in conn.execute("SELECT * FROM target_recipients ORDER BY 1, 2")] == links_before
    assert conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 2 and t2
    assert sqlite_db.delete_user(conn, gone) is False
    assert_one_to_one(conn)


def test_delete_user_removes_the_recipient_even_without_foreign_key_enforcement(conn):
    uid = _user(conn, "nofk", email="n@e.org")
    conn.execute("PRAGMA foreign_keys=OFF")
    assert sqlite_db.delete_user(conn, uid) is True
    assert conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 0


# ─── E9: failed and concurrent writes ───────────────────────────────────────


def test_failure_while_creating_the_recipient_leaves_no_user(conn, monkeypatch: pytest.MonkeyPatch):
    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(sqlite_db.users, "update_recipient", boom)  # fails after the user row and trigger
    with pytest.raises(RuntimeError):
        _user(conn, "half")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 0
    assert not conn.in_transaction


def test_duplicate_phone_or_username_writes_nothing(conn):
    _user(conn, "first", phone=PHONE)
    before = (_snapshot(conn), conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    with pytest.raises(sqlite_db.DuplicatePhoneError):
        _user(conn, "second", phone=PHONE)
    with pytest.raises(sqlite3.IntegrityError, match=r"users\.username"):
        _user(conn, "first", phone=PHONE_2)
    assert (_snapshot(conn), conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]) == before
    other = _user(conn, "other", phone=PHONE_2)
    with pytest.raises(sqlite_db.DuplicatePhoneError):
        sqlite_db.update_user_recipient(
            conn, other, phone=PHONE, email="o@e.org", is_enabled=True, enable_for_all_targets=False
        )
    assert sqlite_db.get_user_recipient(conn, other)["phone"] == PHONE_2
    assert_one_to_one(conn)


def _race(db_path: Path, funcs: list) -> list:
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


def test_parallel_creates_with_the_same_phone_yield_exactly_one_user(db_path: Path):
    outcomes = _race(
        db_path, [lambda c, i=i: _user(c, f"racer{i}", phone=PHONE, email=f"r{i}@e.org") for i in range(6)]
    )
    assert sum(1 for kind, _ in outcomes if kind == "ok") == 1
    assert all(isinstance(v, sqlite_db.DuplicatePhoneError) for kind, v in outcomes if kind == "err")
    conn = sqlite_db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM recipients WHERE phone = ?", (PHONE,)).fetchone()[0] == 1
        assert_one_to_one(conn)
    finally:
        sqlite_db.close_connection(conn)


def test_parallel_creates_with_the_same_username_yield_exactly_one_user(db_path: Path):
    outcomes = _race(db_path, [lambda c: _user(c, "same") for _ in range(6)])
    assert sum(1 for kind, _ in outcomes if kind == "ok") == 1
    assert all(isinstance(v, sqlite3.IntegrityError) for kind, v in outcomes if kind == "err")
    conn = sqlite_db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] == 1
        assert_one_to_one(conn)
    finally:
        sqlite_db.close_connection(conn)


def test_delete_racing_a_recipient_update_ends_consistent(db_path: Path):
    for round_no in range(10):
        conn = sqlite_db.connect(db_path)
        uid = _user(conn, f"victim{round_no}", email=f"v{round_no}@e.org")
        sqlite_db.close_connection(conn)
        outcomes = _race(
            db_path,
            [
                lambda c, uid=uid: sqlite_db.delete_user(c, uid),
                lambda c, uid=uid, n=round_no: sqlite_db.update_user_recipient(
                    c, uid, phone=None, email=f"new{n}@e.org", is_enabled=True, enable_for_all_targets=False
                ),
            ],
        )
        assert outcomes[0][0] == "ok"
        assert outcomes[1][0] == "ok" or isinstance(outcomes[1][1], LookupError)
        conn = sqlite_db.connect(db_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM recipients WHERE user_id = ?", (uid,)).fetchone()[0] == 0
            assert_one_to_one(conn)
        finally:
            sqlite_db.close_connection(conn)


def test_parallel_bootstrap_creates_one_admin_with_one_recipient(db_path: Path, tmp_path: Path):
    bootstrap_credentials.configure(tmp_path)
    try:
        outcomes = _race(db_path, [sqlite_db.ensure_default_admin for _ in range(4)])
    finally:
        bootstrap_credentials.configure(None)
    assert all(kind == "ok" for kind, _ in outcomes)
    assert sum(1 for _, created in outcomes if created) == 1
    conn = sqlite_db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert_one_to_one(conn)
    finally:
        sqlite_db.close_connection(conn)


# ─── External review: admin protection inside the delete transaction ────────────────────────────────────────────
def test_delete_non_admin_user_refuses_an_admin_and_changes_nothing(conn):
    admin = sqlite_db.create_user(conn, username="boss", password_hash="stub", is_admin=True)  # noqa: S106
    before = _snapshot(conn)
    with pytest.raises(sqlite_db.AdminUserError):
        sqlite_db.delete_non_admin_user(conn, admin)
    assert sqlite_db.get_user(conn, admin) is not None
    assert _snapshot(conn) == before


def test_delete_non_admin_user_deletes_a_regular_user_with_its_recipient_and_reports_a_missing_one(conn):
    uid = _user(conn, "regular", email="r@e.org")
    rid = sqlite_db.get_user_recipient(conn, uid)["id"]
    assert sqlite_db.delete_non_admin_user(conn, uid) is True
    assert sqlite_db.get_user(conn, uid) is None and sqlite_db.get_recipient(conn, rid) is None
    assert sqlite_db.delete_non_admin_user(conn, uid) is False
    assert_one_to_one(conn)


def test_role_change_racing_the_delete_is_serialised_by_the_write_lock(conn, db_path: Path):
    uid = _user(conn, "racer", email="x@e.org")
    other = sqlite3.connect(db_path, timeout=10)
    other.execute("BEGIN IMMEDIATE")  # the promotion holds the write lock first ...
    other.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
    outcome: list[object] = []

    def delete() -> None:
        c = sqlite_db.connect(db_path)
        try:
            outcome.append(sqlite_db.delete_non_admin_user(c, uid))
        except sqlite_db.AdminUserError as exc:
            outcome.append(exc)
        finally:
            sqlite_db.close_connection(c)

    thread = threading.Thread(target=delete)
    thread.start()
    thread.join(0.5)
    assert thread.is_alive()  # ... so the delete waits instead of reading the old role
    other.commit()
    other.close()
    thread.join(30)
    assert len(outcome) == 1 and isinstance(outcome[0], sqlite_db.AdminUserError)
    assert sqlite_db.get_user(conn, uid)["is_admin"] == 1


# ─── External review: an empty or blank string is not a contact channel ─────────────────────────────────────────
BLANKS = ["", " ", "   ", "\t", "\n", " \t\r\n "]


@pytest.mark.parametrize("blank", BLANKS)
def test_database_refuses_a_blank_phone_as_the_only_channel_of_an_enabled_recipient(conn, blank: str):
    uid = _user(conn, "blankphone")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("UPDATE recipients SET phone = ?, email = NULL, is_enabled = 1 WHERE user_id = ?", (blank, uid))


@pytest.mark.parametrize("blank", BLANKS)
def test_database_refuses_a_blank_email_as_the_only_channel_of_an_enabled_recipient(conn, blank: str):
    uid = _user(conn, "blankmail")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("UPDATE recipients SET email = ?, phone = NULL, is_enabled = 1 WHERE user_id = ?", (blank, uid))


@pytest.mark.parametrize("blank", BLANKS)
def test_database_refuses_a_standalone_recipient_with_only_blank_channels(conn, blank: str):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO recipients(name, phone, email, is_enabled, created_at, updated_at) "
            "VALUES ('x', ?, ?, 0, 't', 't')",
            (blank, blank),
        )


def test_database_still_accepts_real_channels_and_a_disabled_channelless_user_recipient(conn):
    uid = _user(conn, "okay")
    conn.execute("UPDATE recipients SET phone = '', email = 'k@e.org', is_enabled = 1 WHERE user_id = ?", (uid,))
    conn.execute("UPDATE recipients SET phone = ' ', email = NULL, is_enabled = 0 WHERE user_id = ?", (uid,))
    conn.execute("INSERT INTO recipients(name, phone, created_at, updated_at) VALUES ('p', '+4915100000077', 't', 't')")
