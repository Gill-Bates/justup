# ruff: noqa: SLF001
#
# tests/test_signal_lookup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Recipient number lookup: retry on a false negative, failures are not "not registered", and logging."""

from __future__ import annotations

import logging
import sys

import pytest

from app.api import recipients
from app.services.notifications.signal_errors import SignalUnavailable
from tests import _support  # noqa: F401 - imported for its sys.path side effect

PHONE = "+4915112345678"


class _Backend:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def list_accounts(self):
        return ["+4900000000"]

    def check_user_registered(self, phone, sender=None):  # noqa: ARG002
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(recipients, "_SIGNAL_LOOKUP_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(recipients, "_SIGNAL_REGISTRATION_CACHE", {})
    yield


def _use(monkeypatch, backend):
    module = sys.modules["app.services.notifications.signal_backend"]
    monkeypatch.setattr(module, "get_signal_backend", lambda: backend)


def test_false_negative_on_first_lookup_is_retried(monkeypatch, caplog):
    backend = _Backend([False, True])
    _use(monkeypatch, backend)
    with caplog.at_level(logging.INFO, logger="app.api.recipients"):
        assert recipients._check_signal_registration(PHONE) is None
    assert backend.calls == 2
    assert any("registered" in r.getMessage() and PHONE not in r.getMessage() for r in caplog.records)


def test_definite_negative_is_reported_after_all_lookups_agree(monkeypatch):
    backend = _Backend([False, False, False])
    _use(monkeypatch, backend)
    warning = recipients._check_signal_registration(PHONE)
    assert warning is not None and "is not registered with Signal" in warning
    assert backend.calls == 3


def test_transient_failure_is_not_reported_as_not_registered(monkeypatch, caplog):
    raw_error = "signal-cli timed out " + "x" * 150
    short_error = raw_error[: recipients._SIGNAL_ERROR_DETAIL_MAX]
    backend = _Backend([SignalUnavailable(raw_error)] * 3)
    _use(monkeypatch, backend)
    with caplog.at_level(logging.WARNING, logger="app.api.recipients"):
        warning = recipients._check_signal_registration(PHONE)
    assert warning is not None
    assert "not registered with Signal" not in warning
    assert "Could not verify" in warning and short_error in warning and raw_error not in warning
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(logged) == 3
    assert all(message.endswith(short_error) and PHONE not in message for message in logged)
    assert raw_error not in caplog.text


def test_transient_failure_then_answer_succeeds(monkeypatch):
    backend = _Backend([SignalUnavailable("boom"), True])
    _use(monkeypatch, backend)
    assert recipients._check_signal_registration(PHONE) is None
    assert recipients._SIGNAL_REGISTRATION_CACHE[PHONE][0] is True
