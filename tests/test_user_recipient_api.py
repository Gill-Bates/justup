#!/usr/bin/env python3
#
# tests/test_user_recipient_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""HTTP contract of the user <-> recipient integration (/users and /recipients)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import recipients, users
from app.api.auth import get_current_user
from app.db import sqlite as sqlite_db
from app.utils import crypto
from app.utils.deps import get_conn
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect

PHONE = "+4915112345678"
PHONE_2 = "+4915100000002"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    # The Signal registration lookup talks to signal-cli; it is not part of this contract.
    monkeypatch.setattr(recipients, "_check_signal_registration", lambda _phone: None)


@pytest.fixture
def conn(db_path: Path):
    connection = sqlite_db.connect(db_path)
    yield connection
    sqlite_db.close_connection(connection)


def _mk(conn, name: str, *, admin: bool = False) -> dict:
    uid = sqlite_db.create_user(conn, username=name, password_hash="stub", is_admin=admin)  # noqa: S106
    return dict(sqlite_db.get_user(conn, uid))


@pytest.fixture
def admin(conn) -> dict:
    return _mk(conn, "root", admin=True)


@pytest.fixture
def client(conn, admin) -> TestClient:
    return _client(conn, admin)


def _client(conn, user: dict) -> TestClient:
    app = FastAPI()
    app.state.tsdb_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    app.include_router(users.router)
    app.include_router(recipients.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _new_user(name: str = "alice", **recipient) -> dict:
    body: dict = {"username": name, "password": "Passw0rd-long"}
    if recipient:
        body["recipient"] = recipient
    return body


def _counts(conn) -> tuple[int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0],
    )


def _recipient_of(conn, username: str) -> sqlite3.Row:
    uid = sqlite_db.get_user_by_username(conn, username)["id"]
    return sqlite_db.get_user_recipient(conn, uid)


# User creation


