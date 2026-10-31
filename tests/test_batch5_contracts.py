#!/usr/bin/env python3
#
# tests/test_batch5_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""Contract tests: auth_disabled freshness, restore atomicity, client IP, CSRF cookie, GeoIP, dotenv."""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.db import sqlite as sqlite_db
from app.middleware.csrf import CSRFMiddleware
from app.utils import auth_helpers, bootstrap, settings_cache
from app.utils import backup as backup_mod
from app.utils import config as config_mod
from app.utils.client_ip import TRUSTED_PROXIES_ENV, resolve_trusted_client_ip
from app.utils.constants import AlertStatus, SettingKeys
from app.utils.rate_limit import limiter
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _request(peer: str | None, headers: list[tuple[str, str]] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or [])]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw,
            "scheme": "http",
            "query_string": b"",
            "server": ("testserver", 80),
            "client": (peer, 1234) if peer else None,
        }
    )


# --- CR-01: auth_disabled is always read from the database ------------------------------------


@pytest.fixture
def two_conns(tmp_path: Path):
    path = tmp_path / "auth.db"
    a = sqlite_db.connect(path)
    sqlite_db.init_schema(a)
    sqlite_db.create_user(a, username="root", password_hash="x", is_admin=True)  # noqa: S106
    b = sqlite_db.connect(path)
    settings_cache.invalidate_settings_cache()
    yield a, b
    sqlite_db.close_connection(a)
    sqlite_db.close_connection(b)
    settings_cache.invalidate_settings_cache()


def test_auth_disabled_change_in_other_worker_applies_immediately(two_conns):
    a, b = two_conns
    sqlite_db.set_setting(a, SettingKeys.AUTH_DISABLED, True)
    # Warm the (per-process) cache the way the old implementation did
    assert settings_cache.get_setting_cached(b, "auth_disabled", False, ttl=settings_cache.SECURITY_TTL_SECONDS)
    assert auth_helpers.resolve_user_optional(_request("203.0.113.1"), b) is not None  # public mode
    # Another worker flips the flag; this process's cache is not invalidated
    a.execute("UPDATE settings SET value='false' WHERE key='auth_disabled'")
    a.commit()
    assert settings_cache.is_auth_disabled(b) is False
    assert auth_helpers.resolve_user_optional(_request("203.0.113.1"), b) is None


def test_auth_disabled_default_is_false(two_conns):
    _, b = two_conns
    assert settings_cache.is_auth_disabled(b) is False
    assert auth_helpers.resolve_user_optional(_request("203.0.113.1"), b) is None


# --- CR-08: bearer scheme is case-insensitive -------------------------------------------------


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_scheme_case_insensitive(scheme):
    req = _request("1.2.3.4", [("Authorization", f"{scheme} abc")])
    assert auth_helpers._extract_bearer_token(req) == "abc"


def test_bearer_rejects_other_schemes_missing_and_oversized():
    assert auth_helpers._extract_bearer_token(_request("1.2.3.4", [("Authorization", "Basic abc")])) is None
    assert auth_helpers._extract_bearer_token(_request("1.2.3.4")) is None
    assert auth_helpers._extract_bearer_token(_request("1.2.3.4", [("Authorization", "Bearer ")])) is None
    long_value = "Bearer " + "a" * (auth_helpers.MAX_TOKEN_LENGTH + 1)
    assert auth_helpers._extract_bearer_token(_request("1.2.3.4", [("Authorization", long_value)])) is None


# --- CR-02 / CR-07: restore atomicity and structure -------------------------------------------


