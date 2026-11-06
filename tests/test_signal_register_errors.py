#!/usr/bin/env python3
#
# tests/test_signal_register_errors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Signal registration errors reach the UI as stable codes, never as text to be matched."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import signal as signal_api
from app.services.notifications.signal_cli_backend import SignalCliBackend
from app.services.notifications.signal_errors import SignalSendFailed
from tests import _support  # noqa: F401 - imported for its sys.path side effect


def _register_with(monkeypatch: pytest.MonkeyPatch, error: Exception) -> HTTPException:
    class _Backend:
        def register(self, **_kwargs):
            raise error

    monkeypatch.setattr(signal_api, "get_signal_backend", _Backend)
    body = signal_api.RegisterRequest(number="+4915112345678")
    with pytest.raises(HTTPException) as caught:
        signal_api.signal_register(body, _admin=None)
    return caught.value


@pytest.mark.parametrize(
    ("code", "text"),
    [("captcha_required", "wording changed"), ("already_registered", "another wording")],
)
def test_typed_failures_carry_their_code_independent_of_text(monkeypatch, code, text):
    exc = _register_with(monkeypatch, SignalSendFailed(text, code=code))
    assert exc.status_code == 400
    assert isinstance(exc.detail, dict)
    assert exc.detail["code"] == code
    assert exc.detail["message"]


def test_untyped_failure_stays_a_plain_message_even_if_it_mentions_captcha(monkeypatch):
    exc = _register_with(monkeypatch, SignalSendFailed("captcha already registered"))
    assert exc.status_code == 400
    assert exc.detail == "Signal registration failed; check the number and try again"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ({"code": -6, "message": "x"}, "captcha_required"),
        ({"code": -1, "message": "Captcha required"}, "captcha_required"),
        ({"code": -1, "message": "Number is already registered"}, "already_registered"),
        ({"code": -1, "message": "boom"}, None),
    ],
)
def test_cli_backend_classifies_registration_errors(error, code):
    backend = SignalCliBackend(cli_path="signal-cli", data_dir="/nonexistent")
    with pytest.raises(SignalSendFailed) as caught:
        backend._require_rpc_success({"error": error}, "registration", "+4915112345678")  # noqa: SLF001
    assert caught.value.code == code
