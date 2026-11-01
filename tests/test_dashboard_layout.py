#!/usr/bin/env python3
#
# tests/test_dashboard_layout.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-user dashboard layout: schema, storage, API contract (auth, CSRF, isolation, validation)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.dashboard_layout import DASHBOARD_WIDGETS
from app.db import dashboard_layouts as layouts
from app.db import sqlite as sqlite_db
from app.models.users import hash_token, new_token
from app.utils import crypto
from tests import _support  # noqa: F401 - imported for its sys.path side effect

ROOT = Path(__file__).resolve().parent.parent
URL = "/me/dashboard-layout"
_STATUS = {"id": "status-online", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True}
_STATUS_WIDGETS = [
    _STATUS,
    {"id": "status-offline", "x": 3, "y": 0, "w": 3, "h": 2, "visible": True},
    {"id": "status-disabled", "x": 6, "y": 0, "w": 3, "h": 2, "visible": True},
    {"id": "status-total", "x": 9, "y": 0, "w": 3, "h": 2, "visible": True},
]
_QUALITY = {"id": "quality", "x": 0, "y": 2, "w": 3, "h": 4, "visible": False}
_DOWNTIME = {"id": "downtime", "x": 3, "y": 2, "w": 6, "h": 4, "visible": True}
_VALID = {"version": 1, "widgets": [*_STATUS_WIDGETS, _QUALITY, _DOWNTIME]}


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)


@pytest.fixture
def conn(tmp_path: Path):
    connection = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(connection)
    yield connection
    sqlite_db.close_connection(connection)


def _user(conn: sqlite3.Connection, name: str) -> tuple[int, str]:
    user_id = sqlite_db.create_user(conn, username=name, password_hash="stub-hash", is_admin=False)  # noqa: S106
    token, expires_at, max_expires_at = new_token()
    sqlite_db.issue_token(
        conn, user_id=user_id, token_hash=hash_token(token), expires_at=expires_at, max_expires_at=max_expires_at
    )
    return user_id, token


class _Api:
    """A TestClient session for one bearer token that echoes the rotating CSRF cookie."""

    def __init__(self, app, token: str | None) -> None:
        self.client = TestClient(app)
        self.client.get("/ui/login")  # sets the csrf_token cookie
        self.token = token

    def call(self, method: str, body=None, *, csrf: bool = True):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if csrf:
            headers["X-CSRF-Token"] = self.client.cookies.get("csrf_token")
        return self.client.request(method, URL, json=body, headers=headers)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-dashboard-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        from app.main import create_app

        app = create_app()
        connection = sqlite_db.connect(app.state.db_path)
        try:
            sqlite_db.init_schema(connection)
            alice_id, alice_token = _user(connection, "alice")
            bob_id, bob_token = _user(connection, "bob")
        finally:
            sqlite_db.close_connection(connection)
        yield {
            "app": app,
            "alice": _Api(app, alice_token),
            "bob": _Api(app, bob_token),
            "anon": _Api(app, None),
            "ids": (alice_id, bob_id),
        }


def test_schema_constraints_reject_out_of_range_rows(conn):
    user_id, _ = _user(conn, "u")
    insert = (
        "INSERT INTO dashboard_layouts (user_id, widget_id, position, x, y, w, h) VALUES (?, 'quality', 0, ?, ?, ?, ?)"
    )
    for bad in [(-1, 0, 1, 1), (10, 0, 3, 1), (0, 1000, 1, 1), (0, 0, 0, 1), (0, 0, 1, 101)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (user_id, *bad))
    conn.rollback()


def test_storage_roundtrip_replace_delete_and_cascade(conn):
    user_id, _ = _user(conn, "u")
    assert layouts.get_dashboard_layout(conn, user_id) is None
    layouts.set_dashboard_layout(conn, user_id, _VALID)
    assert layouts.get_dashboard_layout(conn, user_id) == _VALID
    layouts.set_dashboard_layout(conn, user_id, {"version": 1, "widgets": [_STATUS]})
    assert layouts.get_dashboard_layout(conn, user_id) == {"version": 1, "widgets": [_STATUS]}
    with pytest.raises(ValueError):
        layouts.set_dashboard_layout(conn, user_id, {"version": 1, "widgets": [_STATUS, _STATUS]})
    assert layouts.get_dashboard_layout(conn, user_id)["widgets"] == [_STATUS]
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    assert layouts.get_dashboard_layout(conn, user_id) is None