def _tree(root: Path, names: tuple[str, ...], tag: str) -> None:
    for name in names:
        (root / name / "nested").mkdir(parents=True)
        (root / name / "nested" / "f.bin").write_bytes(f"{tag}-{name}".encode())


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("fail_at", [2, 3, 4, 5, 6])
def test_restore_failure_preserves_originals_bytewise(tmp_path, monkeypatch, fail_at):
    data = tmp_path / "data"
    _tree(data, ("sqlite", "tsdb", "pdf_reports"), "old")
    (data / "keep.txt").write_text("keep")
    staged = data / ".stage" / "data"
    _tree(staged, ("sqlite", "tsdb", "pdf_reports"), "new")
    before = _snapshot(data / "sqlite"), _snapshot(data / "tsdb"), _snapshot(data / "pdf_reports")

    real, calls = backup_mod._rename, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == fail_at:
            if "stage" in str(src):  # simulate a half-written target before failing
                Path(dst).mkdir()
                (Path(dst) / "partial").write_text("x")
            raise OSError("injected")
        return real(src, dst)

    monkeypatch.setattr(backup_mod, "_rename", flaky)
    with pytest.raises(backup_mod.RestoreFailedError) as exc:
        backup_mod.apply_restore(staged, data)
    assert exc.value.rolled_back is True
    after = _snapshot(data / "sqlite"), _snapshot(data / "tsdb"), _snapshot(data / "pdf_reports")
    assert after == before
    assert (data / "keep.txt").read_text() == "keep"
    assert not list(data.glob(".rollback_*"))


def test_restore_rollback_failure_keeps_rollback_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _tree(data, ("sqlite", "tsdb"), "old")
    staged = tmp_path / "stage"
    _tree(staged, ("sqlite", "tsdb"), "new")
    real, calls = backup_mod._rename, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] >= 4:  # fail the second placement and every rollback rename
            raise OSError("injected")
        return real(src, dst)

    monkeypatch.setattr(backup_mod, "_rename", flaky)
    with pytest.raises(backup_mod.RestoreFailedError) as exc:
        backup_mod.apply_restore(staged, data)
    assert exc.value.rolled_back is False
    assert exc.value.rollback_dir is not None and exc.value.rollback_dir.exists()


def test_restore_with_missing_source_directories_and_repeat(tmp_path):
    data = tmp_path / "data"
    _tree(data, ("sqlite", "tsdb"), "old")
    for round_no in range(2):
        staged = tmp_path / f"stage{round_no}"
        _tree(staged, ("sqlite",), f"new{round_no}")  # tsdb absent from the archive
        assert backup_mod.apply_restore(staged, data) == ["sqlite"]
        assert (data / "sqlite" / "nested" / "f.bin").read_bytes() == f"new{round_no}-sqlite".encode()
        assert (data / "tsdb" / "nested" / "f.bin").read_bytes() == b"old-tsdb"
        assert not list(data.glob(".rollback_*"))


def test_restore_rejects_file_instead_of_directory_before_any_change(tmp_path):
    data = tmp_path / "data"
    _tree(data, ("sqlite", "tsdb"), "old")
    staged = tmp_path / "stage"
    _tree(staged, ("tsdb",), "new")
    (staged / "sqlite").write_text("not a directory")
    with pytest.raises(backup_mod.BackupError):
        backup_mod.apply_restore(staged, data)
    assert (data / "tsdb" / "nested" / "f.bin").read_bytes() == b"old-tsdb"
    assert (data / "sqlite" / "nested" / "f.bin").read_bytes() == b"old-sqlite"


def _members(entries: list[tuple[str, bytes | None]]) -> list[tarfile.TarInfo]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
            else:
                info.size = len(content)
            tar.addfile(info, io.BytesIO(content or b""))
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        return tar.getmembers()


@pytest.mark.parametrize("name", ["data", "data/sqlite", "data/tsdb", "data/pdf_reports"])
def test_validate_members_rejects_files_for_directory_entries(name):
    with pytest.raises(backup_mod.BackupError):
        backup_mod.validate_members(_members([(name, b"x")]))


def test_validate_members_accepts_files_below_and_empty_directories():
    backup_mod.validate_members(
        _members([("data", None), ("data/sqlite", None), ("data/tsdb", None), ("data/sqlite/justup.db", b"x")])
    )


# --- CR-03: X-Forwarded-For is evaluated right to left ----------------------------------------


@pytest.fixture
def proxies(monkeypatch):
    monkeypatch.setenv(TRUSTED_PROXIES_ENV, "10.0.0.0/8,fd00::/8")


@pytest.mark.usefixtures("proxies")
def test_xff_from_untrusted_peer_is_ignored():
    req = _request("203.0.113.9", [("X-Forwarded-For", "1.2.3.4")])
    assert resolve_trusted_client_ip(req) == "203.0.113.9"


