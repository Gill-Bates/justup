#!/usr/bin/env python3
#
# tests/test_signal_delivery_classification.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Signal failure classification: sender, recipient and identity problems are told apart."""

from __future__ import annotations

import pytest

from app.services.notifications.delivery_policy import FailureKind, classify_failure
from app.services.notifications.signal_cli_backend import SignalCliBackend
from app.services.notifications.signal_errors import (
    SignalNotRegistered,
    SignalRecipientInvalid,
)


def test_invalid_recipient_is_permanent_missing_sender_is_limited():
    assert classify_failure(SignalRecipientInvalid("bad")).kind is FailureKind.PERMANENT
    assert classify_failure(SignalNotRegistered("no sender")).kind is FailureKind.LIMITED


@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        ("IDENTITY_FAILURE", FailureKind.LIMITED),
        ("NETWORK_FAILURE", FailureKind.TRANSIENT),
        ("SOMETHING_ELSE", FailureKind.TRANSIENT),
        ("UNREGISTERED_FAILURE", FailureKind.PERMANENT),
    ],
)
def test_send_outcomes_are_classified_differently(tmp_path, monkeypatch, outcome, kind):
    backend = SignalCliBackend(cli_path="signal-cli", data_dir=str(tmp_path), sender_number="+491700000001")
    monkeypatch.setattr(backend, "get_valid_sender", lambda *_a, **_k: "+491700000001")
    monkeypatch.setattr(backend, "_run_signal_rpc", lambda *_a, **_k: {"result": {"results": [{"type": outcome}]}})
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(Exception) as info:  # any Signal error, classified below
        backend.send_message("+491700000002", "hi")
    assert classify_failure(info.value).kind is kind
