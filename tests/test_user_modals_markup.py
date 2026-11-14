#!/usr/bin/env python3
#
# tests/test_user_modals_markup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Markup contract of the Create/Edit User dialogs (notification channels) and the delete/recipient hints."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests import _support  # noqa: F401 - imported for its sys.path side effect

MODALS = (Path(__file__).resolve().parent.parent / "app" / "templates" / "settings" / "modals.html").read_text(
    encoding="utf-8"
)
USERS_JS = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "settings.users.js").read_text(
    encoding="utf-8"
)
FIELDS = ("signal-enabled", "phone", "phone-container", "email-enabled", "email", "email-container")
SETTINGS = ("recipient-enabled", "all-targets")


class _Dialog(HTMLParser):
    """Collects the elements of one dialog (by root id): id -> (tag, attrs), labels and ids in document order."""

    def __init__(self, dialog_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.dialog_id, self.depth, self.inside = dialog_id, 0, False
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.labels: dict[str, str] = {}  # for -> text
        self.attrs_seen: list[str] = []
        self._label_for: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if not self.inside and a.get("id") == self.dialog_id:
            self.inside = True
        if not self.inside:
            return
        if tag == "div":
            self.depth += 1
        self.attrs_seen.extend(a)
        if a.get("id"):
            self.elements[a["id"]] = (tag, a)
        if tag == "label" and a.get("for"):
            self._label_for = a["for"]
            self.labels[a["for"]] = ""

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if tag == "label":
            self._label_for = None
        if tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.inside = False

    def handle_data(self, data):
        if self.inside and self._label_for:
            self.labels[self._label_for] += data


def _dialog(dialog_id: str) -> _Dialog:
    parser = _Dialog(dialog_id)
    parser.feed(re.sub(r"\{#.*?#\}", "", MODALS, flags=re.DOTALL))
    return parser


@pytest.mark.parametrize(("dialog", "prefix"), [("createUserModal", "new-user"), ("editUserModal", "edit-user")])
def test_user_dialog_has_the_channel_and_recipient_controls(dialog: str, prefix: str):
    d = _dialog(dialog)
    for name in (*FIELDS, *SETTINGS):
        assert f"{prefix}-{name}" in d.elements, f"{dialog}: {prefix}-{name} missing"
    for name in ("signal-enabled", "email-enabled", *SETTINGS):
        tag, attrs = d.elements[f"{prefix}-{name}"]
        assert (tag, attrs.get("type"), attrs.get("role")) == ("input", "checkbox", "switch")
    # Every switch is labelled; a channel switch controls exactly its field container.
    for name in ("signal-enabled", "email-enabled", *SETTINGS, "phone", "email"):
        assert d.labels.get(f"{prefix}-{name}", "").strip(), f"{prefix}-{name} has no label"
    assert d.elements[f"{prefix}-signal-enabled"][1]["aria-controls"] == f"{prefix}-phone-container"
    assert d.elements[f"{prefix}-email-enabled"][1]["aria-controls"] == f"{prefix}-email-container"
    assert d.elements[f"{prefix}-phone"][1]["type"] == "tel" and d.elements[f"{prefix}-email"][1]["type"] == "email"
    for field in ("phone", "email"):
        help_id = d.elements[f"{prefix}-{field}"][1]["aria-describedby"]
        assert help_id in d.elements, f"{prefix}-{field}: aria-describedby target missing"
    # Same visible labels as the recipient dialog.
    assert d.labels[f"{prefix}-recipient-enabled"].strip() == "Recipient Enabled"
    assert d.labels[f"{prefix}-all-targets"].strip() == "Enable for all Targets"
    # Conventions: no inline handlers, no native dialogs, no test buttons in the user dialogs.
    assert not [name for name in d.attrs_seen if name.startswith("on")]
    assert not [i for i in d.elements if "test" in i]


def test_new_user_starts_with_recipient_enabled_checked_and_the_edit_dialog_does_not():
    assert "checked" in _dialog("createUserModal").elements["new-user-recipient-enabled"][1]
    assert "checked" not in _dialog("editUserModal").elements["edit-user-recipient-enabled"][1]


def test_dialog_ids_are_unique_across_the_settings_dialogs():
    ids = re.findall(r'\bid="([^"]+)"', re.sub(r"\{#.*?#\}", "", MODALS, flags=re.DOTALL))
    assert len(ids) == len(set(ids))


def test_delete_user_and_edit_recipient_dialogs_explain_the_link():
    delete = _dialog("deleteUserModal")
    assert "deleteUserModalDesc" in delete.elements
    assert "notification recipient" in MODALS.split('id="deleteUserModal"')[1].split("</div>\n\t</div>\n</div>")[0]
    tag, attrs = _dialog("editRecipientModal").elements["edit-recipient-user-note"]
    assert tag == "p" and "hidden" in attrs


def test_user_scripts_send_the_recipient_in_the_same_request_and_never_call_the_recipient_api():
    assert "recipient: recipientConfig('new-user')" in USERS_JS
    assert "recipient," in USERS_JS and "recipientConfig('edit-user')" in USERS_JS
    assert "/recipients" not in USERS_JS
    assert "confirm(" not in re.sub(
        r"confirmAction|confirmDeleteUser|confirmRevokeSessions|confirmViaModal", "", USERS_JS
    )
