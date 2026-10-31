#!/usr/bin/env python3
#
# app/services/seed/_fetch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared download helpers for the POP seed loaders (no redirects, atomic cache writes)."""

from __future__ import annotations

import contextlib
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the seed sources are fixed HTTPS URLs and must not bounce elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise HTTPError(req.full_url, code, f"redirect to {newurl!r} refused", headers, fp)


def open_no_redirect(req: urllib.request.Request, *, timeout: float, context: Any) -> Any:
    """Open ``req`` with a TLS context and without following HTTP redirects."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
    return opener.open(req, timeout=timeout)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a unique temp file in the same directory and ``os.replace``."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        Path(tmp_name).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise
