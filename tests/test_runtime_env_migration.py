#!/usr/bin/env python3
#
# tests/test_runtime_env_migration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""ENV reduction: UI-managed settings, runtime effect, ENV allowlist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError

from app.db import sqlite as sqlite_db
from app.models.settings import Settings, SettingsUpdate
from app.services.notifications.signal_cli_backend import SignalCliBackend
from app.services.notifications.signal_errors import SignalNotRegistered
from app.utils import crypto, runtime_settings
from app.utils.constants import SettingKeys
from app.utils.crypto import decrypt_secret, encrypt_secret
from app.utils.runtime_fields import load_runtime_fields, validate_quality_targets
from tests import _support  # noqa: F401 - imported for its sys.path side effect

APP_DIR = Path(__file__).resolve().parents[1] / "app"
SECRET_VALUE = "super-secret-ip-key"  # noqa: S105 - throwaway test value

# The complete ENV surface that app/ may read (bootstrap values, secrets, security boundaries).
ALLOWED_ENV_NAMES = {
    "SECRET_KEY",
    "SECRET_KEYS",
    "LOG_LEVEL",
    "UPMON_TRUSTED_PROXIES",
    "JUSTUP_DATA_DIR",
    "DATA_DIR",
    "CACHE_DIR",
    "JUSTUP_HOST",
    "JUSTUP_PORT",
    "JUSTUP_BACKUP_HMAC_SECRET",
    "SIGNAL_CLI_PATH",
    "SIGNAL_DATA_DIR",
    "TESTING",
    "PYTEST_CURRENT_TEST",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    crypto.reset_fernet_cache()
    runtime_settings.clear_runtime_cache()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def conn(data_dir: Path):
    connection = sqlite_db.connect(data_dir / "sqlite" / "justup.db")
    sqlite_db.init_schema(connection)
    yield connection
    sqlite_db.close_connection(connection)


def test_defaults_match_former_env_defaults(conn) -> None:
    fields = load_runtime_fields(conn)
    assert fields["rate_limit_auth"] == "10/minute"
    assert fields["quality_min_score"] == 70
    assert fields["quality_rtt_threshold_ms"] == 500
    assert fields["provider_lookup_enabled"] is True
    assert fields["provider_lookup_cache_ttl_seconds"] == 1800
    assert fields["online_geo_enabled"] is False
    # The POP refresh interval/initial-delay settings were removed (refresh is self-managing);
    # they must no longer surface as runtime fields.
    assert "pop_refresh_interval_seconds" not in fields
    assert "pop_refresh_initial_delay_seconds" not in fields
    assert Settings(**fields).ip_api_key_set is False


@pytest.mark.parametrize(
    "payload",
    [
        {"rate_limit_auth": "fast"},
        {"rate_limit_auth": "0/minute"},
        {"quality_min_score": 101},
        {"quality_rtt_threshold_ms": 0},
        {"provider_lookup_cache_ttl_seconds": 5},
    ],
)
def test_update_model_rejects_out_of_range(payload: dict) -> None:
    with pytest.raises(ValidationError):
        SettingsUpdate(**payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"pop_refresh_interval_seconds": 10},
        {"pop_refresh_initial_delay_seconds": 3600},
        {"allow_insecure_ip_api_http": True},
    ],
)
def test_update_model_rejects_removed_pop_settings(payload: dict) -> None:
    # The two POP refresh settings were removed entirely. A stale client still sending them must be
    # rejected (extra="forbid"), never have the value silently accepted as effective config.
    with pytest.raises(ValidationError):
        SettingsUpdate(**payload)


def test_quality_targets_validation() -> None:
    assert validate_quality_targets("") == ""
    assert validate_quality_targets("1.1.1.1, 8.8.8.8 ,https://example.org") == "1.1.1.1,8.8.8.8,https://example.org"
    with pytest.raises(HTTPException):
        validate_quality_targets("1.1.1.1,8.8.8.8")
    with pytest.raises(HTTPException):
        validate_quality_targets("1.1.1.1,8.8.8.8,bad host!")


