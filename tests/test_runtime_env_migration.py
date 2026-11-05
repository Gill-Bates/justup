#!/usr/bin/env python3
#
# tests/test_runtime_env_migration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""ENV reduction: UI-managed settings, runtime effect, ENV allowlist."""

from __future__ import annotations

import ast
import sqlite3
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
from app.utils.constants import QUALITY_MAX_TARGETS, SettingKeys
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
    "TRUSTED_PROXIES",
    "JUSTUP_DATA_DIR",
    "DATA_DIR",
    "CACHE_DIR",
    "JUSTUP_HOST",
    "JUSTUP_PORT",
    "JUSTUP_BACKUP_HMAC_SECRET",
    # Read only to report that HTTP monitors may leave through a proxy (Settings > Network, public egress).
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "SIGNAL_CLI_PATH",
    "SIGNAL_DATA_DIR",
    "TESTING",
    "PYTEST_CURRENT_TEST",
    # Local Nmap worker: socket, status file and parent pid are set by the entrypoint or the parent
    # process, never by the UI; PATH, LANG and NMAPDIR are forwarded into its scrubbed environment.
    "JUSTUP_NMAP_SOCKET",
    "JUSTUP_NMAP_STATUS",
    "JUSTUP_NMAP_PARENT_PID",
    "NMAPDIR",
    "PATH",
    "LANG",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(crypto, "_primary_key", None)
    crypto.reset_fernet_cache()
    runtime_settings.clear_runtime_cache()


def _environment_accesses(source: str) -> tuple[set[str], list[int]]:
    tree = ast.parse(source)
    os_aliases = {"os"}
    getenv_aliases = {"getenv"}
    environ_aliases = {"environ"}
    constants = {}
    for node in tree.body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            constants.update({target.id: value.value for target in targets if isinstance(target, ast.Name)})
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "getenv":
                    getenv_aliases.add(alias.asname or alias.name)
                elif alias.name == "environ":
                    environ_aliases.add(alias.asname or alias.name)

    def is_os_attribute(node: ast.expr, name: str) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == name
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        )

    def is_environ(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id in environ_aliases) or is_os_attribute(node, "environ")

    names: set[str] = set()
    dynamic_lines: list[int] = []
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Call):
            function = node.func
            if (
                (isinstance(function, ast.Name) and function.id in getenv_aliases)
                or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "getenv"
                    and isinstance(function.value, ast.Name)
                    and function.value.id in os_aliases
                )
                or (isinstance(function, ast.Attribute) and function.attr == "get" and is_environ(function.value))
            ):
                key = node.args[0] if node.args else None
        elif isinstance(node, ast.Subscript) and is_environ(node.value):
            key = node.slice
        if key is not None:
            literal = (
                key.value
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
                else constants.get(key.id)
                if isinstance(key, ast.Name)
                else None
            )
            if literal is not None:
                names.add(literal)
            else:
                dynamic_lines.append(node.lineno)
    return names, dynamic_lines


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
    assert "online_geo_enabled" not in fields
    # The POP refresh interval/initial-delay settings were removed (refresh is self-managing);
    # they must no longer surface as runtime fields.
    assert "pop_refresh_interval_seconds" not in fields
    assert "pop_refresh_initial_delay_seconds" not in fields
    assert Settings(**fields).ip_api_key_set is False


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"rate_limit_auth": "fast"}, id="invalid-rate-limit"),
        pytest.param({"rate_limit_auth": "0/minute"}, id="zero-rate-limit"),
        pytest.param({"quality_min_score": 101}, id="quality-score-too-high"),
        pytest.param({"quality_rtt_threshold_ms": 0}, id="zero-rtt-threshold"),
        pytest.param({"provider_lookup_cache_ttl_seconds": 5}, id="cache-ttl-too-short"),
        pytest.param({"pop_refresh_interval_seconds": 10}, id="removed-pop-refresh-interval"),
        pytest.param({"pop_refresh_initial_delay_seconds": 3600}, id="removed-pop-refresh-delay"),
        pytest.param({"allow_insecure_ip_api_http": True}, id="removed-insecure-ip-api-http"),
    ],
)
def test_update_model_rejects_invalid_or_removed_settings(payload: dict) -> None:
    with pytest.raises(ValidationError):
        SettingsUpdate(**payload)


