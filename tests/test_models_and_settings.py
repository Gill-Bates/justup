#!/usr/bin/env python3
#
# tests/test_models_and_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: S106
#!/usr/bin/env python3
#
# tests/test_models_and_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Input normalisation of user/group models and the settings API round-trip."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import settings as settings_api
from app.api.auth import require_admin
from app.db import sqlite as sqlite_db
from app.models.groups import Group, GroupCreate, GroupUpdate
from app.models.users import LoginRequest, UserCreate, UserUpdate
from app.utils.deps import get_conn
from tests import _support  # noqa: F401 - imported for its sys.path side effect


@pytest.mark.parametrize("model", [UserCreate, UserUpdate])
@pytest.mark.parametrize("blank", ["   ", "\t\n", "  "])
def test_blank_username_is_rejected(model, blank):
    extra = {"password": "longenough1"} if model is UserCreate else {}
    with pytest.raises(ValidationError):
        model(username=blank, **extra)


def test_username_is_trimmed_and_lowercased():
    assert UserCreate(username="  AlIcE ", password="longenough1").username == "alice"
    assert UserUpdate(username=" BOB").username == "bob"
    assert LoginRequest(username=" X ", password="p").username == "x"


def test_group_name_is_trimmed_and_blank_rejected():
    assert GroupCreate(name="  core  ").name == "core"
    assert GroupUpdate(name=" a ").name == "a"
    assert GroupUpdate().name is None
    for bad in ("   ", " "):
        with pytest.raises(ValidationError):
            GroupCreate(name=bad)
        with pytest.raises(ValidationError):
            GroupUpdate(name=bad)


@pytest.fixture
def client(tmp_path: Path):
    conn = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(conn)
    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "is_admin": True}
    yield TestClient(app)
    sqlite_db.close_connection(conn)


def test_settings_default_and_patch_roundtrip(client):
    body = client.get("/settings").json()
    assert body["geoip_auto_update"] is True
    r = client.patch("/settings", json={"geoip_auto_update": False})
    assert r.status_code == 200
    assert r.json()["geoip_auto_update"] is False
    again = client.get("/settings").json()
    assert again["geoip_auto_update"] is False


def test_downsample_switch_is_gone_and_stale_values_are_cleaned(client, tmp_path):
    """Rollups always run: the setting is not part of the API and a stored legacy value is dropped."""
    assert "downsample_enabled" not in client.get("/settings").json()
    assert client.patch("/settings", json={"downsample_enabled": False}).status_code == 422
    conn = sqlite_db.connect(tmp_path / "legacy" / "justup.db")
    try:
        sqlite_db.init_schema(conn)
        sqlite_db.set_setting(conn, "downsample_enabled", False)
        sqlite_db.init_schema(conn)  # the next start sweeps retired keys
        assert sqlite_db.get_setting(conn, "downsample_enabled", None) is None
    finally:
        sqlite_db.close_connection(conn)


def test_session_max_days_default_save_and_validation(client):
    assert client.get("/settings").json()["session_max_days"] == 1
    for ok in (1, 7, 3):
        r = client.patch("/settings", json={"session_max_days": ok})
        assert r.status_code == 200 and r.json()["session_max_days"] == ok
    assert client.get("/settings").json()["session_max_days"] == 3
    for bad in (0, 8, -1, "abc", 1.5, None):
        assert client.patch("/settings", json={"session_max_days": bad}).status_code == 422
    assert client.get("/settings").json()["session_max_days"] == 3


def test_session_max_days_requires_admin(tmp_path: Path):
    from fastapi import HTTPException

    conn = sqlite_db.connect(tmp_path / "db" / "justup.db")
    sqlite_db.init_schema(conn)
    app = FastAPI()
    app.include_router(settings_api.router)
    app.dependency_overrides[get_conn] = lambda: conn

    def _deny():
        raise HTTPException(status_code=403, detail="Admin required")

    app.dependency_overrides[require_admin] = _deny
    c = TestClient(app)
    assert c.patch("/settings", json={"session_max_days": 3}).status_code == 403
    assert sqlite_db.get_session_max_days(conn) == 1
    sqlite_db.close_connection(conn)


def test_settings_template_has_labelled_session_slider():
    html = (Path(__file__).resolve().parent.parent / "app/templates/settings/users.html").read_text(encoding="utf-8")
    assert 'for="session_max_days"' in html
    assert 'type="range" class="form-range" name="session_max_days"' in html
    assert "aria-valuetext=" in html and 'aria-describedby="session_max_days_hint"' in html


