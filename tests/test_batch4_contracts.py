#!/usr/bin/env python3
#
# tests/test_batch4_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG005, S105
"""Contract tests for notification dedup, settings cache, sslyze tri-state, log redaction and helpers."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from app.db import sqlite as sqlite_db
from app.services.notifications import dispatcher as dispatcher_mod
from app.services.notifications import signal_cli_backend as cli_mod
from app.services.notifications.spooler import NotificationSpooler, QueueStatus
from app.utils import metric_limits, settings_cache, whatweb_parser
from app.utils.phone import mask_phone
from tests import _support  # noqa: F401 - imported for its sys.path side effect

# ---------------------------------------------------------------------------
# B4-C01: alert_event is part of the alert duplicate key
# ---------------------------------------------------------------------------


@pytest.fixture
def spooler(tmp_path: Path):
    db_path = tmp_path / "spool.db"
    boot = sqlite_db.connect(db_path)
    sqlite_db.init_schema(boot)
    sqlite_db.close_connection(boot)
    return NotificationSpooler(lambda: sqlite_db.connect(db_path)), db_path


def _alert(sp, channel="email", recipient=1, event=None, alert_id="a-1"):
    payload = {"alert_id": alert_id, "message": "m", "subject": "s"}
    if event is not None:
        payload["alert_event"] = event
    addr = "r@example.com" if channel == "email" else "+491701234567"
    return sp.enqueue("alert", channel, recipient, "R", addr, payload, immediate=True)


def test_open_twice_is_one_notification(spooler):
    sp, _ = spooler
    assert _alert(sp, event="open") == _alert(sp, event="open")


def test_resolved_after_open_is_second_notification_per_channel_and_recipient(spooler):
    sp, _ = spooler
    for channel in ("email", "signal"):
        for rid in (1, 2):
            opened = _alert(sp, channel, rid, "open")
            resolved = _alert(sp, channel, rid, "resolved")
            assert opened != resolved


def test_resolved_twice_is_one_recovery_notification(spooler):
    sp, _ = spooler
    _alert(sp, event="open")
    assert _alert(sp, event="resolved") == _alert(sp, event="resolved")


def test_sent_or_failed_open_does_not_block_resolved(spooler):
    sp, db_path = spooler
    for status in (QueueStatus.SENT.value, QueueStatus.FAILED.value):
        alert_id = f"id-{status}"
        qid = _alert(sp, event="open", alert_id=alert_id)
        conn = sqlite_db.connect(db_path)
        conn.execute("UPDATE notification_queue SET status = ? WHERE id = ?", (status, qid))
        conn.commit()
        sqlite_db.close_connection(conn)
        assert _alert(sp, event="resolved", alert_id=alert_id) != qid


def test_legacy_payload_without_event_counts_as_open(spooler):
    sp, _ = spooler
    legacy = _alert(sp, event=None)
    assert _alert(sp, event="open") == legacy
    assert _alert(sp, event=None) == legacy
    assert _alert(sp, event="resolved") != legacy


@pytest.mark.parametrize("status", ["open", "resolved"])
def test_dispatcher_sets_alert_event_in_both_payloads(status):
    captured = []
    fake_spooler = mock.Mock()
    fake_spooler.enqueue.side_effect = lambda **kw: captured.append(kw["payload"]) or len(captured)
    row = {"id": 1, "name": "R", "phone": "+491701234567", "email": "r@example.com", "is_enabled": 1}
    disp = dispatcher_mod.NotificationDispatcher(mock.Mock(), lambda: None)
    alert = dispatcher_mod.AlertPayload(
        alert_id="x", target_id=1, target_name="t", failed_service="http", status=status,
        started_at=datetime.now(UTC),
    )  # fmt: skip
    with (
        mock.patch.object(dispatcher_mod.sqlite_db, "get_target_recipients", return_value=[row]),
        mock.patch.object(dispatcher_mod.sqlite_db, "get_setting", side_effect=lambda c, k, d=None: True),
        mock.patch.object(disp, "_render_signal_message", return_value="msg"),
        mock.patch.object(disp, "_render_email_content", return_value=("s", "<p>h</p>")),
        mock.patch("app.services.notifications.spooler.get_spooler", return_value=fake_spooler),
    ):
        disp.dispatch_alert(alert)
    assert len(captured) == 2
    assert {p["alert_event"] for p in captured} == {status}
    assert all(p["alert_id"] == "x" for p in captured)


# ---------------------------------------------------------------------------
# B4-C06: connection is closed when setup fails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fail_on", ["PRAGMA", "BEGIN"])
def test_db_conn_closes_exactly_once_when_setup_fails(spooler, fail_on):
    sp, _ = spooler
    conn = mock.Mock()
    conn.execute.side_effect = lambda sql, *a: (
        (_ for _ in ()).throw(sqlite3.OperationalError("boom")) if fail_on in sql else None
    )
    sp._db_conn_factory = lambda: conn
    with (
        mock.patch.object(sqlite_db, "close_connection") as close,
        pytest.raises(sqlite3.OperationalError),
        sp._db_conn(immediate=True),
    ):
        pytest.fail("body must not run")
    close.assert_called_once_with(conn)


# ---------------------------------------------------------------------------
# B4-C04 / C10: settings cache invalidation after commit; in-memory isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def two_conns(tmp_path: Path):
    path = tmp_path / "s.db"
    a = sqlite_db.connect(path)
    sqlite_db.init_schema(a)
    sqlite_db.set_setting(a, "k1", "old1")
    sqlite_db.set_setting(a, "k2", "old2")
    b = sqlite_db.connect(path)
    settings_cache.invalidate_settings_cache()
    yield a, b
    sqlite_db.close_connection(a)
    sqlite_db.close_connection(b)
    settings_cache.invalidate_settings_cache()


def test_set_setting_without_autocommit_does_not_invalidate(two_conns):
    a, b = two_conns
    assert settings_cache.get_setting_cached(b, "k1") == "old1"
    sqlite_db.set_setting(a, "k1", "new1", auto_commit=False)
    with mock.patch.object(settings_cache, "invalidate_settings_cache") as inv:
        sqlite_db.set_setting(a, "k2", "new2", auto_commit=False)
    inv.assert_not_called()
    a.rollback()


def test_interleaved_reader_never_caches_old_value_after_commit(two_conns):
    a, b = two_conns
    sqlite_db.set_setting(a, "k1", "new1", auto_commit=False)
    sqlite_db.set_setting(a, "k2", "new2", auto_commit=False)
    # Reader sees the committed (old) values while the writer's transaction is open
    assert settings_cache.get_setting_cached(b, "k1") == "old1"
    a.commit()
    for k in ("k1", "k2"):
        settings_cache.invalidate_settings_cache(k)
    assert settings_cache.get_setting_cached(b, "k1") == "new1"
    assert settings_cache.get_setting_cached(b, "k2") == "new2"


def test_update_settings_endpoint_invalidates_after_commit_and_on_rollback(two_conns):
    from app.api import settings as settings_api

    a, _ = two_conns
    order = []
    real_commit = a.commit
    with (
        mock.patch.object(settings_api, "invalidate_settings_cache", side_effect=lambda k: order.append(("inv", k))),
        mock.patch.object(settings_api, "_load", return_value={}),
        mock.patch.object(settings_api, "require_admin", create=True),
    ):
        a_proxy = mock.Mock(wraps=a)
        # A bare Mock reports a truthy in_transaction (a foreign transaction); mirror the real handle.
        type(a_proxy).in_transaction = mock.PropertyMock(side_effect=lambda: a.in_transaction)
        a_proxy.commit.side_effect = lambda: (order.append(("commit",)), real_commit())[1]
        payload = settings_api.SettingsUpdate(pdf_page_size="letter")
        settings_api.update_settings(payload, conn=a_proxy, _admin=None)
    assert order[0] == ("commit",)
    assert ("inv", "pdf_page_size") in order[1:]


def test_in_memory_databases_do_not_share_cache():
    c1 = sqlite3.connect(":memory:")
    c2 = sqlite3.connect(":memory:")
    c1.row_factory = c2.row_factory = sqlite3.Row
    try:
        for c, v in ((c1, "one"), (c2, "two")):
            c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            c.execute("INSERT INTO settings VALUES ('same', ?, 'x')", (f'"{v}"',))
            c.commit()
        settings_cache.invalidate_settings_cache()
        assert settings_cache.get_setting_cached(c1, "same") == "one"
        assert settings_cache.get_setting_cached(c2, "same") == "two"
    finally:
        c1.close()
        c2.close()


# ---------------------------------------------------------------------------
# B4-C07 / C02: URL redaction in logs; WhatWeb HTTPS hostnames stay fail-closed
# ---------------------------------------------------------------------------

SECRET_URL = "https://user:hunter2@example.org:8443/p?token=SECRETTOKEN#frag"


def _no_secrets(text: str) -> None:
    assert "hunter2" not in text
    assert "SECRETTOKEN" not in text
    assert "user:" not in text


def test_redact_url_keeps_only_scheme_host_port():
    from app.utils.ssrf import redact_url

    assert redact_url(SECRET_URL) == "https://example.org:8443"
    assert redact_url("https://h.example/bot123:TOKEN/send") == "https://h.example"
    assert redact_url("http://[2001:db8::1]:81/x?y=1") == "http://[2001:db8::1]:81"
    assert redact_url("http://[2001:db8::1]:81/x?y=1", keep_path=True) == "http://[2001:db8::1]:81/x"
    assert redact_url("http://[bad/?token=SECRETTOKEN") == "<unparseable-url>"


def test_whatweb_logs_do_not_leak_url_secrets(caplog):
    caplog.set_level(logging.DEBUG)
    url = "http://user:hunter2@example.org/p?token=SECRETTOKEN"
    done = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    with (
        mock.patch.object(whatweb_parser, "resolve_validated", return_value=["93.184.216.34"]),
        mock.patch.object(whatweb_parser, "_find_whatweb_binary", return_value="/usr/bin/whatweb"),
        mock.patch.object(whatweb_parser, "_run_capped", return_value=done),
    ):
        whatweb_parser.run_whatweb(url, aggression=1, timeout=5)
    with (
        mock.patch.object(whatweb_parser, "resolve_validated", return_value=["93.184.216.34"]),
        mock.patch.object(whatweb_parser, "_find_whatweb_binary", return_value="/usr/bin/whatweb"),
        mock.patch.object(whatweb_parser, "_run_capped", side_effect=subprocess.TimeoutExpired("w", 5)),
    ):
        whatweb_parser.run_whatweb(url, aggression=1, timeout=5)
    assert caplog.records
    _no_secrets(caplog.text)


def test_run_capped_kills_a_process_that_floods_stdout_or_stderr():
    import sys
    import time

    for stream in ("stdout", "stderr"):
        code = f"import sys,time\nwhile True: sys.{stream}.write('x'*65536); sys.{stream}.flush()"
        started = time.monotonic()
        with (
            mock.patch.object(whatweb_parser, "MAX_OUTPUT_BYTES", 256 * 1024),
            pytest.raises(whatweb_parser.WhatWebOutputLimitError),
        ):
            whatweb_parser._run_capped([sys.executable, "-c", code], 30)
        assert time.monotonic() - started < 15
    ok = whatweb_parser._run_capped([sys.executable, "-c", "print('{}')"], 30)
    assert ok.returncode == 0 and ok.stdout.strip() == "{}"


def test_whatweb_error_messages_do_not_leak_url_secrets():
    with (
        mock.patch.object(whatweb_parser, "resolve_validated", return_value=["93.184.216.34"]),
        pytest.raises(ValueError, match="SNI") as exc,
    ):
        whatweb_parser.run_whatweb(SECRET_URL, aggression=1, timeout=5)
    _no_secrets(str(exc.value))
    with pytest.raises(ValueError, match="SSRF") as exc:
        whatweb_parser.run_whatweb("http://u:hunter2@127.0.0.1/?token=SECRETTOKEN", aggression=1, timeout=5)
    _no_secrets(str(exc.value))
    with pytest.raises(ValueError, match="scheme") as exc:
        whatweb_parser.run_whatweb("ftp://u:hunter2@h/?token=SECRETTOKEN", aggression=1, timeout=5)
    _no_secrets(str(exc.value))


# ---------------------------------------------------------------------------
# B4-C05: link process output is drained
# ---------------------------------------------------------------------------


class _FakeProc:
    """Pipe-backed fake: wait() blocks until the child's output has been consumed."""

    def __init__(self, stdout_text, stderr_text, returncode=0, hang=False):
        import io

        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.hang = hang
        self.terminated = False
        self.killed = False
        self.pid = 1

    def wait(self, timeout=None):
        if self.hang and not self.terminated:
            raise subprocess.TimeoutExpired("signal-cli", timeout)
        return self.returncode

    def poll(self):
        return None if self.hang and not self.terminated else self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _backend_with(proc):
    be = object.__new__(cli_mod.SignalCliBackend)
    be._state_lock = threading.RLock()
    be._link_process = proc
    be._link_session_id = "sid"
    be._link_drain_threads = cli_mod._start_pipe_drain(proc)
    return be