@pytest.mark.usefixtures("proxies")
def test_xff_appending_proxy_cannot_be_spoofed():
    req = _request("10.0.0.1", [("X-Forwarded-For", "6.6.6.6, 198.51.100.7")])
    assert resolve_trusted_client_ip(req) == "198.51.100.7"


@pytest.mark.usefixtures("proxies")
def test_xff_overwriting_proxy_and_multiple_trusted_hops():
    assert resolve_trusted_client_ip(_request("10.0.0.1", [("X-Forwarded-For", "198.51.100.7")])) == "198.51.100.7"
    req = _request("10.0.0.1", [("X-Forwarded-For", "6.6.6.6, 198.51.100.7, 10.0.0.2, 10.0.0.3")])
    assert resolve_trusted_client_ip(req) == "198.51.100.7"


@pytest.mark.usefixtures("proxies")
def test_xff_multiple_header_lines_and_ipv6():
    req = _request("fd00::1", [("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", "2001:db8::5, fd00::2")])
    assert resolve_trusted_client_ip(req) == "2001:db8::5"


@pytest.mark.usefixtures("proxies")
def test_xff_invalid_entries_fall_back_to_peer():
    assert resolve_trusted_client_ip(_request("10.0.0.1", [("X-Forwarded-For", "198.51.100.7, junk")])) == "10.0.0.1"
    assert resolve_trusted_client_ip(_request("10.0.0.1", [("X-Forwarded-For", "junk, 10.0.0.2")])) == "10.0.0.1"


@pytest.mark.usefixtures("proxies")
def test_xff_all_hops_trusted_uses_leftmost():
    assert resolve_trusted_client_ip(_request("10.0.0.1", [("X-Forwarded-For", "10.9.9.9, 10.0.0.2")])) == "10.9.9.9"


@pytest.mark.usefixtures("proxies")
def test_limiter_key_matches_resolved_client_ip():
    req = _request("10.0.0.1", [("X-Forwarded-For", "6.6.6.6, 198.51.100.7")])
    assert limiter._key_func(req) == resolve_trusted_client_ip(req) == "198.51.100.7"
    spoof = _request("203.0.113.9", [("X-Forwarded-For", "1.2.3.4")])
    assert limiter._key_func(spoof) == "203.0.113.9"


@pytest.mark.usefixtures("proxies")
def test_forwarded_appending_proxy_cannot_be_spoofed():
    req = _request("10.0.0.1", [("Forwarded", "for=6.6.6.6, for=198.51.100.7;proto=https")])
    assert resolve_trusted_client_ip(req) == "198.51.100.7"


@pytest.mark.usefixtures("proxies")
def test_forwarded_multiple_trusted_hops_and_header_lines():
    req = _request("10.0.0.1", [("Forwarded", "for=6.6.6.6"), ("Forwarded", "for=198.51.100.7, for=10.0.0.2")])
    assert resolve_trusted_client_ip(req) == "198.51.100.7"
    assert resolve_trusted_client_ip(_request("10.0.0.1", [("Forwarded", "for=10.9.9.9, for=10.0.0.2")])) == "10.9.9.9"


@pytest.mark.usefixtures("proxies")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('for="[2001:db8::1]:4711"', "2001:db8::1"),
        ('for="[2001:db8::1]"', "2001:db8::1"),
        ('for="192.0.2.60:4711";proto=https', "192.0.2.60"),
        ("FOR=192.0.2.60;by=10.0.0.1", "192.0.2.60"),
        ('by="a,b;c=d";for=192.0.2.61', "192.0.2.61"),
    ],
)
def test_forwarded_rfc7239_node_forms(value, expected):
    assert resolve_trusted_client_ip(_request("10.0.0.1", [("Forwarded", value)])) == expected


@pytest.mark.usefixtures("proxies")
@pytest.mark.parametrize(
    "value",
    [
        "for=unknown",
        "for=_hidden",
        "for=6.6.6.6, for=unknown",
        "for=6.6.6.6, proto=https",
        'for="[2001:db8::1"',
        "for=192.0.2.60:port",
        "for=999.1.1.1",
        "garbage",
    ],
)
def test_forwarded_invalid_entries_fall_back_to_peer(value):
    assert resolve_trusted_client_ip(_request("10.0.0.1", [("Forwarded", value)])) == "10.0.0.1"


