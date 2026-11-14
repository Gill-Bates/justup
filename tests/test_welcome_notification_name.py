#!/usr/bin/env python3
#
# tests/test_welcome_notification_name.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The welcome message names the target as stored when the task runs, not as it was when it was queued."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.api import targets as targets_api
from app.api.targets import _build_welcome_email_html
from app.db import sqlite as sqlite_db
from app.services.notifications import signal_backend
from app.utils.constants import SettingKeys
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.fixture
def spooler(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    fake.is_channel_idle.return_value = True
    monkeypatch.setattr("app.services.notifications.spooler.get_spooler", lambda _factory: fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_notification_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(targets_api, "_notification_tasks", {})
    # The task builds the Signal backend, which creates its data dir; keep that off the real /app.
    monkeypatch.setenv("SIGNAL_DATA_DIR", str(tmp_path / "signal"))
    monkeypatch.setattr(signal_backend, "_backend_instance", None)


def _prepare(tmp_path: Path, *, stored_name: str) -> tuple[Path, int, int]:
    db_path = tmp_path / "justup.db"
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.init_schema(conn)
        now = utcnow()
        target_id = sqlite_db.create_target(
            conn,
            {
                "name": stored_name,
                "host": "renamed.example.org",
                "interval_seconds": 60,
                "retention_days": 30,
                "enable_ping": 1,
                "created_at": now,
                "updated_at": now,
            },
        )
        recipient_id = sqlite_db.create_recipient(conn, name="Rita", email="rita@example.org")
        sqlite_db.set_target_recipients(conn, target_id, [recipient_id])
        sqlite_db.set_setting(conn, SettingKeys.SMTP_ENABLED, True)
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)
    return db_path, target_id, recipient_id


def _run(db_path: Path, target_id: int, recipient_id: int, queued_name: str) -> None:
    targets_api._notification_tasks.setdefault(target_id, {})["task"] = {  # noqa: SLF001 - task registry under test
        "status": "running",
        "success": 0,
        "queued": 0,
        "skipped": 0,
        "failed": 0,
        "updated_at": 0.0,
    }
    targets_api._notify_recipients_background_in_section(  # noqa: SLF001 - the background task under test
        db_path, target_id, "task", queued_name, {recipient_id}
    )


def test_welcome_email_uses_the_name_stored_when_the_task_runs(tmp_path: Path, spooler: MagicMock) -> None:
    db_path, target_id, recipient_id = _prepare(tmp_path, stored_name="New Name")
    _run(db_path, target_id, recipient_id, queued_name="Old Name")  # renamed between queueing and execution
    spooler.enqueue.assert_called_once()
    payload = spooler.enqueue.call_args.kwargs["payload"]
    assert payload["target_name"] == "New Name"
    for body in (payload["text_body"], payload["html_body"]):
        assert "New Name" in body
        assert "Old Name" not in body


def test_welcome_email_keeps_the_name_when_it_did_not_change(tmp_path: Path, spooler: MagicMock) -> None:
    db_path, target_id, recipient_id = _prepare(tmp_path, stored_name="Same")
    _run(db_path, target_id, recipient_id, queued_name="Same")
    payload = spooler.enqueue.call_args.kwargs["payload"]
    assert "'Same'" in payload["text_body"]


def test_welcome_task_counts_queued_and_skipped_separately(tmp_path: Path, spooler: MagicMock) -> None:  # noqa: ARG001
    db_path, target_id, recipient_id = _prepare(tmp_path, stored_name="T")
    _run(db_path, target_id, recipient_id, queued_name="T")
    status = targets_api._notification_tasks[target_id]["task"]  # noqa: SLF001
    assert (status["success"], status["queued"], status["skipped"]) == (1, 1, 0)

    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.set_setting(conn, SettingKeys.SMTP_ENABLED, False)  # no usable channel left
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)
    _run(db_path, target_id, recipient_id, queued_name="T")
    status = targets_api._notification_tasks[target_id]["task"]  # noqa: SLF001
    assert (status["success"], status["queued"], status["skipped"]) == (0, 0, 1)
    assert status["status"] == "completed"


def test_welcome_task_skips_recipient_removed_meanwhile(tmp_path: Path, spooler: MagicMock) -> None:
    db_path, target_id, recipient_id = _prepare(tmp_path, stored_name="T")
    conn = sqlite_db.connect(db_path)
    try:
        sqlite_db.set_target_recipients(conn, target_id, [])
        conn.commit()
    finally:
        sqlite_db.close_connection(conn)
    _run(db_path, target_id, recipient_id, queued_name="T")
    status = targets_api._notification_tasks[target_id]["task"]  # noqa: SLF001
    assert (status["success"], status["skipped"]) == (0, 1)
    spooler.enqueue.assert_not_called()


def test_welcome_email_links_http_system_url() -> None:
    html_body = _build_welcome_email_html(
        "Ann", "T", "msg", system_url="https://up.example/?a=1&amp;b=2", github_url="https://g.example"
    )
    assert 'href="https://up.example/?a=1&amp;b=2"' in html_body


@pytest.mark.parametrize("url", ["#", "", "javascript:alert(1)"])
def test_welcome_email_omits_link_for_non_http_system_url(url) -> None:
    html_body = _build_welcome_email_html("Ann", "T", "msg", system_url=url, github_url="https://g.example")
    assert "Open justUp!" not in html_body


def test_welcome_text_body_links_http_and_https_system_urls(tmp_path: Path, spooler: MagicMock, monkeypatch) -> None:
    for base in ("http://up.example", "https://up.example"):
        spooler.enqueue.reset_mock()
        monkeypatch.setattr(targets_api, "get_public_base_url", lambda _conn, base=base: base)
        (tmp_path / base[:5]).mkdir()
        db_path, target_id, recipient_id = _prepare(tmp_path / base[:5], stored_name="N")
        _run(db_path, target_id, recipient_id, queued_name="N")
        payload = spooler.enqueue.call_args.kwargs["payload"]
        assert f"Open justUp!: {base}" in payload["text_body"]
        assert f'href="{base}' in payload["html_body"]
