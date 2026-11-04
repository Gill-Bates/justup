#!/usr/bin/env python3
#
# tests/test_public_url.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Public hostname: validation, the settings API, and the links it puts into alert mails and Signal texts."""

from __future__ import annotations

import html as html_lib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.api.auth import require_admin
from app.db import sqlite as sqlite_db
from app.services.notifications.dispatcher import AlertPayload, NotificationDispatcher
from app.services.notifications.email_template import (
    DEFAULT_HTML_TEMPLATE,
    build_alert_context,
    build_email_message,
    html_to_text,
    render_template,
)
from app.utils.constants import SettingKeys
from app.utils.deps import get_conn
from app.utils.public_url import get_public_base_url, normalize_public_hostname, target_url
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect

# ── Normalisation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "https://x.example.com",
        "http://x.example.com",
        "x.example.com:443",
        "[::1]:443",
        "x.example.com/path",
        "x.example.com/",
        "[[::1]]",
        "user@x.example.com",
        "x example.com",
        "x.example.com\n",
        "x.exa\tmple.com",
        "x.example.com\x00",
        "x_y.example.com",
        "-x.example.com",
        "x-.example.com",
        "x..example.com",
        ".x.example.com",
        "x.example.com.",
        "a" * 64 + ".example.com",
        ("a" * 60 + ".") * 5 + "com",
        "exämple.com",
        "x.example.com?q=1",
        "x.example.com#frag",
        "x.example.com<script>",
    ],
)
def test_invalid_public_hostname_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_public_hostname(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("justup.example.com", "justup.example.com"),
        ("  JustUp.Example.com  ", "justup.example.com"),
        ("localhost", "localhost"),
        ("192.0.2.10", "192.0.2.10"),
        ("::1", "::1"),
        ("[::1]", "::1"),
        ("2001:DB8::1", "2001:db8::1"),
    ],
)
def test_valid_public_hostname_is_normalised(value, expected):
    assert normalize_public_hostname(value) == expected


def test_target_url_links_to_target_detail_and_falls_back_to_dashboard():
    assert target_url("https://j.example.com", 42) == "https://j.example.com/ui/targets/42"
    assert target_url("https://j.example.com", None) == "https://j.example.com/"
    assert target_url("", 42) == "#"


# ── Settings API ─────────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(c)
    yield c
    sqlite_db.close_connection(c)


@pytest.fixture
def client(conn):
    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "is_admin": True}
    return TestClient(app)


@pytest.mark.parametrize("bad", ["https://x.example.com", "x.example.com:8443", "x.example.com/a", "a b", "u@x.com"])
def test_patch_rejects_invalid_public_hostname_with_422_and_keeps_old_value(client, bad):
    assert client.patch("/settings", json={"public_hostname": "ok.example.com"}).status_code == 200
    r = client.patch("/settings", json={"public_hostname": bad})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert client.get("/settings").json()["public_hostname"] == "ok.example.com"


def test_patch_rejects_null_public_hostname(client):
    assert client.patch("/settings", json={"public_hostname": None}).status_code == 422


def test_public_hostname_defaults_to_empty_and_can_be_saved_and_cleared(client, conn):
    assert client.get("/settings").json()["public_hostname"] == ""
    r = client.patch("/settings", json={"public_hostname": "  [2001:db8::1] "})
    assert r.status_code == 200 and r.json()["public_hostname"] == "2001:db8::1"
    assert get_public_base_url(conn) == "https://[2001:db8::1]"
    r = client.patch("/settings", json={"public_hostname": ""})
    assert r.status_code == 200 and r.json()["public_hostname"] == ""
    assert get_public_base_url(conn) == ""


def test_base_url_is_derived_from_public_hostname_only(client, conn):
    client.patch("/settings", json={"public_hostname": "justup.example.com"})
    assert get_public_base_url(conn) == "https://justup.example.com"


def test_corrupt_stored_hostname_yields_no_base_url(conn):
    sqlite_db.set_setting(conn, SettingKeys.PUBLIC_HOSTNAME, 'evil.example.com/"><script>')
    assert get_public_base_url(conn) == ""


# ── Alert mails and Signal texts ─────────────────────────────────────────────