@pytest.mark.usefixtures("proxies")
def test_forwarded_from_untrusted_peer_is_ignored():
    req = _request("203.0.113.9", [("Forwarded", "for=1.2.3.4")])
    assert resolve_trusted_client_ip(req) == "203.0.113.9"


# --- CR-04: CSRF cookie Secure flag follows the trusted-proxy aware scheme --------------------


def _csrf_cookie(peer: str, headers: dict[str, str], base_url: str = "http://testserver") -> str:
    async def home(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", home)])
    app.add_middleware(CSRFMiddleware)
    client = TestClient(app, base_url=base_url, client=(peer, 5000))
    return client.get("/", headers=headers).headers["set-cookie"].lower()


@pytest.mark.usefixtures("proxies")
def test_csrf_cookie_secure_only_with_trusted_forwarded_proto():
    assert "secure" in _csrf_cookie("10.0.0.1", {"X-Forwarded-Proto": "https"})
    assert "secure" not in _csrf_cookie("203.0.113.9", {"X-Forwarded-Proto": "https"})
    assert "secure" not in _csrf_cookie("10.0.0.1", {"X-Forwarded-Proto": "http"}, "https://testserver")
    assert "secure" in _csrf_cookie("203.0.113.9", {"X-Forwarded-Proto": "http"}, "https://testserver")
    cookie = _csrf_cookie("fd00::1", {"X-Forwarded-Proto": "https"})
    assert "secure" in cookie and "samesite=lax" in cookie and "httponly" not in cookie


# --- CR-05 / CR-06: GeoIP download limits and change detection --------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _stub_download(monkeypatch, response: _FakeResponse, limit: int = 100):
    monkeypatch.setattr(bootstrap, "GEOIP_MAX_BYTES", limit)
    monkeypatch.setattr(bootstrap, "urlopen", lambda *_a, **_k: response)
    monkeypatch.setattr(bootstrap, "_verify_mmdb", lambda _p: True)


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b"x" * 10, {"Content-Length": "101"}),  # declared too large
        (b"x" * 101, {}),  # no header, body too large
        (b"x" * 101, {"Content-Length": "5"}),  # lying header
    ],
)
def test_geoip_download_over_limit_aborts_and_keeps_existing(tmp_path, monkeypatch, body, headers):
    target = tmp_path / "GeoLite2-City.mmdb"
    target.write_bytes(b"old-valid")
    _stub_download(monkeypatch, _FakeResponse(body, headers))
    assert bootstrap._download_geoip_db(target, force=True) is None
    assert target.read_bytes() == b"old-valid"
    assert not list(tmp_path.glob("geoip_*"))


def test_geoip_download_exactly_at_limit_is_installed(tmp_path, monkeypatch):
    target = tmp_path / "GeoLite2-City.mmdb"
    _stub_download(monkeypatch, _FakeResponse(b"x" * 100, {"Content-Length": "100", "ETag": '"e1"'}))
    assert bootstrap._download_geoip_db(target) == target
    assert target.stat().st_size == 100
    assert bootstrap._read_meta(target) == {"etag": '"e1"'}
    assert not list(tmp_path.glob("geoip_*"))


def test_geoip_update_reason(tmp_path):
    db = tmp_path / "GeoLite2-City.mmdb"
    db.write_bytes(b"x" * 10)
    meta = bootstrap._meta_path(db)
    meta.write_text(json.dumps({"etag": '"a"', "last_modified": ""}))
    # same size, changed ETag -> update
    assert bootstrap._update_reason(db, 10, {"size": 10, "etag": '"b"', "last_modified": None})
    # unchanged ETag -> keep, even if the size differs
    assert bootstrap._update_reason(db, 10, {"size": 99, "etag": '"a"', "last_modified": None}) is None
    # remote down -> keep
    assert bootstrap._update_reason(db, 10, None) is None
    # no validators: size fallback
    meta.unlink()
    assert bootstrap._update_reason(db, 10, {"size": 11, "etag": None, "last_modified": None})
    assert bootstrap._update_reason(db, 10, {"size": 10, "etag": None, "last_modified": None}) is None


