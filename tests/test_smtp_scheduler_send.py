#!/usr/bin/env python3
#
# tests/test_smtp_scheduler_send.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""End-to-end SMTP send path of the scheduler: real DB settings -> spooler -> send_message.

Alert mail and test mail are queue rows handled by the same send function of the scheduler. Only
the smtplib client classes are replaced by recorders, so no socket opens and no mail leaves.
"""

# ruff: noqa: SLF001
from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import sqlite as sqlite_db
from app.monitor import scheduler as scheduler_module
from app.monitor.scheduler import MonitorScheduler
from app.services.notifications import smtp_transport
from app.services.notifications.spooler import ChannelType, NotificationSpooler, NotificationType
from app.utils import crypto, maintenance
from app.utils.constants import SettingKeys
from app.utils.crypto import encrypt_secret
from tests import _support  # noqa: F401 - imported for its sys.path side effect
from tests.test_smtp_transport import _FakeSMTP, _FakeSMTPSSL

_PASSWORD = "Zq7-dummy-secret-Xk9"  # noqa: S105 - dummy credential for a recording fake


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    crypto.reset_fernet_cache()
    yield
    crypto.reset_fernet_cache()


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch):
    _FakeSMTP.instances = []
    _FakeSMTPSSL.ssl_instances = []
    monkeypatch.setattr(smtp_transport.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtp_transport.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    return _FakeSMTP, _FakeSMTPSSL


@pytest.fixture
def scheduler(db_path: Path, tmp_path: Path) -> MonitorScheduler:
    return MonitorScheduler(get_conn=lambda: sqlite_db.connect(db_path), tsdb_dir=tmp_path / "tsdb")


def _configure(db_path: Path, **settings) -> None:
    connection = sqlite_db.connect(db_path)
    try:
        values = {"smtp_enabled": True, "smtp_host": "mail.example.com", "smtp_from": "alerts@example.com"}
        values.update(settings)
        for key, value in values.items():
            sqlite_db.set_setting(connection, SettingKeys(key), value)
    finally:
        sqlite_db.close_connection(connection)


def _send(scheduler: MonitorScheduler) -> bool:
    generation = scheduler_module.maintenance.current_generation()
    return scheduler._send_spooler_email(generation, "to@example.com", "subject", "text", "<p>html</p>", "key-1")


# --- spooler send function ---------------------------------------------------------------------


def test_default_without_stored_key_requires_starttls(scheduler, db_path, fakes):
    _configure(db_path)
    assert _send(scheduler) is True
    inst = fakes[0].instances[0]
    assert (inst.host, inst.port) == ("mail.example.com", 587)
    assert inst.started_tls is True
    assert inst.ops.index("starttls") < inst.ops.index("send")


def test_default_refuses_a_server_without_starttls_and_sends_nothing(scheduler, db_path, fakes, monkeypatch):
    _configure(db_path)
    monkeypatch.setattr(_FakeSMTP, "has_extn", lambda _self, _name: False)
    with pytest.raises(smtp_transport.SMTPEncryptionError):
        _send(scheduler)
    assert all(not inst.sent for inst in fakes[0].instances)


def test_explicit_false_without_credentials_allows_plaintext(scheduler, db_path, fakes):
    _configure(db_path, smtp_use_tls=False, smtp_port=25)
    assert _send(scheduler) is True
    inst = fakes[0].instances[0]
    assert inst.started_tls is False
    assert len(inst.sent) == 1


@pytest.mark.parametrize(
    "credentials",
    [
        {"smtp_user": "alerts", "smtp_password": "<encrypted>"},
        {"smtp_user": "alerts"},
        {"smtp_password": "<encrypted>"},
    ],
)
def test_explicit_false_with_full_or_partial_credentials_is_refused(scheduler, db_path, fakes, credentials):
    stored = {k: (encrypt_secret(_PASSWORD) if v == "<encrypted>" else v) for k, v in credentials.items()}
    _configure(db_path, smtp_use_tls=False, smtp_port=25, **stored)
    with pytest.raises(smtplib.SMTPException):
        _send(scheduler)
    assert all(not inst.sent and inst.logged_in is None for inst in fakes[0].instances)


def test_undecryptable_password_aborts_before_any_connection(scheduler, db_path, fakes, monkeypatch):
    stored = encrypt_secret(_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))  # key rotated away
    crypto.reset_fernet_cache()
    _configure(db_path, smtp_user="alerts", smtp_password=stored, smtp_use_tls=False, smtp_port=25)
    with pytest.raises(smtp_transport.SMTPConfigurationError) as exc:
        _send(scheduler)
    assert _PASSWORD not in str(exc.value)
    assert fakes[0].instances == []
    assert fakes[1].ssl_instances == []


def test_port_465_uses_implicit_tls_even_with_the_switch_off(scheduler, db_path, fakes):
    _configure(db_path, smtp_port=465, smtp_use_tls=False, smtp_user="alerts", smtp_password=encrypt_secret(_PASSWORD))
    assert _send(scheduler) is True
    assert len(fakes[1].ssl_instances) == 1
    inst = fakes[1].ssl_instances[0]
    assert inst.logged_in == ("alerts", _PASSWORD)
    assert "starttls" not in inst.ops


# --- through the real spooler queue (alert mail and test mail share this path) -----------------


def _enqueue(spooler: NotificationSpooler, kind: NotificationType) -> int:
    return spooler.enqueue(
        notification_type=kind,
        channel_type=ChannelType.EMAIL,
        recipient_id=0,
        recipient_name="Test (to@example.com)",
        recipient_address="to@example.com",
        payload={"subject": "subject", "text_body": "text", "html_body": "<p>html</p>"},
        error=None,
        immediate=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [NotificationType.TEST, NotificationType.ALERT])
async def test_queue_row_goes_through_the_scheduler_closure_to_the_transport(
    scheduler, db_path, fakes, monkeypatch, kind
):
    _configure(db_path)
    spooler = NotificationSpooler(lambda: sqlite_db.connect(db_path))
    monkeypatch.setattr(scheduler_module, "get_spooler", lambda _factory: spooler)
    _enqueue(spooler, kind)

    await scheduler._process_notification_spooler(datetime.now(UTC))

    inst = fakes[0].instances[0]
    assert inst.started_tls is True  # default: STARTTLS required
    assert len(inst.sent) == 1
    assert inst.sent[0]["To"] == "to@example.com"
    assert spooler.get_queue_stats_detailed()["by_status"].get("sent") == 1


@pytest.mark.asyncio
async def test_spooler_initialisation_runs_inside_a_maintenance_work_section(scheduler, db_path, monkeypatch):
    inflight_during_init = []
    spooler = NotificationSpooler(lambda: sqlite_db.connect(db_path))

    def fake_get_spooler(_factory):
        inflight_during_init.append(maintenance._INFLIGHT)
        return spooler

    monkeypatch.setattr(scheduler_module, "get_spooler", fake_get_spooler)

    await scheduler._process_notification_spooler(datetime.now(UTC), raise_errors=True)

    assert inflight_during_init and inflight_during_init[0] >= 1


@pytest.mark.asyncio
async def test_queue_row_is_not_sent_when_starttls_is_missing(scheduler, db_path, fakes, monkeypatch):
    _configure(db_path)
    monkeypatch.setattr(_FakeSMTP, "has_extn", lambda _self, _name: False)
    spooler = NotificationSpooler(lambda: sqlite_db.connect(db_path))
    monkeypatch.setattr(scheduler_module, "get_spooler", lambda _factory: spooler)
    _enqueue(spooler, NotificationType.TEST)

    await scheduler._process_notification_spooler(datetime.now(UTC))

    assert all(not inst.sent for inst in fakes[0].instances)
    assert spooler.get_queue_stats_detailed()["by_status"].get("sent", 0) == 0


@pytest.mark.asyncio
async def test_first_spooler_init_under_write_lock_does_not_block_event_loop(scheduler, db_path, monkeypatch):
    import asyncio

    from app.services.notifications import spooler as spooler_module

    monkeypatch.setattr(spooler_module, "_spooler", None)
    monkeypatch.setattr(spooler_module, "_spooler_last_failure", None)
    holder = sqlite_db.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    ticks = 0
    ticks_at_release = -1

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    async def release() -> None:
        nonlocal ticks_at_release
        await asyncio.sleep(0.5)
        ticks_at_release = ticks
        holder.rollback()

    tick_task = asyncio.create_task(ticker())
    release_task = asyncio.create_task(release())
    await asyncio.sleep(0)  # let both tasks start their timers before the call under test
    try:
        await scheduler._process_notification_spooler(datetime.now(UTC))
        await release_task
    finally:
        tick_task.cancel()
        sqlite_db.close_connection(holder)
    # With a blocked loop, release() cannot run until the busy timeout expired and no ticks happen.
    assert ticks_at_release >= 10
