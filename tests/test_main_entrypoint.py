#!/usr/bin/env python3
#
# tests/test_main_entrypoint.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""app.__main__.main(): a SIGINT re-raised by uvicorn after shutdown must end quietly."""

from __future__ import annotations

import sys
import types

from app import __main__ as entry


def test_main_swallows_keyboard_interrupt_from_server_run(monkeypatch):
    class _Server:
        def __init__(self, config):
            self.config = config

        def run(self):
            raise KeyboardInterrupt

    fake_uvicorn = types.SimpleNamespace(Config=lambda *a, **kw: types.SimpleNamespace(args=a, kwargs=kw))
    cfg = types.SimpleNamespace(host="127.0.0.1", port=0, log_level="INFO")
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr("app.main.create_app", object)
    monkeypatch.setattr("app.utils.config.load_config", lambda: cfg)
    monkeypatch.setattr(entry, "make_server_class", lambda: _Server)

    assert entry.main() is None