def _target(conn) -> int:
    cur = conn.execute(
        "INSERT INTO targets (name, interval_seconds, retention_days, enable_ping, enable_http_check, "
        "enable_cert_expiration, host, created_at, updated_at) "
        "VALUES ('T1', 60, 30, 1, 0, 0, 'host.example.com', ?, ?)",
        (utcnow(), utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _alert(tid: int, status: str) -> AlertPayload:
    started = utcnow()
    if status == "resolved":
        return AlertPayload("a1", tid, "T1", "TCP", status, started, started, 900)
    return AlertPayload("a1", tid, "T1", "TCP", status, started)


def _plain_part(html_body: str) -> str:
    msg = build_email_message(subject="s", html_body=html_body, from_addr="a@example.com", to_addr="b@example.com")
    return msg.get_body(preferencelist=("plain",)).get_content()


@pytest.mark.parametrize("status", ["open", "resolved"])
def test_mail_has_button_and_plaintext_link_only_with_public_hostname(conn, client, status):
    tid = _target(conn)
    dispatcher = NotificationDispatcher(conn)

    _, html_without = dispatcher._render_email_content(_alert(tid, status))  # noqa: SLF001
    assert "Open in justUp!" not in html_without
    assert "https://" not in _plain_part(html_without).replace("https://github.com", "")

    client.patch("/settings", json={"public_hostname": "justup.example.com"})
    link = f"https://justup.example.com/ui/targets/{tid}"
    _, html_with = dispatcher._render_email_content(_alert(tid, status))  # noqa: SLF001
    assert "Open in justUp!" in html_with
    assert f'href="{link}"' in html_with
    assert link in _plain_part(html_with)


def test_html_to_text_keeps_the_url_of_a_link_as_visible_text():
    text = html_to_text('<p><a href="https://j.example.com/ui/targets/1?a=1&amp;b=2">Open in justUp!</a></p>')
    assert "Open in justUp!" in text
    assert "https://j.example.com/ui/targets/1?a=1&b=2" in text


def test_html_to_text_ignores_non_http_hrefs():
    text = html_to_text('<a href="javascript:alert(1)">x</a><a href="#">y</a><a href="mailto:a@b.c">z</a>')
    assert "javascript" not in text and "#" not in text


def test_hostname_is_html_escaped_in_the_cta_link():
    ctx = build_alert_context(
        {
            "status": "OPEN",
            "started_at": utcnow().isoformat(),
            "reason": "x",
            "system_url": 'https://a.example.com/"><b>',
        },
        {"name": "n", "host": "h", "group": "Default", "check_type": "PING"},
    )
    html = render_template(DEFAULT_HTML_TEMPLATE, ctx)
    assert '"><b>' not in html
    assert html_lib.escape('"><b>', quote=True) in html


def test_saved_custom_template_without_cta_block_is_not_modified(conn, client):
    from app.services.notifications.email_template import EMAIL_TEMPLATE_HTML_KEY, load_email_templates

    custom = "<p>{{target.name}}</p>"
    sqlite_db.set_setting(conn, EMAIL_TEMPLATE_HTML_KEY, custom)
    tid = _target(conn)
    client.patch("/settings", json={"public_hostname": "justup.example.com"})
    assert load_email_templates(conn)["html"] == custom
    _, html = NotificationDispatcher(conn)._render_email_content(_alert(tid, "open"))  # noqa: SLF001
    assert html == "<p>T1</p>"
    assert sqlite_db.get_setting(conn, EMAIL_TEMPLATE_HTML_KEY, None) == custom


@pytest.mark.parametrize("status", ["open", "resolved"])
def test_signal_text_contains_target_link_only_with_public_hostname(conn, client, status):
    tid = _target(conn)
    dispatcher = NotificationDispatcher(conn)
    without = dispatcher._render_signal_message(_alert(tid, status))  # noqa: SLF001
    assert "http" not in without and "#" not in without
    assert "{{" not in without

    client.patch("/settings", json={"public_hostname": "justup.example.com"})
    with_url = dispatcher._render_signal_message(_alert(tid, status))  # noqa: SLF001
    assert f"https://justup.example.com/ui/targets/{tid}" in with_url