def test_quality_targets_validation() -> None:
    assert validate_quality_targets("") == ""
    assert validate_quality_targets("1.1.1.1, 8.8.8.8 ,https://example.org") == "1.1.1.1,8.8.8.8,https://example.org"
    with pytest.raises(HTTPException):
        validate_quality_targets("1.1.1.1,8.8.8.8")
    with pytest.raises(HTTPException):
        validate_quality_targets("1.1.1.1,8.8.8.8,bad host!")

    assert validate_quality_targets("1.1.1.1,2606:4700:4700::1111,https://example.org:8443/x")
    for unreachable in ("host:80", "a/b", "abc:80", "example.org/path"):
        with pytest.raises(HTTPException):
            validate_quality_targets(f"1.1.1.1,8.8.8.8,{unreachable}")


def test_quality_targets_are_limited_in_count_and_deduplicated() -> None:
    limit = QUALITY_MAX_TARGETS
    at_limit = ",".join(f"host{i}.example" for i in range(limit))
    assert validate_quality_targets(at_limit) == at_limit
    with pytest.raises(HTTPException) as exc:
        validate_quality_targets(f"{at_limit},host{limit}.example")
    assert exc.value.status_code == 422 and str(limit) in exc.value.detail
    # Duplicates neither count towards the maximum nor satisfy the minimum.
    assert validate_quality_targets(f"{at_limit},host0.example") == at_limit
    with pytest.raises(HTTPException):
        validate_quality_targets("1.1.1.1,1.1.1.1,1.1.1.1")


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
    found: dict[str, set[str]] = {}
    dynamic: dict[str, list[int]] = {}
    for path in APP_DIR.rglob("*.py"):
        names, dynamic_lines = _environment_accesses(path.read_text(encoding="utf-8"))
        for name in names:
            found.setdefault(name, set()).add(path.name)
        if dynamic_lines:
            dynamic[path.name] = dynamic_lines
    unexpected = {n: f for n, f in found.items() if n not in ALLOWED_ENV_NAMES}
    assert not dynamic, f"dynamic ENV keys require explicit review: {dynamic}"
    assert not unexpected, f"ENV names outside the bootstrap allowlist: {unexpected}"


def test_env_scanner_finds_single_line_multiline_and_subscript_accesses() -> None:
    source = """
import os as host_os
from os import environ as environment, getenv as env_get
one = host_os.getenv("UNLISTED_ONE")
two = env_get(
    "UNLISTED_TWO",
)
three = host_os.environ["UNLISTED_THREE"]
four = environment.get("UNLISTED_FOUR")
five = host_os.getenv(dynamic_name)
"""
    names, dynamic_lines = _environment_accesses(source)
    assert names == {"UNLISTED_ONE", "UNLISTED_TWO", "UNLISTED_THREE", "UNLISTED_FOUR"}
    assert dynamic_lines == [10]


# --- Online geolocation opt-out is fail-closed ---------------------------------------------------------


def _unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_key, _path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime_settings, "_read", broken)


def test_online_fallback_default_applies_only_when_the_store_was_read(conn) -> None:
    key = SettingKeys.ONLINE_GEO_FALLBACK_ENABLED
    assert runtime_settings.get_runtime_bool_fail_closed(key, True) is True  # fresh install: readable, no value
    sqlite_db.set_setting(conn, key, False)
    runtime_settings.clear_runtime_cache()
    assert runtime_settings.get_runtime_bool_fail_closed(key, True) is False