def test_post_users_creates_user_and_recipient_together(client, conn):
    before = _counts(conn)
    response = client.post(
        "/users",
        json=_new_user(phone=PHONE, email="alice@example.org", is_enabled=True, enable_for_all_targets=False),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    recipient = body["recipient"]
    assert (recipient["phone"], recipient["email"], recipient["is_enabled"]) == (PHONE, "alice@example.org", True)
    assert _counts(conn) == (before[0] + 1, before[1] + 1)
    row = _recipient_of(conn, "alice")
    assert row["id"] == recipient["id"] and row["user_id"] == body["id"]


def test_post_users_without_channels_creates_a_disabled_channelless_recipient(client, conn):
    body = client.post("/users", json=_new_user("bob")).json()
    assert body["recipient"] == {
        "id": _recipient_of(conn, "bob")["id"],
        "phone": None,
        "email": None,
        "is_enabled": False,
        "enable_for_all_targets": False,
    }
    # Unset "enabled" without a channel is stored disabled ...
    client.post("/users", json=_new_user("carl", enable_for_all_targets=True))
    assert _recipient_of(conn, "carl")["is_enabled"] == 0


def test_post_users_with_explicit_enable_but_no_channel_is_422_and_writes_nothing(client, conn):
    before = _counts(conn)
    response = client.post("/users", json=_new_user("eager", is_enabled=True))
    assert response.status_code == 422 and "cannot be enabled" in response.text
    assert _counts(conn) == before


def test_post_users_with_a_blank_channel_switches_it_off(client, conn):
    client.post("/users", json=_new_user("blank", phone="", email="  "))
    row = _recipient_of(conn, "blank")
    assert (row["phone"], row["email"], row["is_enabled"]) == (None, None, 0)


@pytest.mark.parametrize(
    "recipient",
    [{"email": "not-an-email"}, {"phone": "0151123"}, {"phone": "+49 151 123"}, {"phone": "+" + "1" * 40}],
)
def test_post_users_with_invalid_channel_is_422_and_writes_nothing(client, conn, recipient):
    before = _counts(conn)
    assert client.post("/users", json=_new_user("bad", **recipient)).status_code == 422
    assert _counts(conn) == before


def test_post_users_with_duplicate_phone_is_409_and_writes_nothing(client, conn):
    sqlite_db.create_recipient(conn, name="ops", phone=PHONE)
    before = _counts(conn)
    response = client.post("/users", json=_new_user("dup", phone=PHONE))
    assert response.status_code == 409 and "Phone number already exists" in response.json()["detail"]
    assert _counts(conn) == before
    assert sqlite_db.get_user_by_username(conn, "dup") is None


def test_post_users_with_duplicate_username_is_409_and_adds_no_recipient(client, conn):
    client.post("/users", json=_new_user("twin"))
    before = _counts(conn)
    response = client.post("/users", json=_new_user("TWIN", email="t@example.org"))
    assert response.status_code == 409 and "Username" in response.json()["detail"]
    assert _counts(conn) == before


def test_duplicate_email_is_allowed_like_for_standalone_recipients(client):
    assert client.post("/users", json=_new_user("e1", email="same@example.org")).status_code == 200
    assert client.post("/users", json=_new_user("e2", email="same@example.org")).status_code == 200
    assert client.post("/recipients", json={"name": "x", "email": "same@example.org"}).status_code == 201


# User and recipient updates


def test_put_users_updates_user_and_recipient_atomically(client, conn):
    created = client.post("/users", json=_new_user("dave")).json()
    response = client.put(
        f"/users/{created['id']}",
        json={
            "username": "david",
            "is_admin": False,
            "can_close_alerts": True,
            "recipient": {
                "phone": PHONE,
                "email": "d@example.org",
                "is_enabled": True,
                "enable_for_all_targets": False,
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["username"] == "david" and body["can_close_alerts"] is True
    assert body["recipient"]["id"] == created["recipient"]["id"]
    row = sqlite_db.get_recipient(conn, body["recipient"]["id"])
    assert (row["name"], row["phone"], row["email"], row["is_enabled"], row["user_id"]) == (
        "david",
        PHONE,
        "d@example.org",
        1,
        created["id"],
    )


def test_put_users_without_recipient_key_leaves_the_recipient_alone(client, conn):
    created = client.post("/users", json=_new_user("erin", phone=PHONE, email="e@example.org")).json()
    before = tuple(sqlite_db.get_recipient(conn, created["recipient"]["id"]))
    assert client.put(f"/users/{created['id']}", json={"username": "erin"}).status_code == 200
    assert tuple(sqlite_db.get_recipient(conn, created["recipient"]["id"])) == before


def test_put_users_clearing_all_channels_needs_the_recipient_switched_off(client, conn):
    created = client.post("/users", json=_new_user("fay", phone=PHONE)).json()
    url = f"/users/{created['id']}"
    before = tuple(sqlite_db.get_recipient(conn, created["recipient"]["id"]))
    explicit = client.put(url, json={"username": "fay", "recipient": {"phone": None, "is_enabled": True}})
    assert explicit.status_code == 422
    assert tuple(sqlite_db.get_recipient(conn, created["recipient"]["id"])) == before
    for recipient in ({"phone": None, "is_enabled": False}, {"phone": None}):
        body = client.put(url, json={"username": "fay", "recipient": recipient}).json()
        assert (body["recipient"]["phone"], body["recipient"]["is_enabled"]) == (None, False)
        sqlite_db.update_user_recipient(
            conn, created["id"], phone=PHONE, email=None, is_enabled=True, enable_for_all_targets=False
        )


def test_put_users_with_duplicate_phone_changes_nothing(client, conn):
    sqlite_db.create_recipient(conn, name="ops", phone=PHONE)
    created = client.post("/users", json=_new_user("gil", email="g@example.org", phone=PHONE_2)).json()
    rid = created["recipient"]["id"]
    user_before, recipient_before = (
        tuple(sqlite_db.get_user(conn, created["id"])),
        tuple(sqlite_db.get_recipient(conn, rid)),
    )
    response = client.put(
        f"/users/{created['id']}",
        json={"username": "renamed", "is_admin": True, "recipient": {"phone": PHONE, "email": "other@example.org"}},
    )
    assert response.status_code == 409 and "Phone" in response.json()["detail"]
    assert tuple(sqlite_db.get_user(conn, created["id"])) == user_before
    assert tuple(sqlite_db.get_recipient(conn, rid)) == recipient_before


def test_put_users_with_invalid_email_is_422_and_changes_nothing(client, conn):
    created = client.post("/users", json=_new_user("hal", email="h@example.org")).json()
    user_before = tuple(sqlite_db.get_user(conn, created["id"]))
    response = client.put(f"/users/{created['id']}", json={"username": "renamed", "recipient": {"email": "broken"}})
    assert response.status_code == 422
    assert tuple(sqlite_db.get_user(conn, created["id"])) == user_before


def test_put_users_keeps_own_phone_number(client):
    created = client.post("/users", json=_new_user("ian", phone=PHONE)).json()
    response = client.put(
        f"/users/{created['id']}", json={"username": "ian", "recipient": {"phone": PHONE, "is_enabled": True}}
    )
    assert response.status_code == 200


def test_user_dialog_and_recipient_dialog_share_the_recipient(client):
    created = client.post("/users", json=_new_user("jo")).json()
    rid = created["recipient"]["id"]
    # User dialog -> recipient views.
    client.put(
        f"/users/{created['id']}", json={"username": "jo", "recipient": {"email": "jo@example.org", "is_enabled": True}}
    )
    listed = {r["id"]: r for r in client.get("/recipients").json()}
    assert listed[rid]["email"] == "jo@example.org" and listed[rid]["is_user_linked"] is True
    assert client.get(f"/recipients/{rid}").json()["email"] == "jo@example.org"
    # Recipient dialog -> user view.
    patched = client.patch(f"/recipients/{rid}", json={"phone": PHONE, "enable_for_all_targets": True})
    assert patched.status_code == 200, patched.text
    user = next(u for u in client.get("/users").json() if u["id"] == created["id"])
    assert user["recipient"]["id"] == rid
    assert (user["recipient"]["phone"], user["recipient"]["enable_for_all_targets"]) == (PHONE, True)


def test_get_users_lists_recipient_id_and_channels(client):
    client.post("/users", json=_new_user("kim", phone=PHONE))
    by_name = {u["username"]: u for u in client.get("/users").json()}
    assert by_name["root"]["recipient"]["phone"] is None and by_name["root"]["recipient"]["is_enabled"] is False
    assert by_name["kim"]["recipient"]["phone"] == PHONE


# Recipient API access


def test_patch_user_recipient_is_allowed_and_keeps_the_link(client, conn):
    created = client.post("/users", json=_new_user("lee")).json()
    rid = created["recipient"]["id"]
    response = client.patch(
        f"/recipients/{rid}",
        json={"name": "Lee (on call)", "email": "lee@example.org", "is_enabled": True, "user_id": 1},
    )
    assert response.status_code == 200, response.text
    row = sqlite_db.get_recipient(conn, rid)
    assert (row["name"], row["email"], row["is_enabled"], row["user_id"]) == (
        "Lee (on call)",
        "lee@example.org",
        1,
        created["id"],
    )
    assert response.json()["recipient"]["is_user_linked"] is True


def test_patch_user_recipient_may_drop_all_channels_only_while_disabled(client, conn):
    created = client.post("/users", json=_new_user("max", phone=PHONE)).json()
    rid = created["recipient"]["id"]
    for body in ({"phone": None, "is_enabled": True}, {"phone": None}):  # would leave an enabled recipient
        response = client.patch(f"/recipients/{rid}", json=body)
        assert response.status_code == 422 and "cannot be enabled" in response.json()["detail"]
        assert sqlite_db.get_recipient(conn, rid)["phone"] == PHONE
    response = client.patch(f"/recipients/{rid}", json={"phone": None, "is_enabled": False})
    assert response.status_code == 200, response.text
    row = sqlite_db.get_recipient(conn, rid)
    assert (row["phone"], row["email"], row["is_enabled"]) == (None, None, 0)
    assert client.patch(f"/recipients/{rid}", json={"is_enabled": True}).status_code == 422


def test_patch_standalone_recipient_still_needs_a_channel(client):
    rid = client.post("/recipients", json={"name": "ops", "email": "ops@example.org"}).json()["recipient"]["id"]
    assert client.patch(f"/recipients/{rid}", json={"email": None}).status_code == 400


def test_recipient_api_cannot_create_or_move_the_user_link(client, conn):
    other = _mk(conn, "other")
    response = client.post("/recipients", json={"name": "sneaky", "email": "s@example.org", "user_id": other["id"]})
    assert response.status_code == 201
    assert sqlite_db.get_recipient(conn, response.json()["recipient"]["id"])["user_id"] is None
    other_recipient = sqlite_db.get_user_recipient(conn, other["id"])
    client.patch(
        f"/recipients/{response.json()['recipient']['id']}", json={"user_id": other["id"], "name": "still free"}
    )
    assert sqlite_db.get_recipient(conn, response.json()["recipient"]["id"])["user_id"] is None
    assert sqlite_db.get_user_recipient(conn, other["id"])["id"] == other_recipient["id"]
    assert "user_id" not in client.get("/recipients").json()[0]


def test_delete_user_bound_recipient_is_409_and_standalone_is_204(client, conn):
    created = client.post("/users", json=_new_user("noa", email="n@example.org")).json()
    rid = created["recipient"]["id"]
    response = client.delete(f"/recipients/{rid}")
    assert response.status_code == 409 and "application user" in response.json()["detail"]
    assert sqlite_db.get_recipient(conn, rid)["user_id"] == created["id"]
    free = client.post("/recipients", json={"name": "ops", "email": "ops@example.org"}).json()["recipient"]["id"]
    assert client.delete(f"/recipients/{free}").status_code == 204
    assert client.delete("/recipients/99999").status_code == 404


def test_recipient_list_marks_user_recipients_and_keeps_channelless_ones(client):
    client.post("/users", json=_new_user("ola"))
    client.post("/recipients", json={"name": "ops", "email": "ops@example.org"})
    listed = {r["name"]: r for r in client.get("/recipients").json()}
    assert listed["ola"]["is_user_linked"] is True and listed["ola"]["phone"] is None and listed["ola"]["email"] is None
    assert listed["root"]["is_user_linked"] is True
    assert listed["ops"]["is_user_linked"] is False


# User deletion


def test_delete_user_removes_recipient_and_assignments_and_spares_everyone_else(client, conn):
    target = sqlite_db.create_target(
        conn, {"name": "t", "host": "t.example.com", "interval_seconds": 60, "retention_days": 30}
    )
    keep = client.post("/users", json=_new_user("keep", email="k@example.org", enable_for_all_targets=True)).json()
    gone = client.post("/users", json=_new_user("gone", phone=PHONE, enable_for_all_targets=True)).json()
    standalone = client.post("/recipients", json={"name": "ops", "email": "o@example.org"}).json()["recipient"]["id"]
    sqlite_db.set_target_recipients(conn, target, [*sqlite_db.get_target_recipient_ids(conn, target), standalone])
    others_before = [
        tuple(r) for r in conn.execute("SELECT * FROM recipients WHERE id != ? ORDER BY id", (gone["recipient"]["id"],))
    ]
    users_before = [tuple(r) for r in conn.execute("SELECT * FROM users WHERE id != ? ORDER BY id", (gone["id"],))]
    assert gone["recipient"]["id"] in sqlite_db.get_target_recipient_ids(conn, target)

    assert client.delete(f"/users/{gone['id']}").status_code == 204

    assert sqlite_db.get_recipient(conn, gone["recipient"]["id"]) is None
    assert gone["recipient"]["id"] not in sqlite_db.get_target_recipient_ids(conn, target)
    assert [tuple(r) for r in conn.execute("SELECT * FROM recipients ORDER BY id")] == others_before
    assert [tuple(r) for r in conn.execute("SELECT * FROM users ORDER BY id")] == users_before
    assert sorted(sqlite_db.get_target_recipient_ids(conn, target)) == sorted([keep["recipient"]["id"], standalone])
    assert client.get(f"/recipients/{gone['recipient']['id']}").status_code == 404


def test_existing_delete_rules_for_users_are_unchanged(client, conn, admin):
    other_admin = _mk(conn, "boss", admin=True)
    assert client.delete(f"/users/{other_admin['id']}").status_code == 403
    assert client.delete(f"/users/{admin['id']}").status_code == 403
    assert client.delete("/users/99999").status_code == 404
    assert sqlite_db.get_user_recipient(conn, other_admin["id"]) is not None


def test_only_admins_can_use_the_user_endpoints(conn):
    plain = _mk(conn, "plain")
    target = _mk(conn, "target")
    member = _client(conn, plain)
    assert member.post("/users", json=_new_user("x")).status_code == 403
    assert member.put(f"/users/{target['id']}", json={"username": "target"}).status_code == 403
    assert member.delete(f"/users/{target['id']}").status_code == 403
    assert member.get("/users").status_code == 403
    assert member.delete(f"/recipients/{sqlite_db.get_user_recipient(conn, target['id'])['id']}").status_code == 403


# Recipient lookup, repair, assignments and pending notifications


def test_me_returns_the_recipient_like_the_admin_list(conn):
    member = _mk(conn, "mia")
    sqlite_db.update_user_recipient(
        conn, member["id"], phone=PHONE, email=None, is_enabled=True, enable_for_all_targets=False
    )
    own = _client(conn, member).get("/me").json()
    listed = next(
        u for u in _client(conn, _mk(conn, "boss", admin=True)).get("/users").json() if u["id"] == member["id"]
    )
    assert own["recipient"] is not None and own["recipient"] == listed["recipient"]
    assert own["recipient"]["phone"] == PHONE


def test_missing_recipient_is_repaired_and_never_reported_as_null(conn, admin):
    """A user whose recipient vanished (database changed around the application) is repaired on read."""
    victim = _mk(conn, "victim")
    for name in ("trg_users_create_recipient", "trg_recipients_guard_delete", "trg_recipients_user_id_immutable"):
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DELETE FROM recipients WHERE user_id = ?", (victim["id"],))
    conn.commit()
    assert sqlite_db.get_user_recipient(conn, victim["id"]) is None
    users_list = _client(conn, admin).get("/users").json()
    assert all(u["recipient"] is not None for u in users_list)
    conn.execute("DELETE FROM recipients WHERE user_id = ?", (victim["id"],))
    conn.commit()
    me = _client(conn, victim).get("/me").json()
    assert me["recipient"] is not None and me["recipient"]["is_enabled"] is False


def test_all_targets_flag_adds_on_and_removes_every_assignment_off(client, conn):
    t1, t2 = (
        sqlite_db.create_target(
            conn, {"name": n, "host": f"{n}.example.com", "interval_seconds": 60, "retention_days": 30}
        )
        for n in ("a", "b")
    )
    created = client.post("/users", json=_new_user("nell", email="n@example.org")).json()
    rid, url = created["recipient"]["id"], f"/users/{created['id']}"
    sqlite_db.set_target_recipients(conn, t1, [rid])  # a manual assignment
    on = {"username": "nell", "recipient": {"email": "n@example.org", "enable_for_all_targets": True}}
    assert client.put(url, json=on).status_code == 200
    assert sqlite_db.get_target_recipient_ids(conn, t1) == [rid] and sqlite_db.get_target_recipient_ids(conn, t2) == [
        rid
    ]
    t3 = sqlite_db.create_target(
        conn, {"name": "c", "host": "c.example.com", "interval_seconds": 60, "retention_days": 30}
    )
    sqlite_db.assign_flagged_recipients(conn, t3)  # what target creation does for flagged recipients
    conn.commit()
    assert sqlite_db.get_target_recipient_ids(conn, t3) == [rid]
    off = {"username": "nell", "recipient": {"email": "n@example.org", "enable_for_all_targets": False}}
    assert client.put(url, json=off).status_code == 200
    assert sqlite_db.get_recipient_targets(conn, rid) == []  # the same rule as PATCH /recipients: all of them go


def _queue(conn, recipient_id: int, status: str) -> int:
    return conn.execute(
        "INSERT INTO notification_queue(notification_type, channel_type, recipient_id, recipient_name, "
        "recipient_address, payload, status, next_retry_at, created_at, updated_at) "
        "VALUES ('alert', 'email', ?, 'x', 'x@example.org', '{}', ?, 't', 't', 't')",
        (recipient_id, status),
    ).lastrowid


def _status(conn, queue_id: int) -> tuple[str, str | None]:
    row = conn.execute("SELECT status, last_error FROM notification_queue WHERE id = ?", (queue_id,)).fetchone()
    return row["status"], row["last_error"]


def test_deleting_a_user_cancels_its_pending_notifications_only(client, conn):
    gone = client.post("/users", json=_new_user("quit", email="q@example.org")).json()
    stay = client.post("/users", json=_new_user("stay", email="s@example.org")).json()
    pending, sending, sent, other = (
        _queue(conn, gone["recipient"]["id"], "pending"),
        _queue(conn, gone["recipient"]["id"], "processing"),
        _queue(conn, gone["recipient"]["id"], "sent"),
        _queue(conn, stay["recipient"]["id"], "pending"),
    )
    conn.commit()
    assert client.delete(f"/users/{gone['id']}").status_code == 204
    assert _status(conn, pending) == ("failed", "[PERMANENT] recipient was deleted")
    # A message that is already being sent cannot be recalled (documented limit); history and others stay.
    assert _status(conn, sending)[0] == "processing" and _status(conn, sent)[0] == "sent"
    assert _status(conn, other)[0] == "pending"


def test_failed_attempt_of_a_deleted_recipients_in_flight_message_is_final(client, conn, db_path):
    from app.services.notifications.spooler import NotificationSpooler

    rid = client.post("/recipients", json={"name": "ops", "email": "o@example.org"}).json()["recipient"]["id"]
    sending = _queue(conn, rid, "processing")
    conn.execute("UPDATE notification_queue SET created_at = ? WHERE id = ?", (utcnow().isoformat(), sending))
    conn.commit()
    assert client.delete(f"/recipients/{rid}").status_code == 204
    spooler = NotificationSpooler(lambda: sqlite_db.connect(db_path))
    assert spooler.mark_retry(sending, "smtp down") is False
    assert _status(conn, sending) == ("failed", "[PERMANENT] recipient was deleted")


@pytest.mark.parametrize("status, aged", [("processing", True), ("pending", False)])
def test_recovery_never_resends_a_message_of_a_deleted_recipient(conn, db_path, status, aged):
    """Pending and aged processing rows of deleted recipients must never be retried."""
    from datetime import timedelta

    from app.db.sqlite.recipients import DELETED_RECIPIENT_ID
    from app.services.notifications.spooler import NotificationSpooler

    now = utcnow()
    stuck = _queue(conn, DELETED_RECIPIENT_ID, status)
    stamp = (now - timedelta(seconds=3600 if aged else 0)).isoformat()
    conn.execute(
        "UPDATE notification_queue SET created_at = ?, updated_at = ?, next_retry_at = ? WHERE id = ?",
        (now.isoformat(), stamp, (now - timedelta(seconds=5)).isoformat(), stuck),
    )
    conn.commit()
    spooler = NotificationSpooler(lambda: sqlite_db.connect(db_path))
    assert spooler.get_pending_items() == []
    assert _status(conn, stuck) == ("failed", "[PERMANENT] recipient was deleted")


def test_deleting_a_standalone_recipient_cancels_its_pending_notifications(client, conn):
    rid = client.post("/recipients", json={"name": "ops", "email": "o@example.org"}).json()["recipient"]["id"]
    pending = _queue(conn, rid, "pending")
    conn.commit()
    assert client.delete(f"/recipients/{rid}").status_code == 204
    assert _status(conn, pending)[0] == "failed"


# Interleaved request validation


def _second_connection(conn) -> sqlite3.Connection:
    """Another connection to the same file, i.e. what a parallel request would use."""
    other = sqlite3.connect(
        Path(conn.execute("PRAGMA database_list").fetchone()[2]), timeout=10, check_same_thread=False
    )
    other.row_factory = sqlite3.Row
    return other


def _interleave_after_first_call(monkeypatch: pytest.MonkeyPatch, name: str, action) -> list[int]:
    """Run ``action`` once, right after the first call of ``sqlite_db.<name>`` returned (the pre-check read)."""
    real = getattr(sqlite_db, name)
    fired: list[int] = []

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(1)
            action()
        return result

    monkeypatch.setattr(sqlite_db, name, wrapper)
    return fired


def test_parallel_channel_removals_are_rejected_as_422_not_500(client, conn, monkeypatch: pytest.MonkeyPatch):
    body = client.post("/users", json=_new_user("pair", phone=PHONE, email="pair@example.org", is_enabled=True)).json()
    rid = body["recipient"]["id"]
    other = _second_connection(conn)

    def request_b_removes_the_email() -> None:
        other.execute("UPDATE recipients SET email = NULL WHERE id = ?", (rid,))
        other.commit()

    fired = _interleave_after_first_call(monkeypatch, "get_recipient", request_b_removes_the_email)
    response = client.patch(f"/recipients/{rid}", json={"phone": None})  # request A removes the phone
    other.close()
    assert fired
    assert response.status_code == 422, response.text
    row = sqlite_db.get_recipient(conn, rid)
    assert (row["phone"], row["email"], row["is_enabled"]) == (PHONE, None, 1)  # only request B took effect


def test_parallel_removal_on_a_standalone_recipient_keeps_the_channel_rule(client, conn, monkeypatch):
    rid = sqlite_db.create_recipient(conn, name="ops", phone=PHONE, email="ops@example.org")
    other = _second_connection(conn)

    def request_b_removes_the_email() -> None:
        other.execute("UPDATE recipients SET email = NULL WHERE id = ?", (rid,))
        other.commit()

    _interleave_after_first_call(monkeypatch, "get_recipient", request_b_removes_the_email)
    response = client.patch(f"/recipients/{rid}", json={"phone": None})
    other.close()
    assert response.status_code == 400, response.text  # the same answer as for the sequential order
    assert sqlite_db.get_recipient(conn, rid)["phone"] == PHONE


def test_check_constraint_conflict_of_a_write_is_422_not_500(client, conn, monkeypatch: pytest.MonkeyPatch):
    rid = sqlite_db.create_recipient(conn, name="ops", phone=PHONE, email="ops@example.org")

    def conflict(*_a, **_k):
        raise sqlite3.IntegrityError("CHECK constraint failed: is_enabled = 0 OR phone IS NOT NULL")

    monkeypatch.setattr(sqlite_db, "update_recipient", conflict)
    response = client.patch(f"/recipients/{rid}", json={"name": "renamed"})
    assert response.status_code == 422, response.text


# Admin protection during role changes


def test_delete_of_a_user_promoted_in_between_is_refused_and_deletes_nothing(client, conn, monkeypatch):
    uid = _mk(conn, "promoted")["id"]
    other = _second_connection(conn)

    def promoted_to_admin() -> None:
        other.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
        other.commit()

    fired = _interleave_after_first_call(monkeypatch, "get_user", promoted_to_admin)
    response = client.delete(f"/users/{uid}")
    other.close()
    assert fired
    assert response.status_code == 403, response.text
    assert sqlite_db.get_user(conn, uid)["is_admin"] == 1
    assert sqlite_db.get_user_recipient(conn, uid) is not None  # the recipient stayed as well