def test_geoip_update_reason_max_age_without_indicator(tmp_path):
    db = tmp_path / "GeoLite2-City.mmdb"
    db.write_bytes(b"x")
    remote = {"size": None, "etag": None, "last_modified": None}
    assert bootstrap._update_reason(db, 1, remote) is None
    old = time.time() - bootstrap.GEOIP_MAX_AGE_SECONDS - 60
    os.utime(db, (old, old))
    assert bootstrap._update_reason(db, 1, remote)


# --- CR-09: dotenv parsing --------------------------------------------------------------------


def _load(tmp_path: Path, monkeypatch, text: str, keys: list[str]) -> dict[str, str | None]:
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    config_mod.load_dotenv(env)
    result = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    return result


def test_dotenv_comments_quotes_and_empty_values(tmp_path, monkeypatch):
    text = (
        'B5_A=plain # comment\nB5_B="/srv/data #2"\nB5_C=\'x #y\' # c\nB5_D="say \\"hi\\" #1"\n'
        'B5_E=\nB5_F=http://h/p#frag\nB5_G=""\n'
    )
    keys = ["B5_A", "B5_B", "B5_C", "B5_D", "B5_E", "B5_F", "B5_G"]
    got = _load(tmp_path, monkeypatch, text, keys)
    assert got == {
        "B5_A": "plain",
        "B5_B": "/srv/data #2",
        "B5_C": "x #y",
        "B5_D": 'say "hi" #1',
        "B5_E": "",
        "B5_F": "http://h/p#frag",
        "B5_G": "",
    }


def test_dotenv_does_not_override_existing_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("B5_X", "from-env")
    env = tmp_path / ".env"
    env.write_text('B5_X="from-file #1"\n', encoding="utf-8")
    config_mod.load_dotenv(env)
    assert os.environ["B5_X"] == "from-env"


# --- DRY-02 / DRY-03 --------------------------------------------------------------------------


def test_alert_status_constants_match_stored_values():
    assert {s.value for s in AlertStatus} == {"open", "resolved"}


def test_bootstrap_keeps_existing_geoip_setting_and_seeds_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    db = tmp_path / "sqlite" / "justup.db"
    db.parent.mkdir()
    conn = sqlite_db.connect(db)
    sqlite_db.init_schema(conn)
    sqlite_db.set_setting(conn, "geoip_auto_update", False)
    sqlite_db.close_connection(conn)
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    assert sqlite_db.get_setting(conn, SettingKeys.GEOIP_AUTO_UPDATE, None) is False
    conn.execute("DELETE FROM settings WHERE key='geoip_auto_update'")
    conn.commit()
    sqlite_db.close_connection(conn)
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    assert sqlite_db.get_setting(conn, "geoip_auto_update", None) is True
    sqlite_db.close_connection(conn)


# --- validate_runtime_config performs the encryption key check ---------------------------------


def test_validate_runtime_config_rejects_missing_key(monkeypatch, caplog):
    from app.utils.crypto import MissingEncryptionKeyError

    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEYS", raising=False)
    with caplog.at_level("INFO"), pytest.raises(MissingEncryptionKeyError):
        config_mod.validate_runtime_config()
    assert "Runtime configuration validated" not in caplog.text


def test_validate_runtime_config_rejects_invalid_key_without_leaking_it(monkeypatch, caplog):
    from app.utils.crypto import MissingEncryptionKeyError

    monkeypatch.delenv("SECRET_KEYS", raising=False)
    monkeypatch.setenv("SECRET_KEY", "not-a-fernet-key-SENTINEL")
    with caplog.at_level("INFO"), pytest.raises(MissingEncryptionKeyError) as exc:
        config_mod.validate_runtime_config()
    assert "SENTINEL" not in str(exc.value)
    assert "SENTINEL" not in caplog.text


def test_validate_runtime_config_accepts_valid_key(monkeypatch, caplog):
    from cryptography.fernet import Fernet

    monkeypatch.delenv("SECRET_KEYS", raising=False)
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    with caplog.at_level("INFO"):
        config_mod.validate_runtime_config()
    assert "Runtime configuration validated" in caplog.text


# --- GeoIP build info follows the configured data directory ------------------------------------