def test_wait_for_link_drains_large_output_and_closes_pipes():
    proc = _FakeProc("x" * 1_000_000 + "\n", "e" * 1_000_000 + "\n")
    be = _backend_with(proc)
    assert be.wait_for_link("sid", timeout=5) is True
    assert proc.stdout.closed
    assert proc.stderr.closed
    assert be._link_process is None
    assert all(not t.is_alive() for t in be._link_drain_threads)


def test_wait_for_link_failure_exit_code_and_timeout_cleanup():
    proc = _FakeProc("", "err\n", returncode=1)
    assert _backend_with(proc).wait_for_link("sid", timeout=5) is False
    hang = _FakeProc("", "", hang=True)
    be = _backend_with(hang)
    t0 = time.monotonic()
    assert be.wait_for_link("sid", timeout=0) is False
    assert time.monotonic() - t0 < 10
    assert hang.terminated
    assert hang.stdout.closed
    assert be._link_process is None


def test_wait_for_link_session_mismatch_and_cancel_race():
    proc = _FakeProc("", "")
    be = _backend_with(proc)
    assert be.wait_for_link("other", timeout=1) is False
    be._link_process = None  # cancel_link() won the race
    assert be.wait_for_link("sid", timeout=1) is False


# ---------------------------------------------------------------------------
# B4-C09: per-port metric families
# ---------------------------------------------------------------------------

