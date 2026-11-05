#!/usr/bin/env python3
#
# tests/test_flag_images.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Country flag loader: fixed host, validation, limits, no caching and fail-soft behavior (no real network)."""

from __future__ import annotations

import io
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.services import flag_images


def _png(size=(48, 36), color=(200, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class _Net:
    """Replaces httpx.Client inside flag_images with a mock transport and records every request URL."""

    def __init__(self, monkeypatch, handler):
        self.urls: list[httpx.URL] = []
        self.requests: list[httpx.Request] = []
        real_client = httpx.Client

        def transport(request: httpx.Request) -> httpx.Response:
            self.urls.append(request.url)
            self.requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            flag_images.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw)
        )


def _ok(_request):
    return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


def test_success_returns_png_and_url_contains_only_allowlisted_host_and_code(monkeypatch):
    net = _Net(monkeypatch, _ok)
    result = flag_images.load_flags([" de "])
    assert set(result) == {"DE"}
    assert result["DE"].startswith(b"\x89PNG")
    (url,) = net.urls
    assert (url.scheme, url.host, url.path, url.query) == ("https", "flagcdn.com", "/48x36/de.png", b"")
    request = net.requests[0]
    assert "cookie" not in request.headers and "referer" not in request.headers
    assert request.headers["user-agent"] == "justUp-pdf-report"


@pytest.mark.parametrize("bad", [None, "", "D", "DEU", "12", "  ", "D1", "../", "d/", 5, "ÄÖ"])
def test_invalid_codes_make_no_request(monkeypatch, bad):
    net = _Net(monkeypatch, _ok)
    assert flag_images.load_flags([bad]) == {}
    assert net.urls == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda _r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
        lambda _r: httpx.Response(404, content=b"nope", headers={"content-type": "text/plain"}),
        lambda _r: httpx.Response(302, headers={"location": "https://evil.example/x.png"}),
        lambda _r: httpx.Response(200, content=b"not a png", headers={"content-type": "image/png"}),
        lambda _r: httpx.Response(200, content=_png(), headers={"content-type": "text/html"}),
        lambda _r: httpx.Response(200, content=_png() + b"\0" * 60_000, headers={"content-type": "image/png"}),
        lambda _r: httpx.Response(200, content=_png((400, 400)), headers={"content-type": "image/png"}),
    ],
    ids=["timeout", "http-error", "redirect", "bad-image", "wrong-type", "too-large", "too-many-pixels"],
)
def test_failures_fall_back_without_raising_and_are_retried_next_time(monkeypatch, handler):
    net = _Net(monkeypatch, handler)
    assert flag_images.load_flags(["DE"]) == {}
    assert flag_images.load_flags(["DE"]) == {}  # no negative cache: every call tries again
    assert len(net.urls) == 2
    assert all(u.host == "flagcdn.com" for u in net.urls)  # a redirect is never followed


def test_codes_per_report_are_capped_and_deduplicated(monkeypatch):
    net = _Net(monkeypatch, _ok)
    codes = [a + b for a in "ABCDEFG" for b in "ABCDEFG"]
    flag_images.load_flags(codes + codes)
    assert len(net.urls) == flag_images.MAX_CODES_PER_REPORT


def test_time_budget_stops_further_requests(monkeypatch):
    net = _Net(monkeypatch, _ok)
    monkeypatch.setattr(flag_images, "REPORT_BUDGET_S", 0.0)
    assert flag_images.load_flags(["DE", "GB"]) == {}
    assert net.urls == []


def test_client_construction_failure_never_raises(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("no ssl")

    monkeypatch.setattr(flag_images.httpx, "Client", boom)
    assert flag_images.load_flags(["DE"]) == {}


def test_flag_url_rejects_unvalidated_input():
    with pytest.raises(ValueError, match="invalid"):
        flag_images.flag_url("de/../x")


class _SlowStream(httpx.SyncByteStream):
    """Delivers valid-looking chunks while advancing a fake clock."""

    def __init__(self, clock):
        self.clock = clock

    def __iter__(self):
        for _ in range(100):
            self.clock[0] += 1.0
            yield b"\x89PNG"


def test_slowly_streaming_server_is_cut_off_within_the_budget(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(flag_images, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    chunks = []
    stream = _SlowStream(clock)
    original = stream.__iter__

    def counting():
        for chunk in original():
            chunks.append(chunk)
            yield chunk

    stream.__iter__ = counting
    _Net(monkeypatch, lambda _r: httpx.Response(200, headers={"content-type": "image/png"}, stream=stream))
    assert flag_images.load_flags(["DE"]) == {}
    assert clock[0] - 1000.0 <= flag_images.REPORT_BUDGET_S + 1.0
    assert len(chunks) < 10


def test_each_code_is_downloaded_once_per_call_and_again_on_the_next(monkeypatch):
    net = _Net(monkeypatch, _ok)
    assert set(flag_images.load_flags(["de", "DE", " De "])) == {"DE"}
    assert len(net.urls) == 1
    assert set(flag_images.load_flags(["DE"])) == {"DE"}
    assert len(net.urls) == 2  # nothing was kept between the calls


def test_no_cache_files_or_directories_are_created(monkeypatch, tmp_path):
    _Net(monkeypatch, _ok)
    before = sorted(p for p in tmp_path.rglob("*"))
    assert set(flag_images.load_flags(["DE", "GB"])) == {"DE", "GB"}
    assert sorted(p for p in tmp_path.rglob("*")) == before
    assert not (tmp_path / "data" / "flags").exists()
    assert not any(tmp_path.rglob("*.png"))


def test_module_keeps_no_cache_state():
    for name in ("_cache", "_failed", "_inflight", "clear_cache", "_file_cache_dir", "CACHE_SIZE", "NEGATIVE_TTL_S"):
        assert not hasattr(flag_images, name)


def test_concurrent_downloads_are_limited(monkeypatch):
    active = [0]
    peak = [0]
    guard = threading.Lock()

    def slow_ok(request):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with guard:
            active[0] -= 1
        return _ok(request)

    _Net(monkeypatch, slow_ok)
    results = []
    threads = [threading.Thread(target=lambda: results.append(flag_images.load_flags(["DE"]))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] <= flag_images.MAX_CONCURRENT_DOWNLOADS
    assert all(set(r) == {"DE"} for r in results)


def test_a_failing_input_iterable_never_raises(monkeypatch):
    _Net(monkeypatch, _ok)

    def codes():
        yield "DE"
        raise RuntimeError("boom")

    assert isinstance(flag_images.load_flags(codes()), dict)
