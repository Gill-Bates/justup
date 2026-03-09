#!/usr/bin/env python3
#
# tracking/test_telemetry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""
Manual telemetry test script.

Sends a test payload to the telemetry endpoint using the same encryption
as the main application.
"""

import sys
import os

# Add app to path
sys.path.insert(0, '/opt/justup-dev')

from app._cython.telemetry import _encrypt_payload, _TELEMETRY_ENDPOINT
import httpx

# Test payload
test_data = {
    "id": "00000000-0000-0000-0000-000000000001",  # Test UUID
    "ts": "2026-02-11T12:00:00Z",
    "v": "1.1.11-test",
    "ip": "127.0.0.1",
    "sys": {
        "os": "Linux",
        "osv": "6.1.0",
        "py": "3.13.0",
        "arch": "x86_64",
        "cpu": 8,
        "mem": 16.0,
        "hh": 12345678,
    },
    "st": {
        "t": 5,  # 5 targets
        "u": 2,  # 2 users
        "nc": ["email", "telegram"],  # notification channels
    },
}

print("🔐 Encrypting test payload...")
payload_b64, signature = _encrypt_payload(test_data)

print(f"📦 Payload size: {len(payload_b64)} bytes")
print(f"🔑 Signature: {signature[:16]}...")
print(f"🎯 Endpoint: {_TELEMETRY_ENDPOINT}")
print()

# Generate curl command
curl_cmd = f"""curl -X POST '{_TELEMETRY_ENDPOINT}' \\
  -H 'Content-Type: application/octet-stream' \\
  -H 'X-Sig: {signature}' \\
  -H 'X-Ver: {test_data["v"]}' \\
  --data '{payload_b64}' \\
  -v"""

print("📋 Copy this curl command:")
print("=" * 80)
print(curl_cmd)
print("=" * 80)
print()

# Also send it directly with httpx
print("🚀 Sending test payload directly...")
try:
    resp = httpx.post(
        _TELEMETRY_ENDPOINT,
        content=payload_b64,
        timeout=10.0,
        follow_redirects=True,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Sig": signature,
            "X-Ver": test_data["v"],
        },
    )
    
    print(f"✅ Response status: {resp.status_code}")
    print(f"📄 Response body: {resp.text[:200]}")
    
    if resp.status_code in (200, 201, 202, 204):
        print("✨ Success! Telemetry endpoint is working.")
    else:
        print(f"⚠️  Unexpected status code: {resp.status_code}")
        
except Exception as e:
    print(f"❌ Error: {type(e).__name__}: {e}")
