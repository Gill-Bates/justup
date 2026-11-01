#!/usr/bin/env python3
#
# app/services/flag_images.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Best-effort country flag PNGs for PDF reports, loaded from a fixed HTTPS host.

Lookup order: memory (LRU), small file cache under <data dir>/flags, then one download.

Purely decorative: every failure degrades to the plain ISO country code. Only the request
path carries data (a validated country code); no IP, hostname or report content is sent.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from pathlib import Path

import httpx
from PIL import Image

from app.utils.config import _env_data_dir

_log = logging.getLogger(__name__)

FLAG_HOST = "flagcdn.com"  # fixed allowlist: never taken from data or settings
FLAG_SIZE = "48x36"  # 4:3 PNG, about 340 dpi at the 3.6 x 2.7 mm print size
FLAG_URL_TEMPLATE = f"https://{FLAG_HOST}/{FLAG_SIZE}/{{code}}.png"
MAX_BYTES = 50 * 1024
MAX_PIXELS = 256 * 256
MAX_CODES_PER_REPORT = 20
REQUEST_TIMEOUT_S = 2.0
REPORT_BUDGET_S = 5.0
NEGATIVE_TTL_S = 600.0
CACHE_SIZE = 64
FILE_CACHE_DIRNAME = "flags"  # not part of BACKUP_DIRECTORIES: a cache, restored on demand
FILE_CACHE_MAX_FILES = 256
FILE_CACHE_MAX_BYTES = 4 * 1024 * 1024
_USER_AGENT = "justUp-pdf-report"
_CODE_RE = re.compile(r"[A-Z]{2}")

_lock = threading.Lock()  # guards the shared tables only; never held during network or file I/O
_cache: OrderedDict[str, bytes] = OrderedDict()
_failed: dict[str, float] = {}  # code -> monotonic expiry
_inflight: dict[str, threading.Event] = {}  # single-flight: one loader per code across threads


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


def clear_cache() -> None:
    """Reset the in-memory state (the file cache is left alone)."""
    with _lock:
        _cache.clear()
        _failed.clear()


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


# --- file cache -------------------------------------------------------------------------------


def _file_cache_dir() -> Path:
    return _env_data_dir() / FILE_CACHE_DIRNAME


def _file_read(code: str) -> bytes | None:
    """Cached PNG for ``code``; a broken or oversized file is removed and reported as a miss."""
    try:
        path = _file_cache_dir() / f"{code.lower()}.png"
        if path.is_symlink():
            path.unlink(missing_ok=True)
            return None
        if not path.is_file():
            return None
        if path.stat().st_size <= MAX_BYTES:
            data = path.read_bytes()
            if _validated_png(data):
                return data
        path.unlink(missing_ok=True)
    except OSError:
        _log.debug("Flag file cache read failed", exc_info=True)
    return None


def _file_write(code: str, data: bytes) -> None:
    """Atomic write, then drop the oldest files beyond the count and byte limits. Errors are ignored."""
    tmp_name = None
    try:
        directory = _file_cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        Path(tmp_name).replace(directory / f"{code.lower()}.png")
        tmp_name = None
        files = sorted(((p.stat().st_mtime, p.stat().st_size, p) for p in directory.glob("??.png")), key=lambda t: t[0])
        total = sum(size for _, size, _ in files)
        while files and (len(files) > FILE_CACHE_MAX_FILES or total > FILE_CACHE_MAX_BYTES):
            _, size, oldest = files.pop(0)
            oldest.unlink(missing_ok=True)
            total -= size
    except OSError:
        _log.debug("Flag file cache write failed", exc_info=True)
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


# --- loading ----------------------------------------------------------------------------------


def _remember(code: str, data: bytes) -> None:
    with _lock:
        _cache[code] = data
        _cache.move_to_end(code)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def _resolve(code: str, deadline: float, client_for: Callable[[], httpx.Client]) -> bytes | None:
    """File cache, else one download; the caller owns the single-flight slot for ``code``."""
    data = _file_read(code)
    if data is not None:
        _remember(code, data)
        return data
    data = _download(client_for(), code, deadline)
    if data is None:
        with _lock:
            _failed[code] = time.monotonic() + NEGATIVE_TTL_S
        return None
    _remember(code, data)
    _file_write(code, data)
    return data


def _load(codes: Iterable[object], result: dict[str, bytes]) -> None:
    unique = list(dict.fromkeys(c for c in map(normalize_country_code, codes) if c))[:MAX_CODES_PER_REPORT]
    deadline = time.monotonic() + REPORT_BUDGET_S
    with ExitStack() as stack:
        client: list[httpx.Client] = []

        def client_for() -> httpx.Client:
            if not client:
                client.append(
                    stack.enter_context(
                        httpx.Client(
                            follow_redirects=False,
                            headers={"User-Agent": _USER_AGENT, "Accept": "image/png"},
                            timeout=REQUEST_TIMEOUT_S,
                        )
                    )
                )
            return client[0]

        for code in unique:
            with _lock:
                if code in _cache:
                    _cache.move_to_end(code)
                    result[code] = _cache[code]
                    continue
                if _failed.get(code, 0.0) > time.monotonic():
                    continue
                waiter = _inflight.get(code)
                if waiter is None:
                    _inflight[code] = threading.Event()
            if waiter is not None:  # another thread loads this code: wait for it, within the budget
                waiter.wait(max(0.0, min(REQUEST_TIMEOUT_S, deadline - time.monotonic())))
                with _lock:
                    if code in _cache:
                        result[code] = _cache[code]
                continue
            try:
                data = _resolve(code, deadline, client_for)
            finally:
                with _lock:
                    _inflight.pop(code).set()
            if data is not None:
                result[code] = data


def load_flags(codes: Iterable[object]) -> dict[str, bytes]:
    """Return PNG bytes per valid code; codes without a usable flag are simply absent.

    Sequential, at most one download per unique uncached code (also across threads), capped per
    report and by a total time budget that also bounds a slowly streaming server. Never raises;
    on any error the flags found so far are returned. Call it outside any lock.
    """
    result: dict[str, bytes] = {}
    try:
        _load(codes, result)
    except Exception:  # noqa: BLE001 - decorative feature must never break the report
        _log.debug("Flag loading aborted", exc_info=True)
    return result