def test_opt_out_survives_an_unreadable_store_and_no_provider_call_is_made(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import traceroute_geo

    key = SettingKeys.ONLINE_GEO_FALLBACK_ENABLED
    sqlite_db.set_setting(conn, key, False)
    runtime_settings.clear_runtime_cache()  # empty runtime cache, no last known value
    _unreadable(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(traceroute_geo, "lookup_online_sources", lambda ip, **_k: calls.append(ip) or ((), {}))
    monkeypatch.setattr(traceroute_geo, "geolocate_ip", lambda _ip: None)
    traceroute_geo.geolocate_with_hostname_hint("8.8.8.8")
    assert calls == []  # unknown consent: nothing leaves the host
    assert runtime_settings.get_runtime_bool_fail_closed(key, True) is False


def test_missing_database_without_known_value_is_off(data_dir) -> None:
    assert not (data_dir / "sqlite" / "justup.db").exists()
    assert runtime_settings.get_runtime_bool_fail_closed(SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, True) is False


def test_last_known_value_is_kept_across_cache_expiry_and_reread_when_the_store_returns(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = SettingKeys.ONLINE_GEO_FALLBACK_ENABLED
    sqlite_db.set_setting(conn, key, True)
    runtime_settings.clear_runtime_cache()
    assert runtime_settings.get_runtime_bool_fail_closed(key, False) is True
    runtime_settings._cache.clear()  # noqa: SLF001 - the 5 s cache entry expired
    with monkeypatch.context() as patch:
        _unreadable(patch)
        assert runtime_settings.get_runtime_bool_fail_closed(key, False) is True  # last known value
    sqlite_db.set_setting(conn, key, False)  # store is back with a new value (write drops the cache)
    assert runtime_settings.get_runtime_bool_fail_closed(key, True) is False


def test_a_settings_write_drops_the_last_known_value(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    key = SettingKeys.ONLINE_GEO_FALLBACK_ENABLED
    sqlite_db.set_setting(conn, key, True)
    runtime_settings.clear_runtime_cache()
    assert runtime_settings.get_runtime_bool_fail_closed(key, False) is True
    sqlite_db.set_setting(conn, key, False)  # a real write must drop the cache and the last known value
    _unreadable(monkeypatch)
    assert runtime_settings.get_runtime_bool_fail_closed(key, True) is False  # stale True must not be used


def test_provider_lookup_switch_is_fail_closed_and_sends_no_ip_when_unreadable(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import provider_lookup

    key = SettingKeys.PROVIDER_LOOKUP_ENABLED
    assert runtime_settings.get_runtime_bool_fail_closed(key, True) is True  # readable, unset: documented default
    sqlite_db.set_setting(conn, key, False)
    assert runtime_settings.get_runtime_bool_fail_closed(key, True) is False  # explicit opt-out
    runtime_settings.clear_runtime_cache()
    _unreadable(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(provider_lookup, "resolve_target_ip", lambda _host: "8.8.8.8")
    monkeypatch.setattr(provider_lookup, "lookup_public_ip", lambda ip, **_k: calls.append(ip))
    assert provider_lookup.lookup_provider_info("example.com")["ip"] == "8.8.8.8"
    assert calls == []  # unknown consent: nothing leaves the host


def test_last_known_value_is_scoped_to_the_current_database(
    conn, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = SettingKeys.ONLINE_GEO_FALLBACK_ENABLED
    sqlite_db.set_setting(conn, key, True)
    runtime_settings.clear_runtime_cache()
    assert runtime_settings.get_runtime_bool_fail_closed(key, False) is True  # A allows
    runtime_settings._cache.clear()  # noqa: SLF001 - the 5 s cache entry expired

    with monkeypatch.context() as patch:
        _unreadable(patch)
        assert runtime_settings.get_runtime_bool_fail_closed(key, False) is True  # A unreadable: last known for A

        patch.setenv("JUSTUP_DATA_DIR", str(tmp_path_factory.mktemp("other")))  # switch to database B
        assert runtime_settings.get_runtime_bool_fail_closed(key, True) is False  # B unreadable: A's value unused


@pytest.mark.usefixtures("data_dir")
def test_unreadable_database_on_first_read_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_settings.clear_runtime_cache()
    _unreadable(monkeypatch)
    assert runtime_settings.get_runtime_bool_fail_closed(SettingKeys.ONLINE_GEO_FALLBACK_ENABLED, True) is False