def test_api_requires_authentication(env):
    anon = env["anon"]
    assert anon.call("GET").status_code == 401
    assert anon.call("PUT", _VALID).status_code == 401
    assert anon.call("DELETE").status_code == 401


def test_mutations_require_csrf_token(env):
    alice = env["alice"]
    assert alice.call("PUT", _VALID, csrf=False).status_code == 403
    assert alice.call("DELETE", csrf=False).status_code == 403
    assert alice.call("GET").json() == {"layout": None}


def test_save_load_reset_cycle(env):
    alice = env["alice"]
    assert alice.call("GET").json() == {"layout": None}
    put = alice.call("PUT", _VALID)
    assert put.status_code == 200
    assert alice.call("GET").json() == {"layout": _VALID}
    assert alice.call("DELETE").json() == {"layout": None}
    assert alice.call("GET").json() == {"layout": None}
    assert alice.call("DELETE").status_code == 200  # idempotent


def test_layouts_are_isolated_per_user(env):
    alice, bob = env["alice"], env["bob"]
    assert alice.call("PUT", _VALID).status_code == 200
    assert bob.call("GET").json() == {"layout": None}
    other = {"version": 1, "widgets": [{**_STATUS, "visible": False}]}
    assert bob.call("PUT", other).status_code == 200
    assert alice.call("GET").json() == {"layout": _VALID}
    assert bob.call("DELETE").status_code == 200
    assert alice.call("GET").json() == {"layout": _VALID}


@pytest.mark.parametrize(
    "layout",
    [
        {"version": 1, "widgets": [{**_STATUS, "id": "nope"}]},
        {"version": 1, "widgets": [_STATUS, _STATUS]},
        {"version": 1, "widgets": [{**_STATUS, "x": 10}]},  # x + w > 12
        {"version": 1, "widgets": [{**_STATUS, "y": 999, "h": 2}]},  # y + h > 1000
        {"version": 1, "widgets": [{**_STATUS, "x": -1}]},
        {"version": 1, "widgets": [{**_STATUS, "w": 0}]},
        {"version": 1, "widgets": [{**_STATUS, "h": 101}]},
        {"version": 1, "widgets": [{**_STATUS, "w": "12"}]},
        {"version": 1, "widgets": [{**_STATUS, "html": "<b>x</b>"}]},
        {"version": 1, "widgets": [_STATUS] * 4},
        {"version": 2, "widgets": []},
        {"version": 1, "widgets": [_STATUS], "extra": 1},
        {"widgets": []},
        [],
    ],
)
def test_invalid_layouts_are_rejected_and_nothing_is_stored(env, layout):
    alice = env["alice"]
    assert alice.call("PUT", _VALID).status_code == 200
    assert alice.call("PUT", layout).status_code == 422
    assert alice.call("GET").json() == {"layout": _VALID}


def test_widget_allowlist_matches_client_registry_and_template():
    registry = (ROOT / "app/static/js/dashboard-widgets.js").read_text(encoding="utf-8")
    template = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    status_fragment = (ROOT / "app/templates/fragments/status_summary.html").read_text(encoding="utf-8")
    assert set(re.findall(r"def\(['\"]([a-z-]+)['\"]", registry)) == DASHBOARD_WIDGETS
    template_widget_ids = set(re.findall(r'data-widget-id="([a-z-]+)"', template))
    assert template_widget_ids == DASHBOARD_WIDGETS
    assert sum(widget_id.startswith("status-") for widget_id in template_widget_ids) == 4
    hero_ids = {f"dashboard-{widget_id}" for widget_id in template_widget_ids if widget_id.startswith("status-")}
    assert set(re.findall(r'id="(dashboard-status-[a-z]+)"', template)) == hero_ids
    assert set(re.findall(r'id="(dashboard-status-[a-z]+)"', status_fragment)) == hero_ids
    assert status_fragment.count('hx-swap-oob="outerHTML"') == 4
    assert len(re.findall(r'data-widget-id="', template)) == len(DASHBOARD_WIDGETS)
