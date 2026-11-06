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
from app.utils.constants import SettingKeys
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

    def test_cache_is_thread_safe_smoke(self, conn, tmp_path):
        sqlite_db.set_setting(conn, "k", "v")
        assert settings_cache.get_setting_cached(conn, "k") == "v"
        errors: list[BaseException] = []
        seen: list[object] = []

        def work():
            reader = sqlite_db.connect(tmp_path / "a.db")  # connections are bound to their thread
            try:
                for _ in range(50):
                    settings_cache.invalidate_settings_cache("k")
                    seen.append(settings_cache.get_setting_cached(reader, "k"))
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)
            finally:
                sqlite_db.close_connection(reader)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)
        assert not errors
        assert seen == ["v"] * 200  # every interleaved read returns the stored value


class TestSettingsCacheConvenienceHelpers:
    """The helpers read the stored database keys (written here with literal names) and keep their caching rules."""

    @pytest.fixture
    def conn(self, tmp_path: Path):
        connection = sqlite_db.connect(tmp_path / "a.db")
        sqlite_db.init_schema(connection)
        settings_cache.invalidate_settings_cache()
        yield connection
        sqlite_db.close_connection(connection)

    def test_stored_key_names_are_unchanged(self):
        assert (
            SettingKeys.AUTH_DISABLED,
            SettingKeys.PURGE_ENABLED,
            SettingKeys.USE_UTC_DASHBOARD,
            SettingKeys.PDF_PAGE_SIZE,
        ) == ("auth_disabled", "purge_enabled", "use_utc_dashboard", "pdf_page_size")

    def test_missing_settings_give_the_defaults(self, conn):
        assert settings_cache.is_auth_disabled(conn) is False
        assert settings_cache.is_purge_enabled(conn) is True
        assert settings_cache.use_utc_dashboard(conn) is False
        assert settings_cache.get_pdf_page_size(conn) == "a4"

    def test_stored_values_win_including_explicit_false(self, conn):
        sqlite_db.set_setting(conn, "auth_disabled", True)
        sqlite_db.set_setting(conn, "purge_enabled", False)
        sqlite_db.set_setting(conn, "use_utc_dashboard", True)
        sqlite_db.set_setting(conn, "pdf_page_size", "LETTER")
        assert settings_cache.is_auth_disabled(conn) is True
        assert settings_cache.is_purge_enabled(conn) is False  # an explicit False is not replaced by the default
        assert settings_cache.use_utc_dashboard(conn) is True
        assert settings_cache.get_pdf_page_size(conn) == "letter"

    def test_empty_page_size_falls_back_to_the_default(self, conn):
        sqlite_db.set_setting(conn, "pdf_page_size", "")
        assert settings_cache.get_pdf_page_size(conn) == "a4"

    @pytest.mark.parametrize(
        ("key", "helper", "first", "second"),
        [
            ("purge_enabled", settings_cache.is_purge_enabled, False, True),
            ("use_utc_dashboard", settings_cache.use_utc_dashboard, True, False),
            ("pdf_page_size", settings_cache.get_pdf_page_size, "letter", "a4"),
        ],
    )
    def test_cached_helpers_serve_the_cache_until_invalidated(self, conn, monkeypatch, key, helper, first, second):
        sqlite_db.set_setting(conn, key, first)
        assert helper(conn) == first
        real_invalidate = settings_cache.invalidate_settings_cache
        monkeypatch.setattr(settings_cache, "invalidate_settings_cache", lambda *_a, **_k: None)
        sqlite_db.set_setting(conn, key, second)  # a write whose invalidation is lost
        monkeypatch.setattr(settings_cache, "invalidate_settings_cache", real_invalidate)
        assert helper(conn) == first  # still cached
        real_invalidate(key)
        assert helper(conn) == second

    def test_auth_disabled_is_never_cached(self, conn):
        sqlite_db.set_setting(conn, "auth_disabled", True)
        assert settings_cache.is_auth_disabled(conn) is True
        sqlite_db.set_setting(conn, "auth_disabled", False)
        assert settings_cache.is_auth_disabled(conn) is False  # no invalidation needed


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
