#!/usr/bin/env python3
#
# tests/test_geoip_asn_update.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001
"""GeoLite2-ASN is managed by the existing GeoIP updater (no network, fixtures only)."""

from __future__ import annotations

import io
import json
import logging
import types

import pytest

from app.utils import bootstrap
from app.utils.constants import GEOIP_ASN_DB_RELPATH, GEOIP_DB_RELPATH

SECRET = "licence-key-must-never-appear"  # noqa: S105 - canary value


class _Resp:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def net(monkeypatch):
    """Fake urlopen keyed by URL; any unexpected URL fails the test instead of touching the network."""
    served: dict[str, object] = {}
    requested: list[str] = []

    def fake_urlopen(req, *_a, **_k):
        requested.append(req.full_url)
        item = served[req.full_url]
        if isinstance(item, Exception):
            raise item
        return _Resp(item[0], item[1])

    monkeypatch.setattr(bootstrap, "urlopen", fake_urlopen)
    monkeypatch.setattr(bootstrap, "_verify_mmdb", lambda _p, *_a, **_k: True)
    return types.SimpleNamespace(served=served, requested=requested)


def test_asn_path_is_inside_geoip_dir_and_constant():
    assert str(GEOIP_ASN_DB_RELPATH) == "geoip/GeoLite2-ASN.mmdb"
    assert GEOIP_ASN_DB_RELPATH.parent == GEOIP_DB_RELPATH.parent
    assert bootstrap.GEOIP_ASN_DOWNLOAD_URL.startswith("https://")
    assert bootstrap.GEOIP_ASN_DOWNLOAD_URL.endswith("/GeoLite2-ASN.mmdb")


def test_inside_geoip_dir_guard(tmp_path):
    (tmp_path / "geoip").mkdir()
    assert bootstrap._is_inside_geoip_dir(tmp_path, tmp_path / GEOIP_ASN_DB_RELPATH)
    assert not bootstrap._is_inside_geoip_dir(tmp_path, tmp_path / "geoip" / ".." / "x.mmdb")
    assert not bootstrap._is_inside_geoip_dir(tmp_path, tmp_path / "other" / "GeoLite2-ASN.mmdb")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "geoip" / "link").symlink_to(outside)
    assert not bootstrap._is_inside_geoip_dir(tmp_path, tmp_path / "geoip" / "link" / "a.mmdb")


def test_asn_download_installs_atomically_with_meta(tmp_path, net):
    target = tmp_path / "geoip" / "GeoLite2-ASN.mmdb"
    net.served[bootstrap.GEOIP_ASN_DOWNLOAD_URL] = (b"new-asn", {"ETag": '"asn1"', "Last-Modified": "Mon"})
    assert bootstrap._download_geoip_db(target, force=True, db=bootstrap._ASN_DB) == target
    assert target.read_bytes() == b"new-asn"
    assert bootstrap._read_meta(target) == {"etag": '"asn1"', "last_modified": "Mon"}
    assert not list(target.parent.glob("geoip_*"))
    assert net.requested == [bootstrap.GEOIP_ASN_DOWNLOAD_URL]


def test_asn_replace_failure_keeps_old_file_and_meta(tmp_path, net, monkeypatch):
    target = tmp_path / "GeoLite2-ASN.mmdb"
    target.write_bytes(b"old-asn")
    bootstrap._meta_path(target).write_text(json.dumps({"etag": '"old"', "last_modified": ""}))
    net.served[bootstrap.GEOIP_ASN_DOWNLOAD_URL] = (b"bad", {"ETag": '"new"'})
    monkeypatch.setattr(bootstrap, "_verify_mmdb", lambda p, *_a, **_k: p == target)
    assert bootstrap._download_geoip_db(target, force=True, db=bootstrap._ASN_DB) is None
    assert target.read_bytes() == b"old-asn"
    assert bootstrap._read_meta(target) == {"etag": '"old"'}
    assert not list(tmp_path.glob("geoip_*"))


def test_asn_unchanged_etag_is_kept_using_asn_url(tmp_path, net):
    target = tmp_path / "GeoLite2-ASN.mmdb"
    target.write_bytes(b"asn")
    bootstrap._meta_path(target).write_text(json.dumps({"etag": '"a"', "last_modified": ""}))
    net.served[bootstrap.GEOIP_ASN_DOWNLOAD_URL] = (b"", {"ETag": '"a"', "Content-Length": "3"})
    assert bootstrap._download_geoip_db(target, db=bootstrap._ASN_DB) == target
    assert net.requested == [bootstrap.GEOIP_ASN_DOWNLOAD_URL]  # only the HEAD check
    assert target.read_bytes() == b"asn"