def test_geoip_build_info_defaults_to_configured_data_dir(tmp_path, monkeypatch):
    import sys
    import types

    (tmp_path / bootstrap.GEOIP_DB_RELPATH).parent.mkdir()
    (tmp_path / bootstrap.GEOIP_DB_RELPATH).write_bytes(b"x")
    meta = types.SimpleNamespace(database_type="GeoLite2-City", build_epoch=123)

    class _Reader:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            assert Path(self.path) == tmp_path / bootstrap.GEOIP_DB_RELPATH
            return self

        def __exit__(self, *_a):
            return False

        def metadata(self):
            return meta

    fake_db = types.SimpleNamespace(Reader=_Reader)
    monkeypatch.setitem(sys.modules, "geoip2", types.SimpleNamespace(database=fake_db))
    monkeypatch.setitem(sys.modules, "geoip2.database", fake_db)
    monkeypatch.setattr(bootstrap, "DATA_DIR", tmp_path)
    assert bootstrap.get_geoip_build_info() == ("GeoLite2-City", 123)


def test_bootstrap_data_dir_is_the_config_data_dir_with_justup_data_dir(tmp_path):
    import subprocess
    import sys

    code = (
        "from app.utils import bootstrap, config; assert bootstrap.DATA_DIR is config.DATA_DIR; print(config.DATA_DIR)"
    )
    env = {**os.environ, "JUSTUP_DATA_DIR": str(tmp_path), "PYTHONPATH": os.pathsep.join(sys.path)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env)
    assert out.stdout.strip() == str(tmp_path)


# --- Settings defaults have a single definition ------------------------------------------------


def _default_setting_values():
    from app.utils import constants

    return {
        SettingKeys.PURGE_ENABLED: constants.DEFAULT_PURGE_ENABLED,
        SettingKeys.GEOIP_AUTO_UPDATE: constants.DEFAULT_GEOIP_AUTO_UPDATE,
        SettingKeys.PDF_PAGE_SIZE: constants.DEFAULT_PDF_PAGE_SIZE,
        SettingKeys.METRICS_TIMEOUT_MINUTES: constants.DEFAULT_METRICS_TIMEOUT_MINUTES,
    }


def test_fresh_db_is_seeded_with_defaults_and_settings_fallback_agrees(tmp_path, monkeypatch):
    from app.api.settings import _load

    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    db = tmp_path / "sqlite" / "justup.db"
    db.parent.mkdir()
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    expected = _default_setting_values()
    for key, value in expected.items():
        assert sqlite_db.get_setting(conn, key, "<missing>") == value
    assert "downsample_enabled" not in {row[0] for row in conn.execute("SELECT key FROM settings")}
    conn.execute("DELETE FROM settings WHERE key IN (?, ?, ?, ?)", tuple(expected))
    conn.commit()
    loaded = _load(conn)
    assert loaded.purge_enabled is expected[SettingKeys.PURGE_ENABLED]
    assert loaded.geoip_auto_update is expected[SettingKeys.GEOIP_AUTO_UPDATE]
    assert loaded.pdf_page_size == expected[SettingKeys.PDF_PAGE_SIZE]
    assert loaded.metrics_timeout_minutes == expected[SettingKeys.METRICS_TIMEOUT_MINUTES]
    sqlite_db.close_connection(conn)


def test_bootstrap_seeds_only_missing_settings_and_keeps_custom_values(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "_download_geoip_db", lambda *_a, **_k: None)
    db = tmp_path / "sqlite" / "justup.db"
    db.parent.mkdir()
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    sqlite_db.set_setting(conn, SettingKeys.PDF_PAGE_SIZE, "letter")
    sqlite_db.set_setting(conn, SettingKeys.METRICS_TIMEOUT_MINUTES, 17)
    conn.execute("DELETE FROM settings WHERE key=?", (SettingKeys.PURGE_ENABLED,))
    conn.commit()
    sqlite_db.close_connection(conn)
    bootstrap.bootstrap(db, tmp_path / "tsdb", tmp_path)
    conn = sqlite_db.connect(db)
    assert sqlite_db.get_setting(conn, SettingKeys.PDF_PAGE_SIZE, None) == "letter"
    assert sqlite_db.get_setting(conn, SettingKeys.METRICS_TIMEOUT_MINUTES, None) == 17
    assert sqlite_db.get_setting(conn, SettingKeys.PURGE_ENABLED, None) is True
    sqlite_db.close_connection(conn)
