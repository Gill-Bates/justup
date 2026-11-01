#!/usr/bin/env python3
#
# tests/test_flag_images.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Country flag loader: fixed host, validation, limits, caches and fail-soft behavior (no real network)."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
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
def _clean_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path / "data"))
    flag_images.clear_cache()
    yield
    flag_images.clear_cache()


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


def test_cache_hit_needs_no_second_request(monkeypatch):
    net = _Net(monkeypatch, _ok)
    flag_images.load_flags(["DE"])
    assert set(flag_images.load_flags(["de", "DE"])) == {"DE"}
    assert len(net.urls) == 1


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
def test_failures_fall_back_without_raising_and_are_negatively_cached(monkeypatch, handler):
    net = _Net(monkeypatch, handler)
    assert flag_images.load_flags(["DE"]) == {}
    assert flag_images.load_flags(["DE"]) == {}  # negative cache: no retry within the TTL
    assert len(net.urls) == 1
    assert all(u.host == "flagcdn.com" for u in net.urls)  # a redirect is never followed


def test_negative_cache_expires(monkeypatch):
    net = _Net(monkeypatch, lambda _r: httpx.Response(500))
    flag_images.load_flags(["DE"])
    flag_images._failed["DE"] = time.monotonic() - 1  # noqa: SLF001
    flag_images.load_flags(["DE"])
    assert len(net.urls) == 2


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


def test_memory_cache_is_bounded(monkeypatch):
    _Net(monkeypatch, _ok)
    monkeypatch.setattr(flag_images, "CACHE_SIZE", 2)
    monkeypatch.setattr(flag_images, "MAX_CODES_PER_REPORT", 5)
    flag_images.load_flags(["AA", "BB", "CC"])
    assert len(flag_images._cache) == 2  # noqa: SLF001
    assert "AA" not in flag_images._cache  # noqa: SLF001


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


def test_parallel_loads_of_one_code_download_once(monkeypatch):
    def slow_ok(request):
        time.sleep(0.3)
        return _ok(request)

    net = _Net(monkeypatch, slow_ok)
    results = []
    threads = [threading.Thread(target=lambda: results.append(flag_images.load_flags(["DE"]))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(net.urls) == 1
    assert all(set(r) == {"DE"} for r in results)


def test_file_cache_survives_restart_without_network(monkeypatch):
    _Net(monkeypatch, _ok)
    flag_images.load_flags(["DE"])
    assert (Path(flag_images._file_cache_dir()) / "de.png").is_file()  # noqa: SLF001
    flag_images.clear_cache()  # simulates a new process
    net = _Net(monkeypatch, lambda _r: httpx.Response(500))
    assert set(flag_images.load_flags(["DE"])) == {"DE"}
    assert net.urls == []


def test_broken_file_cache_entry_is_discarded_and_reloaded(monkeypatch):
    directory = Path(flag_images._file_cache_dir())  # noqa: SLF001
    directory.mkdir(parents=True)
    (directory / "de.png").write_bytes(b"garbage")
    net = _Net(monkeypatch, _ok)
    assert set(flag_images.load_flags(["DE"])) == {"DE"}
    assert len(net.urls) == 1
    assert (directory / "de.png").read_bytes().startswith(b"\x89PNG")


def test_file_cache_is_bounded_and_leaves_no_temp_files(monkeypatch):
    _Net(monkeypatch, _ok)
    monkeypatch.setattr(flag_images, "FILE_CACHE_MAX_FILES", 2)
    for code in ("AA", "BB", "CC"):
        flag_images.load_flags([code])
        time.sleep(0.02)
    names = sorted(p.name for p in Path(flag_images._file_cache_dir()).iterdir())  # noqa: SLF001
    assert names == ["bb.png", "cc.png"]


def test_unwritable_file_cache_does_not_affect_loading(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(blocker))
    _Net(monkeypatch, _ok)
    assert set(flag_images.load_flags(["DE"])) == {"DE"}


def test_a_failing_input_iterable_never_raises(monkeypatch):
    _Net(monkeypatch, _ok)

    def codes():
        yield "DE"
        raise RuntimeError("boom")

    assert isinstance(flag_images.load_flags(codes()), dict)
