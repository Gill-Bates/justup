#!/usr/bin/env python3
#
# app/api/csp_report.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Unauthenticated CSP violation report sink: bounded, rate-limited, log-only."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response

from ..middleware.security_headers import CSP_REPORT_PATH
from ..utils.rate_limit import rate_limit

_log = logging.getLogger(__name__)

router = APIRouter(tags=["security"], include_in_schema=False)

MAX_REPORT_BYTES = 8 * 1024
_MAX_FIELD_CHARS = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ACCEPTED_TYPES = {"application/csp-report", "application/json", "application/reports+json"}
# Only these fields are logged; the original-policy, referrer and script-sample are dropped.
_FIELDS = ("violated-directive", "effective-directive", "blocked-uri", "document-uri", "source-file")


def _clean(value: object) -> str:
    """Strip query/fragment from URLs, drop control characters (log injection), truncate."""
    text = str(value)
    if "://" in text:
        parts = urlsplit(text)
        text = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return _CONTROL_RE.sub("?", text)[:_MAX_FIELD_CHARS]


# Reporting API (``application/reports+json``) uses camelCase keys in a list of typed reports.
_REPORTING_KEYS = {
    "effectiveDirective": "effective-directive",
    "blockedURL": "blocked-uri",
    "documentURL": "document-uri",
    "sourceFile": "source-file",
}


def _extract(payload: object) -> dict[str, str]:
    """Return the whitelisted fields of the first violation in a legacy or Reporting API payload."""
    report: object = payload.get("csp-report") if isinstance(payload, dict) else None
    if not isinstance(report, dict) and isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("type") == "csp-violation" and isinstance(item.get("body"), dict):
                body = item["body"]
                report = {legacy: body.get(camel) for camel, legacy in _REPORTING_KEYS.items()}
                break
    if not isinstance(report, dict):
        return {}
    return {name: _clean(report[name]) for name in _FIELDS if report.get(name) not in (None, "")}


@router.post(CSP_REPORT_PATH, status_code=204, dependencies=[Depends(rate_limit("30/minute"))])
async def csp_report(request: Request) -> Response:
    """Log a legacy ``report-uri`` violation report; always answers 204 once accepted."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_REPORT_BYTES:
        return Response(status_code=413)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in _ACCEPTED_TYPES:
        return Response(status_code=415)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REPORT_BYTES:
            return Response(status_code=413)
    try:
        fields = _extract(json.loads(bytes(body)))
    except (ValueError, RecursionError):
        fields = {}
    if fields:
        _log.warning("CSP_VIOLATION %s", " ".join(f"{k}={v!r}" for k, v in fields.items()))
    return Response(status_code=204)