def test_runtime_change_applies_without_restart(conn) -> None:
    from app.monitor import quality_probe
    from app.utils.rate_limit import RATE_LIMIT_AUTH

    assert RATE_LIMIT_AUTH() == "10/minute"
    assert quality_probe.get_min_score() == 70
    sqlite_db.set_setting(conn, SettingKeys.RATE_LIMIT_AUTH, "3/hour")
    sqlite_db.set_setting(conn, SettingKeys.QUALITY_MIN_SCORE, 55)
    sqlite_db.set_setting(conn, SettingKeys.QUALITY_TARGETS, "a.example,b.example,c.example")
    assert RATE_LIMIT_AUTH() == "3/hour"
    assert quality_probe.get_min_score() == 55
    assert quality_probe.get_quality_targets() == ["a.example", "b.example", "c.example"]


def test_invalid_stored_value_falls_back_to_default(conn) -> None:
    from app.utils.rate_limit import RATE_LIMIT_AUTH

    sqlite_db.set_setting(conn, SettingKeys.RATE_LIMIT_AUTH, "garbage")
    assert RATE_LIMIT_AUTH() == "10/minute"


def test_ip_api_key_is_encrypted_and_never_exposed(conn) -> None:
    sqlite_db.set_setting(conn, SettingKeys.IP_API_KEY, encrypt_secret(SECRET_VALUE))
    stored = sqlite_db.get_setting(conn, SettingKeys.IP_API_KEY)
    assert stored and SECRET_VALUE not in stored
    assert decrypt_secret(stored) == SECRET_VALUE
    assert runtime_settings.get_ip_api_key() == SECRET_VALUE
    fields = load_runtime_fields(conn)
    assert fields["ip_api_key_set"] is True
    assert SECRET_VALUE not in Settings(**fields).model_dump_json()


def _backend(accounts: list[str]) -> SignalCliBackend:
    backend = object.__new__(SignalCliBackend)
    backend.list_accounts = lambda: accounts  # type: ignore[method-assign]
    return backend


def test_sender_valid_configured_is_used() -> None:
    assert _backend(["+491", "+492"]).get_valid_sender("+492") == "+492"


def test_sender_unset_uses_first_account() -> None:
    assert _backend(["+491", "+492"]).get_valid_sender(None) == "+491"


def test_sender_invalid_configured_raises_instead_of_switching() -> None:
    with pytest.raises(SignalNotRegistered):
        _backend(["+491"]).get_valid_sender("+499")


def test_sender_without_registered_account_raises() -> None:
    with pytest.raises(SignalNotRegistered):
        _backend([]).get_valid_sender("+491")
    with pytest.raises(SignalNotRegistered):
        _backend([]).get_valid_sender(None)


def test_backend_ignores_sender_env(monkeypatch) -> None:
    monkeypatch.setenv("SIGNAL_SENDER_NUMBER", "+491701111111")
    backend = SignalCliBackend(cli_path="/bin/true", data_dir="/tmp/x")  # noqa: S108 - never used
    assert backend._sender is None  # noqa: SLF001 - verifies env is not consulted


def test_app_reads_only_allowlisted_env_names() -> None:
    pattern = re.compile(r"""["']((?:UPMON|JUSTUP|SIGNAL|POP|CACHE|DATA|SECRET|LOG|TESTING|PYTEST)[A-Z0-9_]*)["']""")
    found: dict[str, set[str]] = {}
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "environ" not in line and "getenv" not in line and "_ENV" not in line:
                continue
            for name in pattern.findall(line):
                found.setdefault(name, set()).add(path.name)
    unexpected = {n: f for n, f in found.items() if n not in ALLOWED_ENV_NAMES}
    assert not unexpected, f"ENV names outside the bootstrap allowlist: {unexpected}"
