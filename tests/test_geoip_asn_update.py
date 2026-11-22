#!/usr/bin/env python3
#
# tests/test_geoip_asn_update.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup refresh of the GeoLite2 databases: when bootstrap asks gillbates_geoip to download (no network)."""

from __future__ import annotations

import pytest
from gillbates_geoip import GeoIPUnavailableError

from app.utils import bootstrap
from app.utils.constants import GEOIP_ASN_DB_RELPATH, GEOIP_DB_RELPATH


class _Recorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def ensure(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(bootstrap, "geoip_databases", lambda _city_path: rec)
    return rec


def _touch(data_dir, *relpaths) -> None:
    for relpath in relpaths:
        (data_dir / relpath).parent.mkdir(parents=True, exist_ok=True)
        (data_dir / relpath).write_bytes(b"x")


def test_auto_update_on_always_asks_the_package(tmp_path, recorder) -> None:
    _touch(tmp_path, GEOIP_DB_RELPATH, GEOIP_ASN_DB_RELPATH)
    bootstrap.refresh_geoip(tmp_path, auto_update=True)
    assert recorder.calls == 1


def test_auto_update_off_downloads_only_missing_files(tmp_path, recorder) -> None:
    _touch(tmp_path, GEOIP_DB_RELPATH, GEOIP_ASN_DB_RELPATH)
    bootstrap.refresh_geoip(tmp_path, auto_update=False)
    assert recorder.calls == 0
    (tmp_path / GEOIP_ASN_DB_RELPATH).unlink()
    bootstrap.refresh_geoip(tmp_path, auto_update=False)
    assert recorder.calls == 1


def test_a_failed_download_never_blocks_startup(tmp_path, monkeypatch) -> None:
    rec = _Recorder(GeoIPUnavailableError("offline"))
    monkeypatch.setattr(bootstrap, "geoip_databases", lambda _city_path: rec)
    bootstrap.refresh_geoip(tmp_path, auto_update=True)
    assert rec.calls == 1
