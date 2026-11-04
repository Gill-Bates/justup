#!/usr/bin/env python3
#
# tests/test_targets_search.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Targets page search: field markup and the unpaginated fragment the client-side filter needs."""

from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import sqlite as sqlite_db
from app.models.users import hash_password, hash_token, new_token
from app.utils import crypto
from app.utils.constants import UI_PAGE_SIZE_TARGETS
from app.utils.time import utcnow
from tests import _support  # noqa: F401 - imported for its sys.path side effect

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
EXTRA = 5


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with TemporaryDirectory(prefix="justup-search-") as temp_dir:
        monkeypatch.setenv("JUSTUP_DATA_DIR", temp_dir)
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        monkeypatch.delenv("SECRET_KEYS", raising=False)
        monkeypatch.setattr(crypto, "_primary_key", None)
        from app.main import create_app

        app = create_app()
        conn = sqlite_db.connect(app.state.db_path)
        sqlite_db.init_schema(conn)
        uid = sqlite_db.create_user(conn, username="boss", password_hash=hash_password("Passw0rd!x1"), is_admin=True)
        token, expires, max_expires = new_token()
        sqlite_db.issue_token(
            conn, user_id=uid, token_hash=hash_token(token), expires_at=expires, max_expires_at=max_expires
        )
        for i in range(UI_PAGE_SIZE_TARGETS + EXTRA):
            conn.execute(
                "INSERT INTO targets (name, interval_seconds, retention_days, enable_ping, enable_http_check, "
                "enable_cert_expiration, host, created_at, updated_at) VALUES (?, 60, 30, 1, 0, 0, ?, ?, ?)",
                (f"T{i:03d}", f"host-{i:03d}.example.org", utcnow(), utcnow()),
            )
        conn.commit()
        sqlite_db.close_connection(conn)
        c = TestClient(app)
        c.get("/ui/login")
        c.cookies.set("token", token)
        yield c


def _rows(html: str) -> list[str]:
    return re.findall(r'<tr data-search="([^"]*)"', html)


def test_page_has_a_labelled_search_field_and_a_polite_status_region() -> None:
    page = (TEMPLATES / "targets.html").read_text(encoding="utf-8")
    field = re.search(r'<input\b[^>]*id="targets-search"[^>]*>', page, re.DOTALL)
    assert field and 'type="search"' in field.group(0) and 'autocomplete="off"' in field.group(0)
    assert re.search(r'<label\b[^>]*for="targets-search"[^>]*>\s*Search targets', page), "placeholder is no label"
    assert re.search(r'id="targets-search-status"[^>]*role="status"|role="status"[^>]*id="targets-search-status"', page)
    assert 'aria-live="polite"' in page
    # The field and the query live outside the swapped fragment, so polling cannot drop them.
    assert page.index('id="targets-search"') < page.index('id="targets-table"')
    assert 'name="all"' in page and "#targets-scope-state" in page


def test_fragment_pages_by_default_and_lists_every_target_for_the_search(client) -> None:
    paged = client.get("/ui/fragments/targets-table")
    assert paged.status_code == 200
    assert len(_rows(paged.text)) == UI_PAGE_SIZE_TARGETS
    assert 'data-total-pages="2"' in paged.text and 'data-scope="page"' in paged.text

    everything = client.get("/ui/fragments/targets-table?all=1&page=2")
    assert len(_rows(everything.text)) == UI_PAGE_SIZE_TARGETS + EXTRA
    assert 'data-total-pages="1"' in everything.text and 'data-scope="all"' in everything.text
    # Leaving the search returns to the page the user was on.
    assert re.search(r'id="targets-page-state"[^>]*value="2"', everything.text)
    assert "T054" in "".join(_rows(everything.text))


def test_search_data_lists_name_host_and_group_but_no_unrelated_defaults(client) -> None:
    rows = _rows(client.get("/ui/fragments/targets-table?all=1").text)
    first = next(row for row in rows if row.startswith("T000"))
    assert "host-000.example.org" in first and "Ungrouped" in first
    # tcp_ports defaults to 80 even when TCP is off; it must not become a searchable port.
    assert " 80" not in first