_SINCE = datetime(2026, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2026, 1, 2, tzinfo=UTC)


def _limit(metric, profile="ui"):
    return metric_limits.choose_points_limit(metric=metric, since=_SINCE, until=_UNTIL, profile=profile)


def test_per_port_metrics_match_base_metric_limits():
    assert _limit("tcp_ms_443") == _limit("tcp_ms") == int(600 * 1.15)
    assert _limit("tcp_up_443") == _limit("tcp_up") == int(600 * 0.8)
    assert _limit("tcp_up_65535") == _limit("tcp_up")


@pytest.mark.parametrize("metric", ["tcp_up_0", "tcp_up_65536", "tcp_ms_0", "tcp_ms_65536", "tcp_ms_x"])
def test_invalid_port_metrics_are_ordinary(metric):
    assert _limit(metric) == 600


def test_ordinary_and_status_metrics_unchanged():
    assert _limit("ping_ms") == int(600 * 1.15)
    assert _limit("http_up") == int(600 * 0.8)
    assert _limit("cpu") == 600
    assert _limit("http_status") == 3000


# ---------------------------------------------------------------------------
# D01 / D05: shared helpers keep their output
# ---------------------------------------------------------------------------


def test_signal_cli_mask_matches_shared_mask_phone():
    for number in ("+491701234567", "12345", "", "+4915"):
        assert cli_mod._mask_phone(number) == mask_phone(number)
    assert mask_phone("+491701234567") == "+49****67"
    assert mask_phone(None) == "***"


def test_spooler_now_is_timezone_aware_utc():
    from app.services.notifications import spooler as spooler_mod

    now = spooler_mod._utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0
