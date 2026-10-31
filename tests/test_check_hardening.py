#!/usr/bin/env python3
#
# tests/test_check_hardening.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Host contract (FR-06) and subprocess argument hygiene (NFR-07)."""

from __future__ import annotations

import asyncio
import subprocess
import unittest
from unittest import mock

from app.models.targets import TargetCreate, TargetUpdate, validate_host
from app.monitor import checks
from tests import _support  # noqa: F401 - imported for its sys.path side effect


class HostValidationTests(unittest.TestCase):
    def test_accepts_hostnames_and_ip_literals(self):
        for value in ("example.com", "a-b.example.org.", "localhost", "10.0.0.1", "2001:db8::1"):
            with self.subTest(value=value):
                self.assertEqual(validate_host(value), value)

    def test_rejects_free_text_and_option_like_hosts(self):
        for value in ("-oProxyCommand=x", "exa mple.com", "http://example.com", "a..b", "-a.example.com", "x;y"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_host(value)

    def test_models_apply_the_contract(self):
        with self.assertRaises(ValueError):
            TargetCreate(name="t", host="not a host")
        with self.assertRaises(ValueError):
            TargetUpdate(ping_host="-c")
        self.assertEqual(TargetCreate(name="t", host="example.com").host, "example.com")


class PingInvocationTests(unittest.TestCase):
    def test_ping_ends_options_before_host_and_has_hard_timeout(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="time=1.5 ms", stderr="")

        with mock.patch("subprocess.run", fake_run):
            result = asyncio.run(checks.ping("192.0.2.1", timeout_seconds=3, skip_ssrf_check=True))
        self.assertTrue(result.ok)
        self.assertEqual(seen["cmd"][-2:], ["--", "192.0.2.1"])
        self.assertIsNotNone(seen["kwargs"].get("timeout"))

    def test_hard_timeout_is_reported_as_timeout(self):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with mock.patch("subprocess.run", fake_run):
            result = asyncio.run(checks.ping("192.0.2.1", timeout_seconds=1, skip_ssrf_check=True))
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "timeout")


if __name__ == "__main__":
    unittest.main()
