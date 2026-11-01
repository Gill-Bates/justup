#!/usr/bin/env python3
#
# tests/test_utils_hardening.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""request-id sanitising, crypto cache/guard behavior, settings cache race, plain-text rendering."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import sqlite as sqlite_db
from app.services.notifications.email_template import render_template
from app.utils import crypto, settings_cache
from app.utils.request_id import RequestIDMiddleware
from tests import _support  # noqa: F401 - imported for its sys.path side effect


class TestRequestId:
    @pytest.fixture
    def client(self):
        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)

        @app.get("/")
        def index():
            return {}

        return TestClient(app)

    def test_valid_incoming_id_is_kept(self, client):
        assert client.get("/", headers={"X-Request-ID": "trace-1.2_ab"}).headers["X-Request-ID"] == "trace-1.2_ab"

    @pytest.mark.parametrize("bad", ["a" * 65, "id with space", "<script>", "a;b"])
    def test_invalid_incoming_id_is_replaced(self, client, bad):
        got = client.get("/", headers={"X-Request-ID": bad}).headers["X-Request-ID"]
        assert got != bad
        assert len(got) == 12


class TestCrypto:
    def test_reset_clears_primary_key_cache(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        crypto.reset_fernet_cache()
        first = crypto.get_primary_key()
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        assert crypto.get_primary_key() == first  # cached until reset
        crypto.reset_fernet_cache()
        assert crypto.get_primary_key() != first

    def test_domain_keys_are_separated(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
        a = crypto.derive_domain_keys(b"one")[0]
        assert a != crypto.derive_domain_keys(b"two")[0]
        assert a != crypto.get_primary_key()


class TestSettingsCache:
    @pytest.fixture
    def conn(self, tmp_path: Path):
        connection = sqlite_db.connect(tmp_path / "a.db")
        sqlite_db.init_schema(connection)
        settings_cache.invalidate_settings_cache()
        yield connection
        sqlite_db.close_connection(connection)

    def test_invalidation_during_read_does_not_republish_stale_value(self, conn, monkeypatch):
        sqlite_db.set_setting(conn, "k", "old")
        real_get = sqlite_db.get_setting

        def racing_get(c, key, default=None):
            value = real_get(c, key, default)  # reader observed the old value ...
            sqlite_db.set_setting(c, key, "new")  # ... then a writer updates and invalidates
            settings_cache.invalidate_settings_cache(key)
            return value

        monkeypatch.setattr(sqlite_db, "get_setting", racing_get)
        assert settings_cache.get_setting_cached(conn, "k", None) == "old"
        monkeypatch.setattr(sqlite_db, "get_setting", real_get)
        assert settings_cache.get_setting_cached(conn, "k", None) == "new"

    def test_key_includes_database_and_default(self, conn, tmp_path: Path):
        other = sqlite_db.connect(tmp_path / "b.db")
        try:
            sqlite_db.init_schema(other)
            sqlite_db.set_setting(conn, "k", "from-a")
            sqlite_db.set_setting(other, "k", "from-b")
            assert settings_cache.get_setting_cached(conn, "k") == "from-a"
            assert settings_cache.get_setting_cached(other, "k") == "from-b"
            assert settings_cache.get_setting_cached(conn, "missing", "d1") == "d1"
            assert settings_cache.get_setting_cached(conn, "missing", "d2") == "d2"
        finally:
            sqlite_db.close_connection(other)

    def test_cache_is_thread_safe_smoke(self, conn):
        sqlite_db.set_setting(conn, "k", "v")
        assert settings_cache.get_setting_cached(conn, "k") == "v"
        errors: list[BaseException] = []

        def work():
            try:
                for _ in range(50):
                    settings_cache.invalidate_settings_cache("k")
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


class TestPlainTextRendering:
    CTX: ClassVar[dict] = {"target": {"name": "AT&T <b>Müller</b>", "check_badges": "<span>PING</span> &amp; HTTP"}}

    def test_html_mode_escapes(self):
        assert render_template("{{target.name}}", self.CTX) == "AT&amp;T &lt;b&gt;Müller&lt;/b&gt;"

    def test_plain_text_mode_keeps_characters(self):
        assert render_template("{{target.name}}", self.CTX, mode="plain_text") == "AT&T <b>Müller</b>"

    def test_plain_text_strips_markup_from_prerendered_html(self):
        assert render_template("{{target.check_badges}}", self.CTX, mode="plain_text") == "PING & HTTP"

    def test_unknown_placeholder_still_empty_and_mode_validated(self):
        assert render_template("a{{nope}}b", self.CTX, mode="plain_text") == "ab"
        with pytest.raises(ValueError, match="mode"):
            render_template("x", {}, mode="xml")