def test_general_settings_have_unique_form_ids_and_keep_all_fields():
    root = Path(__file__).resolve().parent.parent / "app/templates"
    fragments = [
        (root / f"settings/{n}.html").read_text(encoding="utf-8") for n in ("general", "network", "storage", "users")
    ]
    html = (root / "settings.html").read_text(encoding="utf-8") + "".join(fragments)

    class IdCollector(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []

        def handle_starttag(self, _tag, attrs):
            self.ids.extend(value for name, value in attrs if name == "id")

    parser = IdCollector()
    parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    assert 'id="settings-form"' in html
    assert 'id="general-settings-form"' in html

    expected_fields = {
        "use_utc_dashboard",
        "pdf_page_size",
        "geoip_auto_update",
        "purge_enabled",
        "metrics_timeout_minutes",
        "auth_disabled",
        "rate_limit_auth",
        "session_max_days",
        "quality_targets",
        "quality_min_score",
        "quality_rtt_threshold_ms",
        "provider_lookup_enabled",
        "provider_lookup_cache_ttl_seconds",
        "ip_api_key",
        "online_geo_enabled",
    }
    assert all(f'name="{field}"' in html for field in expected_fields)
    # The removed POP refresh settings must not reappear in any settings template.
    assert 'name="pop_refresh_interval_seconds"' not in html
    assert 'name="pop_refresh_initial_delay_seconds"' not in html
    # Downsampling is automatic: no switch and no help text may come back.
    assert 'name="downsample_enabled"' not in html
    assert "Metric Downsampling" not in html
    assert "System Status" not in html

    class ContainerParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, _attrs):
            if tag in {"div", "form", "section"}:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in {"div", "form", "section"}:
                assert self.stack and self.stack.pop() == tag

    for fragment in fragments:
        structure = ContainerParser()
        structure.feed(fragment)
        assert structure.stack == []

    # Every setting field must live in a form that auto-saves (delegated or data-settings-autosave).
    class FormOwnership(HTMLParser):
        def __init__(self):
            super().__init__()
            self.form_stack = []
            self.orphans = []

        def handle_starttag(self, tag, attrs):
            attr = dict(attrs)
            if tag == "form":
                autosave = "data-settings-autosave" in attr or attr.get("id") == "general-settings-form"
                self.form_stack.append(autosave)
            elif tag in {"input", "select"} and attr.get("name") in expected_fields:
                if not (self.form_stack and self.form_stack[-1]):
                    self.orphans.append(attr["name"])

        def handle_endtag(self, tag):
            if tag == "form":
                self.form_stack.pop()

    for fragment in fragments:
        ownership = FormOwnership()
        ownership.feed(fragment)
        assert ownership.orphans == []


def test_mobile_tab_selector_mirrors_desktop_tabs():
    html = (Path(__file__).resolve().parent.parent / "app/templates/settings.html").read_text(encoding="utf-8")
    desktop = re.findall(r'id="settingsTabs".*?</ul>', html, re.DOTALL)[0]
    targets = re.findall(r'data-bs-target="(#[a-z]+-pane)"', desktop)
    options = re.findall(r'<option value="(#[a-z]+-pane)"', html)
    assert targets and targets == options
    assert "settingsMobileNav" in html


def test_ip_api_key_clear_action_is_explicit_and_untouched_field_stays_empty():
    html = (Path(__file__).resolve().parent.parent / "app/templates/settings/network.html").read_text(encoding="utf-8")
    assert "Remove stored key" in html
    assert "Stored — enter a new key to replace" in html


def test_about_page_hides_third_party_design_and_uses_theme_text_color():
    html = (Path(__file__).resolve().parent.parent / "app/templates/about.html").read_text(encoding="utf-8")
    assert "Third-party design" not in html
    assert ".about-meta-value" in html
    assert "color: var(--bs-body-color);" in html
    assert ".dependencies-table code" in html


def test_auth_toggle_bindings_are_idempotent_across_htmx_swaps():
    source = (Path(__file__).resolve().parent.parent / "app/static/js/settings.storage.js").read_text(encoding="utf-8")
    assert "checkbox.dataset.authToggleBound === '1'" in source
    assert "form.dataset.authToggleBound !== '1'" in source
    assert "modal.dataset.authToggleHiddenBound !== '1'" in source
    assert "const currentCheckbox = DOM.authDisabled();" in source


def test_group_response_model_accepts_legacy_blank_name() -> None:
    now = datetime.now(UTC)
    legacy = Group(id=1, name="   ", description=None, created_at=now, updated_at=now)
    assert legacy.name == "   "
    with pytest.raises(ValueError):
        GroupCreate(name="   ")


@pytest.mark.parametrize("field", ["interval_seconds", "max_retries", "retention_days"])
def test_monitoring_limits_match_target_models(field: str) -> None:
    from app.models.targets import TargetCreate, TargetUpdate
    from app.utils.constants import MONITORING_LIMITS

    low, high = MONITORING_LIMITS[field]["min"], MONITORING_LIMITS[field]["max"]
    for model, extra in ((TargetCreate, {"name": "t", "host": "example.com"}), (TargetUpdate, {})):
        assert getattr(model(**extra, **{field: low}), field) == low
        assert getattr(model(**extra, **{field: high}), field) == high
        for bad in (low - 1, high + 1):
            with pytest.raises(ValidationError):
                model(**extra, **{field: bad})


def test_target_forms_take_monitoring_limits_from_template_context() -> None:
    templates = Path(__file__).resolve().parents[1] / "app" / "templates"
    for name in ("target_form.html", "target_wizard.html"):
        source = (templates / name).read_text(encoding="utf-8")
        for field in ("interval_seconds", "max_retries", "retention_days"):
            assert f"limits.{field}.min" in source
            assert f"limits.{field}.max" in source
        assert 'max="86400"' not in source
        assert 'max="3650"' not in source
