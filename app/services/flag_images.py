#!/usr/bin/env python3
#
# app/services/flag_images.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Best-effort country flag PNGs for PDF reports, loaded from a fixed HTTPS host.

Nothing is cached: flags live only for the duration of one ``load_flags`` call, which downloads each
requested country code at most once. Concurrent downloads across threads are limited by a semaphore.

Purely decorative: every failure degrades to the plain ISO country code. Only the request
path carries data (a validated country code); no IP, hostname or report content is sent.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from collections.abc import Iterable

import httpx
from PIL import Image

_log = logging.getLogger(__name__)

FLAG_HOST = "flagcdn.com"  # fixed allowlist: never taken from data or settings
FLAG_SIZE = "48x36"  # 4:3 PNG, about 340 dpi at the 3.6 x 2.7 mm print size
FLAG_URL_TEMPLATE = f"https://{FLAG_HOST}/{FLAG_SIZE}/{{code}}.png"
MAX_BYTES = 50 * 1024
MAX_PIXELS = 256 * 256
MAX_CODES_PER_REPORT = 20
REQUEST_TIMEOUT_S = 2.0
REPORT_BUDGET_S = 5.0
MAX_CONCURRENT_DOWNLOADS = 2  # across all threads: parallel PDF builds must not open many connections
_USER_AGENT = "justUp-pdf-report"
_CODE_RE = re.compile(r"[A-Z]{2}")

_download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)


def normalize_country_code(value: object) -> str | None:
    """Return ``value`` upper-cased if it has the shape of an ISO 3166-1 alpha-2 code (two letters A-Z).

    Only the form is checked, not membership in the ISO list; unknown codes just fail to download.
    """
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _CODE_RE.fullmatch(code) else None


def flag_url(code: str) -> str:
    """URL for an already validated code."""
    if _CODE_RE.fullmatch(code) is None:
        raise ValueError("invalid country code")
    return FLAG_URL_TEMPLATE.format(code=code.lower())


def _validated_png(data: bytes) -> bool:
    try:
        if len(data) > MAX_BYTES:
            return False
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "PNG" or img.width * img.height > MAX_PIXELS or min(img.size) < 1:
                return False
            img.verify()
        return True
    except Exception:  # noqa: BLE001 - any decoder problem means: discard the image
        return False


def _download(client: httpx.Client, code: str, deadline: float) -> bytes | None:
    """One GET without redirects; None on any problem or once ``deadline`` (monotonic) has passed."""
    timeout = min(REQUEST_TIMEOUT_S, deadline - time.monotonic())
    if timeout <= 0:
        return None
    try:
        with client.stream("GET", flag_url(code), timeout=timeout) as response:
            if response.status_code != 200:
                return None
            if response.headers.get("content-type", "").split(";")[0].strip().lower() != "image/png":
                return None
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_BYTES:
                return None
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > MAX_BYTES or time.monotonic() > deadline:
                    return None
    except (httpx.HTTPError, ValueError, OSError):
        return None
    return bytes(data) if _validated_png(bytes(data)) else None


def _load(codes: Iterable[object], result: dict[str, bytes]) -> None:
    unique = list(dict.fromkeys(c for c in map(normalize_country_code, codes) if c))[:MAX_CODES_PER_REPORT]
    deadline = time.monotonic() + REPORT_BUDGET_S
    if not unique:
        return
    with httpx.Client(
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "image/png"},
        timeout=REQUEST_TIMEOUT_S,
    ) as client:
        for code in unique:
            wait = deadline - time.monotonic()
            if wait <= 0 or not _download_slots.acquire(timeout=wait):
                break
            try:
                data = _download(client, code, deadline)
            finally:
                _download_slots.release()
            if data is not None:
                result[code] = data


def load_flags(codes: Iterable[object]) -> dict[str, bytes]:
    """Return PNG bytes per valid code; codes without a usable flag are simply absent.

    Sequential, exactly one download attempt per unique code, nothing kept afterwards. Capped per
    report and by a total time budget that also bounds a slowly streaming server. Never raises;
    on any error the flags found so far are returned. Call it outside any lock.
    """
    result: dict[str, bytes] = {}
    try:
        _load(codes, result)
    except Exception:  # noqa: BLE001 - decorative feature must never break the report
        _log.debug("Flag loading aborted", exc_info=True)
    return result