def test_verify_uses_edition_specific_size_and_type(tmp_path, monkeypatch):
    f = tmp_path / "x.mmdb"
    f.write_bytes(b"x" * 2_000_000 + bootstrap.MMDB_METADATA_MARKER)
    meta = types.SimpleNamespace(database_type="GeoLite2-ASN", build_epoch=1)

    class _Reader:
        def __init__(self, _p):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def metadata(self):
            return meta

    import geoip2.database

    monkeypatch.setattr(geoip2.database, "Reader", _Reader)
    assert bootstrap._verify_mmdb(f, bootstrap._ASN_DB)  # 2 MB passes the ASN floor
    assert not bootstrap._verify_mmdb(f)  # City floor (10 MB) and type reject it
    meta.database_type = "GeoLite2-City"
    assert not bootstrap._verify_mmdb(f, bootstrap._ASN_DB)  # wrong edition


def _run_bootstrap(tmp_path, monkeypatch, auto: bool, results: dict[str, object]):
    calls: list[tuple[str, bool]] = []

    def fake_dl(target_path, force=False, db=bootstrap._CITY_DB):
        calls.append((db.label, force))
        outcome = results[db.label]
        if outcome is True:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(db.label.encode())
            return target_path
        return None

    monkeypatch.setattr(bootstrap, "_download_geoip_db", fake_dl)
    monkeypatch.setattr(bootstrap.sqlite_db, "get_setting", lambda *_a, **_k: auto)
    data = tmp_path / "data"
    data.mkdir()
    bootstrap.bootstrap(data / "j.db", data / "tsdb", data_dir=data)
    return data, calls


@pytest.mark.parametrize(("city", "asn"), [(True, False), (False, True), (True, True)])
def test_partial_failure_is_independent(tmp_path, monkeypatch, city, asn):
    data, calls = _run_bootstrap(tmp_path, monkeypatch, True, {"City": city, "ASN": asn})
    assert [c[0] for c in calls] == ["City", "ASN"]  # both attempted, neither blocks the other
    assert (data / GEOIP_DB_RELPATH).exists() is city
    assert (data / GEOIP_ASN_DB_RELPATH).exists() is asn


def test_auto_update_off_downloads_only_missing_files(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "geoip").mkdir(parents=True)
    (data / GEOIP_DB_RELPATH).write_bytes(b"city")
    calls: list[str] = []

    def fake_dl(target_path, force=False, db=None):  # noqa: ARG001
        calls.append(db.label)

    monkeypatch.setattr(bootstrap, "_download_geoip_db", fake_dl)
    monkeypatch.setattr(bootstrap.sqlite_db, "get_setting", lambda *_a, **_k: False)
    bootstrap.bootstrap(data / "j.db", data / "tsdb", data_dir=data)
    assert calls == ["ASN"]  # existing installation: ASN appears, City untouched


def test_no_secret_in_logs_or_meta(tmp_path, net, caplog, monkeypatch):
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", SECRET)
    target = tmp_path / "GeoLite2-ASN.mmdb"
    net.served[bootstrap.GEOIP_ASN_DOWNLOAD_URL] = (b"asn", {"ETag": '"e"'})
    with caplog.at_level(logging.DEBUG, logger=bootstrap.__name__):
        bootstrap._download_geoip_db(target, force=True, db=bootstrap._ASN_DB)
        net.served[bootstrap.GEOIP_ASN_DOWNLOAD_URL] = (b"", {})
        monkeypatch.setattr(bootstrap, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
        bootstrap._download_geoip_db(target, force=True, db=bootstrap._ASN_DB)
    assert SECRET not in caplog.text
    assert SECRET not in bootstrap._meta_path(target).read_text()
    assert "?" not in bootstrap.GEOIP_ASN_DOWNLOAD_URL  # no credential-bearing query string


def test_build_info_accepts_asn_relpath(tmp_path):
    assert bootstrap.get_geoip_build_info(tmp_path, GEOIP_ASN_DB_RELPATH) is None


def test_about_page_reports_city_and_asn_database_separately(monkeypatch):
    from app.api import frontend

    def fake(_data_dir=None, relpath=GEOIP_DB_RELPATH):
        return ("GeoLite2-ASN", 222) if relpath == GEOIP_ASN_DB_RELPATH else ("GeoLite2-City", 111)

    monkeypatch.setattr(bootstrap, "get_geoip_build_info", fake)
    assert frontend._get_geoip_build_info() == 111
    assert frontend._get_geoip_build_info(asn=True) == 222
    monkeypatch.setattr(bootstrap, "get_geoip_build_info", lambda *_a, **_k: None)
    assert frontend._get_geoip_build_info(asn=True) is None  # file absent: the page shows N/A
