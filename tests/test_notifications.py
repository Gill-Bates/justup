#!/usr/bin/env python3
#
# tests/test_notifications.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Notification inbox: events, visibility, read state, deduplication, delivery and report access."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import notifications as inbox
from app.db import sqlite as sqlite_db
from app.models.users import hash_password, hash_token, new_token
from app.services.notifications.spooler import NotificationSpooler
from app.utils import crypto
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def db(tmp_path: Path):
    path = tmp_path / "n.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    yield conn, path
    sqlite_db.close_connection(conn)


def _user(conn, name: str, *, admin: bool = False) -> dict:
    uid = sqlite_db.create_user(conn, username=name, password_hash="x", is_admin=admin)  # noqa: S106
    return {"id": uid, "username": name, "is_admin": 1 if admin else 0}


def _target(conn, name: str = "T1") -> int:
    cur = conn.execute(
        "INSERT INTO targets (name, interval_seconds, retention_days, enable_ping, enable_http_check, "
        "enable_cert_expiration, host, created_at, updated_at) VALUES (?, 60, 30, 1, 0, 0, 'h', ?, ?)",
        (name, utcnow(), utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _alert(conn, tid: int, aid: str = "a1"):
    sqlite_db.create_alert(
        conn,
        alert_id=aid,
        target_id=tid,
        target_name="T1",
        failed_service="PING",
        started_at=utcnow(),
        recipients_notified=[],
    )


# ─── Events and transactions ────────────────────────────────────────────────


def test_alert_opened_and_resolved_create_one_notification_each(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    tid = _target(conn)
    _alert(conn, tid)
    sqlite_db.resolve_alert(conn, "a1", utcnow() + timedelta(seconds=5))
    sqlite_db.resolve_alert(conn, "a1", utcnow() + timedelta(seconds=9))  # repeated: no duplicate
    items, total = inbox.list_notifications(conn, admin)
    assert total == 2
    assert {i["event_type"] for i in items} == {"monitoring.alert.opened", "monitoring.alert.resolved"}


def test_no_notification_when_the_transaction_fails(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    with pytest.raises(RuntimeError), sqlite_db.transaction(conn):
        inbox.notify_target_created(conn, target_id=1, name="X", actor=admin)
        raise RuntimeError("boom")
    assert inbox.count_unread(conn, admin) == 0


def test_duplicate_events_are_ignored_across_parallel_writers(db):
    conn, path = db
    admin = _user(conn, "root", admin=True)
    errors: list[BaseException] = []

    def writer():
        c = sqlite_db.connect(path)
        try:
            inbox.notify_user_created(c, user_id=99, username="n", actor=admin)
            c.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            sqlite_db.close_connection(c)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert inbox.list_notifications(conn, admin)[1] == 1


# ─── Visibility and read state ──────────────────────────────────────────────


def test_visibility_and_per_user_unread_counts(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    alice = _user(conn, "alice")
    bob = _user(conn, "bob")
    tid = _target(conn)
    sqlite_db.set_target_alert_users(conn, tid, [alice["id"]])
    _alert(conn, tid)
    inbox.notify_admin_password_changed(conn, user_id=admin["id"], username="root", event_key="k1")
    inbox.notify_report(conn, stage="completed", job_id="j1", user_id=alice["id"], target_id=tid, target_name="T1")
    conn.commit()
    assert inbox.count_unread(conn, admin) == 2  # alert + admin password change; alice's PDF is private
    assert inbox.count_unread(conn, alice) == 2  # responsible for the target + own PDF
    assert inbox.count_unread(conn, bob) == 0  # no admin events, no foreign target
    assert inbox.mark_read(conn, admin, 1)
    assert inbox.count_unread(conn, admin) == 1
    assert inbox.count_unread(conn, alice) == 2  # read state is per user


def test_legacy_successful_login_notifications_are_hidden_without_deleting_history(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    notification_id = inbox.emit(
        conn,
        event_type="security.login.success",
        category="security",
        severity="info",
        title="Successful sign-in",
        idempotency_key="legacy-login:k1",
        audience=inbox.AUDIENCE_ADMINS,
    )
    conn.commit()

    assert notification_id is not None
    assert inbox.count_unread(conn, admin) == 0
    assert inbox.get_summary(conn, admin)["items"] == []
    assert inbox.list_notifications(conn, admin) == ([], 0)
    assert inbox.get_notification(conn, admin, notification_id) is None
    assert inbox.mark_read(conn, admin, notification_id) is False
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE id=?", (notification_id,)).fetchone()[0] == 1


def test_foreign_private_notification_cannot_be_read_or_marked(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    alice = _user(conn, "alice")
    bob = _user(conn, "bob")
    tid = _target(conn)
    inbox.notify_report(conn, stage="completed", job_id="j1", user_id=alice["id"], target_id=tid, target_name="T1")
    nid = inbox.list_notifications(conn, alice)[0][0]["id"]
    for other in (bob, admin):
        assert inbox.get_notification(conn, other, nid) is None
        assert inbox.mark_read(conn, other, nid) is False
    assert inbox.count_unread(conn, alice) == 1


def test_mark_read_does_not_change_the_alert(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    tid = _target(conn)
    _alert(conn, tid)
    inbox.mark_all_read(conn, admin)
    row = sqlite_db.get_alert(conn, "a1")
    assert row["status"] == "open" and not row["is_acknowledged"]
    sqlite_db.resolve_alert(conn, "a1", utcnow())
    assert inbox.count_unread(conn, admin) == 1  # resolved is a new unread event, independent of read


# ─── Reports ────────────────────────────────────────────────────────────────


def test_report_notifications_go_only_to_the_requester_and_are_not_duplicated(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    alice = _user(conn, "alice")
    tid = _target(conn)
    sqlite_db.create_report_job(conn, "job1", tid, user_id=alice["id"])
    for _ in range(2):  # worker repetition
        sqlite_db.update_report_job(conn, "job1", status="completed", filename="f.pdf", completed=True)
    sqlite_db.create_report_job(conn, "job2", tid, user_id=alice["id"])
    sqlite_db.update_report_job(conn, "job2", status="failed", error="Traceback secret", completed=True)
    items, total = inbox.list_notifications(conn, alice)
    assert total == 4  # requested x2, completed, failed
    assert {i["event_type"] for i in items} >= {"report.completed", "report.failed"}
    assert "secret" not in repr(items)
    assert inbox.list_notifications(conn, admin)[1] == 0


# ─── Delivery ───────────────────────────────────────────────────────────────


@pytest.fixture
def frozen_window(monkeypatch: pytest.MonkeyPatch):
    """Pin the clock to the start of a 10-minute slot so grouped sends never straddle a boundary."""
    from datetime import UTC, datetime

    fixed = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
    monkeypatch.setattr(inbox, "utcnow", lambda: fixed)


@pytest.fixture
def spooler(db):
    _, path = db
    return NotificationSpooler(lambda: sqlite_db.connect(path))


def _enqueue(sp):
    return sp.enqueue("alert", "email", 1, "R", "secret@example.com", {"subject": "s", "body": "private"})


def test_sent_only_after_real_delivery_and_retry_is_silent(db, spooler):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    qid = _enqueue(spooler)
    assert inbox.list_notifications(conn, admin)[1] == 0  # queued is not sent
    spooler.mark_retry(qid, "temporary")
    assert inbox.list_notifications(conn, admin)[1] == 0  # retries are silent
    spooler.mark_sent(qid)
    spooler.mark_sent(qid)  # repeated call: no second event
    items, _ = inbox.list_notifications(conn, admin)
    assert [i["event_type"] for i in items] == ["delivery.email.sent"]
    assert "secret@example.com" not in repr(items) and "private" not in repr(items)


@pytest.mark.usefixtures("frozen_window")
def test_sent_events_are_grouped_per_channel_window(db, spooler):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    for _ in range(3):
        spooler.mark_sent(_enqueue(spooler))
    items, total = inbox.list_notifications(conn, admin)
    assert total == 1 and items[0]["event_count"] == 3 and "(3)" in items[0]["title"]


@pytest.mark.usefixtures("frozen_window")
def test_grouped_delivery_after_read_becomes_unread_again(db, spooler):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    spooler.mark_sent(_enqueue(spooler))
    item = inbox.list_notifications(conn, admin)[0][0]
    seq_before = inbox.get_summary(conn, admin)["unread_event_seq"]
    assert inbox.mark_read(conn, admin, item["id"])
    assert inbox.count_unread(conn, admin) == 0  # admin has caught up

    # A further delivery folds into the same grouped row; it must surface as unread again so the
    # bell re-triggers instead of staying silently read.
    spooler.mark_sent(_enqueue(spooler))
    summary = inbox.get_summary(conn, admin)
    assert summary["unread"] == 1
    # The grouped row keeps its id, so latest_unread_id cannot change; the bell must instead
    # re-animate off a monotonic unread-activity sequence that does rise here.
    assert summary["latest_unread_id"] == item["id"]
    assert summary["unread_event_seq"] > seq_before
    spooler.mark_sent(1)  # repeated transition of an already sent item: no extra count
    assert inbox.list_notifications(conn, admin)[0][0]["event_count"] == 2
    again = inbox.list_notifications(conn, admin)[0][0]
    assert again["id"] == item["id"] and again["event_count"] == 2 and again["is_read"] is False


def test_permanent_failure_after_crash_recovery_notifies_once(db, spooler):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    qid = _enqueue(spooler)
    spooler.mark_failed_permanent(qid, "smtp rejected secret@example.com")
    spooler.mark_failed_permanent(qid, "again")
    items, total = inbox.list_notifications(conn, admin)
    assert total == 1 and items[0]["severity"] == "error" and items[0]["event_type"] == "delivery.email.failed"
    assert "secret@example.com" not in repr(items)


# ─── HTTP: report access, UI page, legacy redirect ──────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-notif-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        monkeypatch.delenv("SECRET_KEYS", raising=False)
        monkeypatch.setattr(crypto, "_primary_key", None)
        from app.main import create_app

        app = create_app()
        conn = sqlite_db.connect(app.state.db_path)
        sqlite_db.init_schema(conn)
        tokens: dict[str, str] = {}
        for name, admin in (("boss", True), ("alice", False), ("bob", False)):
            uid = sqlite_db.create_user(conn, username=name, password_hash=hash_password("Passw0rd!x1"), is_admin=admin)
            token, expires, max_expires = new_token()
            sqlite_db.issue_token(
                conn, user_id=uid, token_hash=hash_token(token), expires_at=expires, max_expires_at=max_expires
            )
            tokens[name] = token
        tid = _target(conn)
        alice_id = sqlite_db.get_user_by_username(conn, "alice")["id"]
        sqlite_db.create_report_job(conn, "alicejob", tid, user_id=alice_id)
        sqlite_db.update_report_job(conn, "alicejob", status="completed", filename="a.pdf", completed=True)
        sqlite_db.close_connection(conn)
        c = TestClient(app)
        c.get("/ui/login")
        yield c, tokens


def _h(tokens, name):
    return {"Authorization": f"Bearer {tokens[name]}"}


def test_report_job_access_owner_and_admin_only(client):
    c, tokens = client
    assert c.get("/reports/jobs/alicejob", headers=_h(tokens, "alice")).status_code == 200
    assert c.get("/reports/jobs/alicejob", headers=_h(tokens, "boss")).status_code == 200  # admin sees all
    assert c.get("/reports/jobs/alicejob", headers=_h(tokens, "bob")).status_code == 404
    assert c.get("/reports/jobs/alicejob/download", headers=_h(tokens, "bob")).status_code == 404


def test_expired_report_download_is_gone(client):
    c, tokens = client
    conn = sqlite_db.connect(c.app.state.db_path)
    conn.execute("UPDATE report_jobs SET expires_at = ?", (utcnow() - timedelta(minutes=1),))
    conn.commit()
    sqlite_db.close_connection(conn)
    assert c.get("/reports/jobs/alicejob/download", headers=_h(tokens, "alice")).status_code == 410


def test_download_action_only_on_ready_report_notification(client):
    c, tokens = client
    items = c.get("/ui/notifications/items?category=reports", headers=_h(tokens, "alice")).json()["items"]
    actions = {i["event_type"]: i["action"] for i in items}
    assert actions["report.requested"] is None
    assert actions["report.completed"]["type"] == "download"


def test_summary_endpoint_is_private_and_scoped(client):
    c, tokens = client
    r = c.get("/ui/notifications/summary", headers=_h(tokens, "alice"))
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    assert r.json()["unread"] == 2  # requested + completed
    assert c.get("/ui/notifications/summary", headers=_h(tokens, "bob")).json()["unread"] == 0
    assert c.get("/ui/notifications/summary").status_code == 401
    assert c.post("/ui/notifications/1/read", headers=_h(tokens, "bob")).status_code in (403, 404)


def test_notifications_page_renders_and_alerts_redirects(client):
    c, tokens = client
    c.cookies.set("token", tokens["boss"])
    page = c.get("/ui/notifications")
    assert page.status_code == 200 and "Notifications" in page.text
    mon = c.get("/ui/notifications?category=monitoring")
    assert mon.status_code == 200 and "alert-filters" in mon.text
    legacy = c.get("/ui/alerts?status=open", follow_redirects=False)
    assert legacy.status_code == 302 and legacy.headers["location"].startswith("/ui/notifications?category=monitoring")


def test_crash_recovered_item_yields_exactly_one_sent_event(db, spooler):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    qid = _enqueue(spooler)
    old = (utcnow() - timedelta(days=1)).isoformat()
    conn.execute("UPDATE notification_queue SET status='processing', updated_at=? WHERE id=?", (old, qid))
    conn.commit()
    assert [i.id for i in spooler.get_pending_items()] == [qid]  # recovery re-claims the stuck item
    assert inbox.list_notifications(conn, admin)[1] == 0  # recovery itself is not a delivery
    spooler.mark_sent(qid)
    assert inbox.list_notifications(conn, admin)[0][0]["event_count"] == 1


def test_backup_snapshot_keeps_notifications_and_read_state(db, tmp_path: Path):
    from app.utils import backup as backup_mod

    conn, path = db
    admin = _user(conn, "root", admin=True)
    inbox.notify_admin_password_changed(conn, user_id=admin["id"], username="root", event_key="a")
    inbox.notify_admin_password_changed(conn, user_id=admin["id"], username="root", event_key="b")
    inbox.mark_read(conn, admin, 1)
    conn.commit()
    snap = tmp_path / "snap"
    snap.mkdir()
    backup_mod.snapshot_sqlite_dir(path.parent, snap)  # online backup: includes uncheckpointed WAL data
    restored = sqlite_db.connect(snap / "n.db")
    try:
        sqlite_db.init_schema(restored)  # what the restarted application does after a restore
        assert inbox.count_unread(restored, admin) == 1
        assert inbox.list_notifications(restored, admin)[1] == 2
    finally:
        sqlite_db.close_connection(restored)


def test_admin_login_is_silent_but_required_password_change_notifies(client):
    c, _tokens = client
    conn = sqlite_db.connect(c.app.state.db_path)
    try:
        admin = dict(sqlite_db.get_user_by_username(conn, "boss"))
        sqlite_db.update_user_password(
            conn,
            int(admin["id"]),
            str(admin["password_hash"]),
            must_change_password=True,
        )
    finally:
        sqlite_db.close_connection(conn)

    first_login = c.post(
        "/login",
        json={"username": "boss", "password": "Passw0rd!x1"},
        headers={"X-CSRF-Token": _csrf(c)},
    )
    assert first_login.status_code == 200 and first_login.json()["must_change_password"] is True

    changed = c.post(
        "/me/complete-required-change",
        json={"new_password": "Admin-New-pass-2!"},
        headers={"X-CSRF-Token": _csrf(c)},
    )
    assert changed.status_code == 200

    second_login = c.post(
        "/login",
        json={"username": "boss", "password": "Admin-New-pass-2!"},
        headers={"X-CSRF-Token": _csrf(c)},
    )
    assert second_login.status_code == 200

    conn = sqlite_db.connect(c.app.state.db_path)
    try:
        items, total = inbox.list_notifications(conn, admin)
        assert total == 1
        assert items[0]["event_type"] == "security.admin_password.changed"
        assert items[0]["title"] == "Administrator password changed"
        login_events = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE event_type='security.login.success'"
        ).fetchone()[0]
        assert login_events == 0
    finally:
        sqlite_db.close_connection(conn)


def test_admin_reset_of_admin_password_creates_security_notification(client):
    c, tokens = client
    conn = sqlite_db.connect(c.app.state.db_path)
    try:
        admin = dict(sqlite_db.get_user_by_username(conn, "boss"))
    finally:
        sqlite_db.close_connection(conn)

    response = c.put(
        f"/users/{admin['id']}",
        json={
            "username": "boss",
            "is_admin": True,
            "can_close_alerts": True,
            "new_password": "Temporary-Admin-4!",
        },
        headers={**_h(tokens, "boss"), "X-CSRF-Token": _csrf(c)},
    )
    assert response.status_code == 200, response.text

    conn = sqlite_db.connect(c.app.state.db_path)
    try:
        items, total = inbox.list_notifications(conn, admin)
        assert total == 1
        assert items[0]["event_type"] == "security.admin_password.reset"
        assert items[0]["title"] == "Administrator password reset"
        assert "boss" in items[0]["body"]
    finally:
        sqlite_db.close_connection(conn)


def test_regular_user_password_change_does_not_notify_admins(client):
    c, tokens = client
    response = c.post(
        "/me/password",
        json={"current_password": "Passw0rd!x1", "new_password": "Alice-New-pass-3!"},
        headers={**_h(tokens, "alice"), "X-CSRF-Token": _csrf(c)},
    )
    assert response.status_code == 200, response.text
    conn = sqlite_db.connect(c.app.state.db_path)
    try:
        password_events = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE event_type LIKE 'security.admin_password.%'"
        ).fetchone()[0]
        assert password_events == 0
    finally:
        sqlite_db.close_connection(conn)


def test_database_without_inbox_tables_is_upgraded_in_place(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite_db.connect(path)
    sqlite_db.init_schema(conn)
    conn.execute("DROP TABLE notification_reads")
    conn.execute("DROP TABLE notifications")
    conn.execute("DROP INDEX idx_report_jobs_user")
    conn.execute("ALTER TABLE report_jobs DROP COLUMN user_id")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    sqlite_db.init_schema(conn)
    try:
        assert sqlite_db.get_schema_version(conn) == sqlite_db.SCHEMA_VERSION
        assert "user_id" in {r[1] for r in conn.execute("PRAGMA table_info(report_jobs)")}
        conn.execute("SELECT COUNT(*) FROM notification_reads").fetchone()
    finally:
        sqlite_db.close_connection(conn)


def test_expired_report_status_is_gone_and_terminal_state_is_entered_once(client):
    c, tokens = client
    conn = sqlite_db.connect(c.app.state.db_path)
    conn.execute("UPDATE report_jobs SET expires_at = ?", (utcnow() - timedelta(minutes=1),))
    conn.commit()
    sqlite_db.update_report_job(conn, "alicejob", status="failed", error="late", completed=True)  # already completed
    row = conn.execute("SELECT status FROM report_jobs WHERE job_id='alicejob'").fetchone()
    sqlite_db.close_connection(conn)
    assert row["status"] == "completed"
    assert c.get("/reports/jobs/alicejob", headers=_h(tokens, "alice")).status_code == 410


def test_retries_are_silent_until_the_final_permanent_failure(db, spooler):
    from app.services.notifications.spooler import MAX_RETRIES

    conn, _ = db
    admin = _user(conn, "root", admin=True)
    qid = _enqueue(spooler)
    for _ in range(MAX_RETRIES):
        assert spooler.mark_retry(qid, "temporary") is True
        assert inbox.list_notifications(conn, admin)[1] == 0
    assert spooler.mark_retry(qid, "still failing") is False  # retries exhausted
    assert spooler.mark_retry(qid, "late duplicate") is False
    items, total = inbox.list_notifications(conn, admin)
    assert total == 1 and items[0]["event_type"] == "delivery.email.failed"


def test_quiesce_refuses_notification_writes_and_the_triggering_operation(client):
    c, tokens = client
    assert sqlite_db.begin_quiesce(timeout=5) is True
    try:
        with pytest.raises(sqlite_db.DatabaseQuiescedError):
            sqlite_db.connect(c.app.state.db_path)
        # The CSRF token rotates after every validated POST, so fetch a fresh one per request.
        read = c.post("/ui/notifications/read-all", headers={**_h(tokens, "alice"), "X-CSRF-Token": _csrf(c)})
        login = c.post(
            "/login", json={"username": "alice", "password": "Passw0rd!x1"}, headers={"X-CSRF-Token": _csrf(c)}
        )
        assert read.status_code == 503 and login.status_code == 503  # existing contract: 503 + Retry-After
        assert read.headers["retry-after"] == "30"
    finally:
        sqlite_db.end_quiesce()
    conn = sqlite_db.connect(c.app.state.db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM notifications WHERE event_type='security.login.success'").fetchone()[0]
            == 0
        )
    finally:
        sqlite_db.close_connection(conn)


def _csrf(c) -> str:
    c.get("/ui/login")
    return c.cookies.get("csrf_token")


# ─── SMTP transport unification (DECISIONS B6) ──────────────────────────────

_TEST_PASSWORD = "p"  # noqa: S105 - dummy credential for fake SMTP, never a real secret


class _RecordingSMTP:
    """Fake smtplib.SMTP that records whether the link was protected before send."""

    instances: ClassVar[list[_RecordingSMTP]] = []

    def __init__(self, host, port, timeout=None):  # noqa: ARG002 - fake mirrors smtplib signature
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in = None
        self.sent: list = []
        self.starttls_available = True
        _RecordingSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo_or_helo_if_needed(self):
        pass

    def ehlo(self):
        pass

    def has_extn(self, name):
        return name == "starttls" and self.starttls_available

    def starttls(self, context=None):
        assert context is not None  # policy: a verified ssl context is always passed
        self.started_tls = True

    def login(self, user, password):
        assert self.started_tls, "login before STARTTLS"
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def _email_sender_with_config(path: Path, **settings):
    """Build an EmailSender over a DB populated with the given SMTP settings."""
    from app.services.notifications.email_sender import EmailSender
    from app.utils.constants import SettingKeys

    conn = sqlite_db.connect(path)
    try:
        sqlite_db.set_setting(conn, SettingKeys.SMTP_ENABLED, True)
        sqlite_db.set_setting(conn, SettingKeys.SMTP_HOST, settings.get("host", "mail.example.com"))
        sqlite_db.set_setting(conn, SettingKeys.SMTP_PORT, settings.get("port", 587))
        sqlite_db.set_setting(conn, SettingKeys.SMTP_FROM, "from@example.com")
        sqlite_db.set_setting(conn, SettingKeys.SMTP_USE_TLS, settings.get("use_tls", True))
        if settings.get("user"):
            sqlite_db.set_setting(conn, SettingKeys.SMTP_USER, settings["user"])
        if settings.get("password"):
            sqlite_db.set_setting(conn, SettingKeys.SMTP_PASSWORD, settings["password"])
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)
    return EmailSender(lambda: sqlite_db.connect(path))


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_email_sender_routes_through_hardened_transport_with_starttls(db, monkeypatch):
    """The EmailSender channel upgrades with STARTTLS before AUTH (shared transport)."""
    from app.services.notifications import smtp_transport
    from app.services.notifications.dispatcher import AlertPayload, RecipientInfo

    _, path = db
    _RecordingSMTP.instances = []
    monkeypatch.setattr(smtp_transport.smtplib, "SMTP", _RecordingSMTP)

    sender = _email_sender_with_config(path, port=587, use_tls=True, user="u", password=_TEST_PASSWORD)
    recipient = RecipientInfo(id=1, name="R", phone=None, email="to@example.com", is_enabled=True)
    alert = AlertPayload(
        alert_id="a1", target_id=1, target_name="T1", failed_service="PING", status="open", started_at=utcnow()
    )
    result = _run(sender.send(recipient, alert))

    assert result.success is True
    inst = _RecordingSMTP.instances[0]
    assert inst.started_tls is True and inst.logged_in == ("u", "p") and len(inst.sent) == 1


def test_email_sender_refuses_cleartext_when_starttls_unavailable(db, monkeypatch):
    """STARTTLS requested but unavailable -> no send, surfaced as a failed result not a crash."""
    from app.services.notifications import smtp_transport
    from app.services.notifications.dispatcher import AlertPayload, RecipientInfo

    _, path = db
    _RecordingSMTP.instances = []

    def _make(host, port, timeout=None):
        inst = _RecordingSMTP(host, port, timeout)
        inst.starttls_available = False
        return inst

    monkeypatch.setattr(smtp_transport.smtplib, "SMTP", _make)

    sender = _email_sender_with_config(path, port=587, use_tls=True, user="u", password=_TEST_PASSWORD)
    recipient = RecipientInfo(id=1, name="R", phone=None, email="to@example.com", is_enabled=True)
    alert = AlertPayload(
        alert_id="a1", target_id=1, target_name="T1", failed_service="PING", status="open", started_at=utcnow()
    )
    result = _run(sender.send(recipient, alert))

    assert result.success is False
    inst = _RecordingSMTP.instances[-1]
    assert inst.sent == [] and inst.logged_in is None


# ─── Context rendering ──────────────────────────────────────────────────────


def _admin_page(c, tokens, category="all") -> str:
    c.cookies.set("token", tokens["boss"])
    return c.get(f"/ui/notifications?category={category}").text


def test_creation_events_name_the_object_and_the_actor(db):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    tid = _target(conn, "example.com")
    inbox.notify_target_created(conn, target_id=tid, name="example.com", actor=admin)
    inbox.notify_user_created(conn, user_id=77, username="newbie", actor=admin)
    items, _ = inbox.list_notifications(conn, admin)
    by_type = {i["event_type"]: i for i in items}
    assert by_type["activity.target.created"]["title"] == "Target created: example.com"
    assert by_type["activity.target.created"]["body"] == "Created by root"
    assert by_type["activity.user.created"]["title"] == "User created: newbie"
    assert by_type["activity.user.created"]["actor"] == "root"


def test_monitoring_report_and_delivery_events_carry_their_context(db, spooler):
    conn, _ = db
    admin = _user(conn, "root", admin=True)
    alice = _user(conn, "alice")
    tid = _target(conn, "T1")
    _alert(conn, tid)
    sqlite_db.create_report_job(conn, "jx", tid, user_id=alice["id"])
    spooler.mark_sent(_enqueue(spooler))
    texts = {i["event_type"]: (i["title"], i["body"]) for i in inbox.list_notifications(conn, admin)[0]}
    assert texts["monitoring.alert.opened"] == ("Alert opened: T1", "Check: PING")
    assert texts["delivery.email.sent"][1].startswith("Channel: Email")
    assert inbox.list_notifications(conn, alice)[0][0]["title"] == "PDF report requested: T1"


def test_deleted_target_keeps_its_name_and_offers_no_link(client):
    c, tokens = client
    conn = sqlite_db.connect(c.app.state.db_path)
    boss = dict(sqlite_db.get_user_by_username(conn, "boss"))
    tid = _target(conn, "Gone Host")
    inbox.notify_target_created(conn, target_id=tid, name="Gone Host", actor=boss)
    conn.execute("DELETE FROM targets WHERE id = ?", (tid,))
    conn.commit()
    sqlite_db.close_connection(conn)
    html = _admin_page(c, tokens, "activity")
    assert "Target created: Gone Host" in html
    assert f'href="/ui/targets/{tid}"' not in html
    assert "Target no longer exists" in html


def test_names_are_escaped_in_the_page(client):
    c, tokens = client
    conn = sqlite_db.connect(c.app.state.db_path)
    boss = dict(sqlite_db.get_user_by_username(conn, "boss"))
    tid = _target(conn, "<script>alert(1)</script>")
    inbox.notify_target_created(conn, target_id=tid, name="<script>alert(1)</script>", actor=boss)
    inbox.notify_user_created(conn, user_id=90, username='"><img src=x onerror=alert(2)>', actor=boss)
    sqlite_db.close_connection(conn)
    html = _admin_page(c, tokens, "activity")
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_old_rows_without_context_render_without_none(client):
    c, tokens = client
    conn = sqlite_db.connect(c.app.state.db_path)
    conn.execute(
        "INSERT INTO notifications (event_type, category, severity, title, body, created_at, audience, "
        "idempotency_key) VALUES ('activity.target.created', 'activity', 'info', 'Target created', '', ?, "
        "'admins', 'legacy-1')",
        (utcnow(),),
    )
    conn.commit()
    sqlite_db.close_connection(conn)
    html = _admin_page(c, tokens, "activity")
    assert "Target created" in html and "None" not in html
    summary = c.get("/ui/notifications/summary", headers=_h(tokens, "boss")).json()
    assert all(i["body"] is not None and i["title"] for i in summary["items"])


def test_unread_text_is_visually_hidden_in_the_page_list(client):
    c, tokens = client
    html = _admin_page(c, tokens, "reports")  # admin has no reports; use a visible event instead
    conn = sqlite_db.connect(c.app.state.db_path)
    inbox.notify_user_created(
        conn, user_id=91, username="zed", actor=dict(sqlite_db.get_user_by_username(conn, "boss"))
    )
    sqlite_db.close_connection(conn)
    html = _admin_page(c, tokens, "activity")
    assert '<span class="visually-hidden">Unread: </span>' in html
    assert "ju-unread-dot" in html


def test_cli_admin_password_reset_notifies_admins_without_secret(db, tmp_path: Path, monkeypatch, capsys):
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader

    conn, path = db
    admin = _user(conn, "root", admin=True)
    _user(conn, "alice")
    conn.commit()
    cli_path = Path(__file__).resolve().parents[1] / "app" / "justup-cli"
    loader = SourceFileLoader("justup_cli_under_test", str(cli_path))
    cli = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(cli)
    monkeypatch.setattr(cli, "DB_PATH", path)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)

    assert cli.reset_password("alice") == 0  # regular user: no security event
    assert cli.reset_password("admin") == 0
    new_password = next(ln.split(": ", 1)[1] for ln in capsys.readouterr().out.splitlines() if "New password" in ln)

    items, total = inbox.list_notifications(conn, admin)
    assert total == 1
    assert items[0]["event_type"] == "security.admin_password.reset"
    assert "root" in items[0]["body"] and "host CLI (justup-cli)" in items[0]["body"]
    assert new_password not in items[0]["body"] + items[0]["title"]


def _queue_row(path: Path, qid: int):
    conn = sqlite_db.connect(path)
    try:
        return conn.execute(
            "SELECT status, retry_count, payload FROM notification_queue WHERE id = ?", (qid,)
        ).fetchone()
    finally:
        sqlite_db.close_connection(conn)


def test_queued_email_is_sent_by_the_worker_and_carries_no_smtp_secret(db, spooler):
    _, path = db
    qid = spooler.enqueue(
        "alert",
        "email",
        1,
        "R",
        "rcpt@example.com",
        {"subject": "s", "text_body": "t", "html_body": "<p>h</p>"},
        immediate=True,
    )
    sent: list[tuple] = []

    def send_email(to, subject, text, html):
        sent.append((to, subject, text, html))
        return True

    assert spooler.process_queue_sync(send_email=send_email) == (1, 1)
    assert sent == [("rcpt@example.com", "s", "t", "<p>h</p>")]
    status, _, payload = _queue_row(path, qid)
    assert status == "sent"
    assert "password" not in payload.lower()


def test_email_smtp_failure_is_retried_and_signal_path_is_untouched(db, spooler):
    _, path = db
    mail_id = spooler.enqueue(
        "alert", "email", 1, "R", "rcpt@example.com", {"subject": "s", "text_body": "t"}, immediate=True
    )
    sig_id = spooler.enqueue(
        "alert", "signal", 2, "S", "+4915112345678", {"message": "m", "sender": "+4930000000"}, immediate=True
    )
    signal_calls: list[tuple] = []

    def send_signal(phone, message, sender):
        signal_calls.append((phone, message, sender))
        return True

    def send_email(*_args):
        raise OSError("smtp down")

    processed, succeeded = spooler.process_queue_sync(send_signal=send_signal, send_email=send_email)
    assert (processed, succeeded) == (2, 1)
    assert signal_calls == [("+4915112345678", "m", "+4930000000")]
    assert _queue_row(path, sig_id)[0] == "sent"
    status, retries, _ = _queue_row(path, mail_id)
    assert status == "pending" and retries == 1


# ─── SLA threshold in alert mails ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("actual", "sla_enabled"),
    [(99.89, True), (99.9, True), (99.91, True), (99.5, False)],
    ids=["below", "equal", "above", "sla-disabled"],
)
def test_alert_mail_uses_per_target_sla_threshold_in_both_render_paths(db, actual, sla_enabled):
    from app.services.notifications.dispatcher import AlertPayload, NotificationDispatcher
    from app.services.notifications.email_sender import EmailSender

    conn, path = db
    tid = _target(conn)
    conn.execute(
        "UPDATE targets SET sla_enabled = ?, sla_availability_pct = 99.9 WHERE id = ?", (int(sla_enabled), tid)
    )
    conn.execute(
        "INSERT INTO target_status (target_id, uptime_percent, updated_at) VALUES (?, ?, ?)", (tid, actual, utcnow())
    )
    conn.commit()
    alert = AlertPayload("a1", tid, "T1", "PING", "open", utcnow())

    sender = EmailSender(lambda: sqlite_db.connect(path))
    sla = sender._build_email_context(conn, alert)["sla"]  # noqa: SLF001 - both render paths are the contract
    _, html = NotificationDispatcher(conn)._render_email_content(alert)  # noqa: SLF001

    if sla_enabled:
        assert sla == {"threshold": "99.90%", "actual": f"{actual:.2f}%"}
        assert "99.90%" in html and f"{actual:.2f}%" in html
    else:
        assert sla == {"threshold": "N/A", "actual": "N/A"}


# ─── Certificate expiry ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("days_left", "expected_title"),
    [(8, None), (7, "Certificate warning"), (3, "Certificate warning"), (0, "Certificate warning")],
)
def test_certificate_expiry_threshold_creates_inbox_warning(db, days_left, expected_title):
    import asyncio
    from contextlib import contextmanager
    from unittest import mock

    from app.monitor.scheduler import MonitorScheduler, TargetRuntime

    conn, _ = db
    admin = _user(conn, "root", admin=True)
    tid = _target(conn)

    sched = MonitorScheduler.__new__(MonitorScheduler)
    sched._rt = {tid: TargetRuntime(next_due=utcnow())}  # noqa: SLF001

    @contextmanager
    def _conn():
        yield conn

    sched._db_conn = _conn  # noqa: SLF001
    sched._dispatch_alert = mock.AsyncMock()  # noqa: SLF001

    asyncio.run(sched._maybe_cert_alert(target_id=tid, target_name="T1", days_left=days_left, cert_host="h"))  # noqa: SLF001

    items, _ = inbox.list_notifications(conn, admin)
    if expected_title is None:
        assert items == []
        sched._dispatch_alert.assert_not_called()  # noqa: SLF001
        return
    assert [i["event_type"] for i in items] == ["monitoring.certificate.warning"]
    assert items[0]["title"].startswith(expected_title)
    assert items[0]["severity"] == "warning"
    sched._dispatch_alert.assert_awaited_once()  # noqa: SLF001
