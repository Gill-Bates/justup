#!/usr/bin/env python3
#
# tests/test_pdf_report_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

# ruff: noqa: SLF001, ARG005, PLW0108
"""PDF report builder: provider table contract, host choice and markup escaping."""

from __future__ import annotations

import dataclasses
import io
import itertools
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import matplotlib.colors as mcolors
import matplotlib.dates as mdates
import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import LineCollection
from reportlab.platypus import SimpleDocTemplate

from app.services import pdf_logo, pdf_report
from app.services.pdf_report import PDFReportBuilder, ReportData, _esc
from app.utils.tls_model import Check, ProtocolResult, ScanStatus, TlsScanResult, Verdict


def _builder(target: dict) -> PDFReportBuilder:
    now = datetime.now(UTC)
    data = ReportData(
        target=target,
        from_date=now,
        to_date=now,
        http_data=[],
        uptime_data=[],
        ping_data=[],
        tcp_data=[],
        cert_data=[],
        status_code_data=[],
    )
    return PDFReportBuilder(data)


def _render(builder: PDFReportBuilder) -> bytes:
    buf = io.BytesIO()
    SimpleDocTemplate(buf).build(list(builder.elements))
    return buf.getvalue()


def _info(**over):
    base = {"ip": "203.0.113.5", "asn": "AS64500", "provider": "Example", "location": "Berlin", "country_code": "DE"}
    base.update(over)
    return base


@pytest.mark.parametrize(
    "info",
    [
        _info(),
        # DNS failure / lookup disabled returns placeholders
        {"ip": "-", "asn": "-", "provider": "-", "location": "-", "country_code": ""},
    ],
)
def test_provider_table_uses_ip_key(monkeypatch, info):
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: info)
    b = _builder({"http_url": "https://example.com:8443/x"})
    b._add_provider_info()
    assert len(b.elements) == 1
    texts = _texts(b)  # collected before rendering, which consumes the flowables
    assert _render(b).startswith(b"%PDF")
    assert (info["ip"] if info["ip"] != "-" else "—") in texts
    assert info["provider"] in texts or info["provider"] == "-"


def test_provider_table_markup_in_external_fields_is_escaped(monkeypatch):
    evil = _info(
        ip="1.2.3.4",
        asn="AS<b>1&2",
        provider='<font color="red">Evil & "Co"</font>\x00\x07',
        location="A<B>",
        country_code="X",
    )
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: evil)
    b = _builder({"ping_host": "example.com"})
    b._add_provider_info()
    texts = _texts(b)  # collected before rendering, which consumes the flowables
    assert _render(b).startswith(b"%PDF")
    assert '<font color="red">' not in texts
    assert "&lt;font" in texts
    assert "AS&lt;b&gt;1&amp;2" in texts
    assert "\x00" not in texts
    assert "\x07" not in texts


def test_provider_section_skipped_without_host(monkeypatch):
    called = []
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: called.append(host))
    b = _builder({})
    b._add_provider_info()
    assert not b.elements
    assert not called


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ({"http_url": "https://example.com:8443/path", "ping_host": "p"}, "example.com"),
        ({"http_url": "http://[2001:db8::1]:80/", "ping_host": "p"}, "2001:db8::1"),
        ({"http_url": "http://[bad", "ping_host": "p"}, "p"),
        ({"http_url": "", "host": "h", "cert_host": "c"}, "h"),
        ({"cert_host": "c"}, "c"),
        ({}, None),
    ],
)
def test_lookup_host_selection(monkeypatch, target, expected):
    seen = []
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: seen.append(host) or _info())
    b = _builder(target)
    b._add_provider_info()
    assert (seen[0] if seen else None) == expected


def test_esc_handles_markup_quotes_and_control_chars():
    assert _esc("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    assert _esc('<font color="red">x</font>') == '&lt;font color="red"&gt;x&lt;/font&gt;'
    assert _esc("a\x00b\x1fc") == "a b c"
    assert _esc(None) == ""


def test_esc_preserves_unicode_text():
    assert _esc("Grüße 世界 🙂") == "Grüße 世界 🙂"


@pytest.mark.parametrize(
    "font_name",
    ["IBMPlexSans", "IBMPlexSans-Bold", "IBMPlexSans-Italic", "IBMPlexSans-BoldItalic"],
)
def test_shaped_width_uses_harfbuzz_kerning(font_name):
    pdf_report._register_fonts()
    assert pdf_report.pdfmetrics.getFont(font_name).shapable
    for text in ("AV", "To", "Wa", "VA", "Ty", "Availability & Performance"):
        unshaped = pdf_report.pdfmetrics.stringWidth(text, font_name, 20)
        shaped = pdf_report._text_width(text, font_name, 20)
        assert shaped > 0
        assert shaped <= unshaped


def test_canvas_text_helper_passes_shaped_glyphs_and_uses_shaped_alignment():
    pdf_report._register_fonts()

    class Canvas:
        _fontname = "IBMPlexSans"
        _fontsize = 20

        def drawString(self, x, y, text):  # noqa: N802 - mirrors ReportLab canvas API
            self.drawn = (x, y, text)

    canvas = Canvas()
    width = pdf_report._text_width("AV", canvas._fontname, canvas._fontsize)
    pdf_report._draw_text(canvas, 100, 20, "AV", align="center")
    x, y, shaped = canvas.drawn
    assert x == pytest.approx(100 - width / 2)
    assert y == 20
    assert isinstance(shaped, pdf_report.ShapedStr)


def test_empty_canvas_text_has_zero_width_and_is_not_drawn():
    pdf_report._register_fonts()

    class Canvas:
        _fontname = "IBMPlexSans"
        _fontsize = 20

        def drawString(self, *_args):  # noqa: N802 - mirrors ReportLab canvas API
            pytest.fail("empty text must not be sent to the canvas")

    assert pdf_report._text_width("", "IBMPlexSans", 20) == 0
    pdf_report._draw_text(Canvas(), 0, 0, "")


def test_paragraph_style_requires_shaping():
    pdf_report._register_fonts()
    style = pdf_report._paragraph_style("Contract", font_name="IBMPlexSans")
    assert style.shaping == 1


def test_generate_pdf_report_returns_reportlab_pdf_without_converter(monkeypatch):
    expected_pdf = b"%PDF-1.4 ReportLab output"

    class Builder:
        def __init__(self, _report_data, page_size):
            assert page_size == "a4"

        def prefetch_external_data(self):
            pass

        def build(self):
            return expected_pdf

    monkeypatch.setattr(pdf_report, "PDFReportBuilder", Builder)

    assert pdf_report.generate_pdf_report(object()) == expected_pdf


def test_downtime_table_repeats_header_on_page_break():
    now = datetime.now(UTC)
    builder = _builder({})
    builder.downtime_events = [
        pdf_report.DowntimeEvent(start=now + timedelta(hours=hour), end=None, duration_minutes=None)
        for hour in range(30)
    ]

    builder._build_downtime_page()

    table = next(
        element
        for element in builder.elements
        if getattr(element, "repeatRows", 0) == 1
        and getattr(getattr(element, "_cellvalues", [[None]])[0][0], "text", "") == "#"
    )
    assert table.repeatRows == 1
    assert len(table._cellvalues) == 32  # header + all 30 events + the note row, none dropped


def _tls_result(**overrides) -> TlsScanResult:
    import datetime as dt

    from app.utils.tls_model import CertificateInfo

    ok = Check(Verdict.SECURE)
    now = dt.datetime.now(dt.UTC)
    cert = CertificateInfo(
        subject="CN=host.example",
        issuer="CN=Issuer CA",
        valid_from=now - dt.timedelta(days=10),
        valid_until=now + dt.timedelta(days=80),
        days_remaining=80,
        signature_algorithm="sha256",
        key_type="RSA",
        key_size=2048,
        san=("host.example", "www.host.example"),
        chain_length=2,
        trust=ok,
        expiry=ok,
        signature=ok,
        key_strength=ok,
        hostname_match=ok,
        chain_order=ok,
    )
    protocols = tuple(
        ProtocolResult(n, legacy, supported, ("SUITE_A", "SUITE_B") if supported else ())
        for n, legacy, supported in (
            ("SSL 2.0", True, False),
            ("SSL 3.0", True, False),
            ("TLS 1.0", True, False),
            ("TLS 1.1", True, False),
            ("TLS 1.2", False, True),
            ("TLS 1.3", False, True),
        )
    )
    base = {
        "host": "host.example",
        "port": 443,
        "status": ScanStatus.COMPLETED,
        "certificate": cert,
        "certificate_check": ok,
        "protocols": protocols,
        "mozilla_tls_profile": ok,
        "fallback_scsv": ok,
        "extended_master_secret": ok,
        "tls_1_3_early_data": False,
        "session_resumption": "Session IDs: 5/5; TLS tickets: 4/5",
        "elliptic_curves": "secp256r1",
        "http_headers": "max-age=31536000; includeSubDomains",
        "heartbleed": ok,
        "ccs_injection": ok,
        "robot": ok,
        "secure_renegotiation": ok,
        "compression": ok,
    }
    return TlsScanResult(**{**base, **overrides})


def _texts(builder) -> str:
    """All paragraph text in the builder's elements (tables included), space-joined."""
    out = []

    def walk(el):
        if hasattr(el, "text") and isinstance(el.text, str):
            out.append(el.text)
        for row in getattr(el, "_cellvalues", []) or []:
            for cell in row:
                walk(cell)

    for el in builder.elements:
        walk(el)
    return " | ".join(out)


def _walk_text(el, out):
    if isinstance(el, tuple | list):
        for item in el:
            _walk_text(item, out)
        return
    if hasattr(el, "text") and isinstance(el.text, str):
        out.append(el.text)
    for attr in ("label", "value", "hint"):  # RoundedKPICard
        if isinstance(el, pdf_report.RoundedKPICard):
            out.append(getattr(el, attr))
    for row in getattr(el, "_cellvalues", []) or []:
        for cell in row:
            _walk_text(cell, out)
    for child in getattr(el, "_content", []) or []:
        _walk_text(child, out)


def _ssl_section(monkeypatch, result) -> tuple[str, bytes]:
    b = _builder({"http_url": "https://host.example"})
    monkeypatch.setattr(b, "_get_ssl_data", lambda: result)
    b._add_ssl_summary()
    return _texts(b), _render(b)


def test_ssl_section_secure_scan_shows_all_values(monkeypatch):
    text, pdf = _ssl_section(monkeypatch, _tls_result())
    assert pdf.startswith(b"%PDF")
    for expected in (
        "CN=host.example",
        "CN=Issuer CA",
        "80 days",
        "RSA 2048-bit",
        "www.host.example",
        "Certificate",
        "TLS &amp; Security",
        "Mozilla TLS Profile (Intermediate)",
        "TLS_FALLBACK_SCSV",
        "Extended Master Secret",
        "TLS 1.3 Early Data",
        "Session IDs: 5/5; TLS tickets: 4/5",
        "secp256r1",
        "max-age=31536000",
        "Cipher Suite Example",
        "SUITE_A",
    ):
        assert expected in text
    assert text.count("Not vulnerable") == 3  # Heartbleed, CCS injection, ROBOT
    assert "TLS 1.3" in text and "NOT TESTED" not in text and "FAIL" not in text


def test_ssl_section_marks_insecure_findings_without_swapping_states(monkeypatch):
    bad = Check(Verdict.INSECURE)
    text, _ = _ssl_section(
        monkeypatch,
        _tls_result(heartbleed=bad, compression=bad, secure_renegotiation=bad, ccs_injection=Check(Verdict.SECURE)),
    )
    assert text.count("Vulnerable") == 1  # only Heartbleed
    assert "Compression enabled" in text and "Not supported" in text
    assert text.count("FAIL") == 3  # Heartbleed, compression, renegotiation
    assert text.count("Not vulnerable") == 2  # CCS and ROBOT stay secure


def test_ssl_section_renders_unknown_not_tested(monkeypatch):
    unknown = Check.unknown()
    result = _tls_result(
        status=ScanStatus.PARTIAL,
        heartbleed=unknown,
        robot=unknown,
        command_errors={"heartbleed": "CONNECTIVITY_ISSUE", "robot": "CONNECTIVITY_ISSUE"},
        protocols=(ProtocolResult("SSL 2.0", True, None), ProtocolResult("TLS 1.3", False, True, ("S",))),
    )
    text, pdf = _ssl_section(monkeypatch, result)
    assert pdf.startswith(b"%PDF")
    assert text.count("NOT TESTED") >= 3  # SSL 2.0, Heartbleed, ROBOT (and the weak-protocol roll-up)
    assert "INCOMPLETE" in text
    assert "Incomplete: heartbleed, robot failed" in text
    assert "Not vulnerable" in text and text.count("Not vulnerable") == 1  # only CCS


def test_ssl_section_failed_scan_is_not_presented_as_secure(monkeypatch):
    failed = TlsScanResult(host="h", port=443, status=ScanStatus.FAILED, error="connection to server failed")
    text, pdf = _ssl_section(monkeypatch, failed)
    assert pdf.startswith(b"%PDF")
    assert "TLS scan failed: connection to server failed" in text and "not a passed check" in text
    assert "SCAN FAILED" in text
    assert "Not vulnerable" not in text and "None offered" not in text


def test_ssl_section_skipped_for_non_applicable_target():
    b = _builder({"http_url": "http://plain.example"})
    b._add_ssl_summary()
    assert not b.elements


def test_ssl_section_survives_hostile_text(monkeypatch):
    base = _tls_result()
    cert = dataclasses.replace(
        base.certificate, subject='CN=<font color="red">a&b', issuer="O=<i>Issuer", san=("<b>x</b>.example",)
    )
    text, pdf = _ssl_section(monkeypatch, dataclasses.replace(base, certificate=cert))
    assert pdf.startswith(b"%PDF")
    assert '<font color="red">' not in text and "<i>Issuer" not in text and "<b>x</b>" not in text
    assert "CN=&lt;font" in text and "O=&lt;i&gt;Issuer" in text


def test_ssl_section_lists_every_certificate_deployment(monkeypatch):
    base = _tls_result()
    alternate = dataclasses.replace(base.certificate, subject="CN=alternate.example", san=("alternate.example",))
    result = dataclasses.replace(base, certificate_deployments=(base.certificate, alternate))
    text, pdf = _ssl_section(monkeypatch, result)
    assert pdf.startswith(b"%PDF")
    assert "Deployment 1" in text and "Deployment 2" in text
    assert "CN=alternate.example" in text


def test_completed_scan_with_unknown_checks_is_not_marked_complete(monkeypatch):
    result = _tls_result(mozilla_tls_profile=Check.unknown("certificate deployment unavailable"))
    text, _ = _ssl_section(monkeypatch, result)
    assert "Completed with inconclusive checks" in text
    assert "INCONCLUSIVE" in text and "No result: certificate deployment unavailable" in text


def test_stack_section_survives_hostile_text(monkeypatch):
    stack = {
        "webserver": "nginx <1.0> & co",
        "cms": '<font color="red">WP</font>',
        "country": "D&E",
        "ip": "1.1.1.1",
        "security": {},
    }
    b = _builder({"http_url": "https://example.com"})
    monkeypatch.setattr(b, "_get_whatweb_data", lambda: stack)
    b._add_detected_stack()
    assert b.elements
    text = _texts(b)  # collected before rendering, which consumes the flowables
    assert _render(b).startswith(b"%PDF")
    assert '<font color="red">' not in text and "nginx &lt;1.0&gt; &amp; co" in text


def test_get_ssl_data_turns_scanner_crash_into_failed_result(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("secret-detail")

    monkeypatch.setattr(pdf_report, "scan_tls", boom)
    result = _builder({"http_url": "https://host.example"})._get_ssl_data()
    assert result.status is ScanStatus.FAILED and "secret-detail" not in (result.error or "")


def test_pdf_tls_scan_deadline_leaves_time_before_the_job_timeout(monkeypatch):
    calls = []

    def scan(target, **kwargs):
        calls.append((target, kwargs))
        return _tls_result()

    monkeypatch.setattr(pdf_report, "scan_tls", scan)
    _builder({"http_url": "https://host.example"})._get_ssl_data()
    assert calls == [("https://host.example", {"timeout": 15, "deadline": 60.0})]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"http_url": "https://host.example", "cert_port": 8443}, "https://host.example:8443"),
        ({"cert_host": "2001:db8::1", "cert_port": 8443}, "https://[2001:db8::1]:8443"),
        ({"cert_host": "2001:db8::1"}, "https://[2001:db8::1]"),
        ({"cert_host": "c.example", "http_url": "https://h.example:9443/x"}, "https://c.example:9443"),
        ({"cert_host": "c.example", "http_url": "https://h.example", "http_port": 8443}, "https://c.example:8443"),
        ({"cert_host": "c.example", "http_url": "http://h.example"}, "https://c.example"),
        ({"http_url": "https://[2001:db8::1]:8443/"}, "https://[2001:db8::1]:8443"),
        ({"http_url": "http://host.example", "http_port": 8080}, None),
        ({"http_url": "https://host.example", "cert_port": 99999}, None),
    ],
)
def test_get_ssl_data_uses_the_shared_certificate_target_resolution(monkeypatch, fields, expected):
    calls = []

    def scan(target, **_kwargs):
        calls.append(target)
        return _tls_result()

    monkeypatch.setattr(pdf_report, "scan_tls", scan)
    result = _builder(fields)._get_ssl_data()
    assert calls == ([expected] if expected else [])
    assert (result is None) == (expected is None)


def _scan_targets(monkeypatch, fields) -> list[str]:
    calls = []
    monkeypatch.setattr(pdf_report, "scan_tls", lambda target, **_k: calls.append(target) or _tls_result())
    _builder(fields)._get_ssl_data()
    return calls


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # A configured http:// is never upgraded to https:// for a scan, whatever else is set.
        ({"http_url": "http://host.example"}, []),
        ({"http_url": "HTTP://host.example"}, []),
        ({"http_url": "http://host.example:8443/", "cert_port": 8443}, []),
        ({"http_url": "http://[2001:db8::1]:8080/"}, []),
        ({"http_url": "http://u:pw@host.example/p?token=x#frag", "http_port": 8443}, []),
        ({"http_url": "ftp://host.example"}, []),
        ({"http_url": "http://[bad"}, []),
        # The scheme check is case-insensitive; userinfo, path, query and fragment never reach the scan target.
        ({"http_url": "HTTPS://Host.Example/"}, ["https://host.example"]),
        ({"http_url": "https://u:pw@host.example:8443/p?token=x#frag"}, ["https://host.example:8443"]),
        ({"http_url": "https://[2001:db8::1]:8443/"}, ["https://[2001:db8::1]:8443"]),
        ({"http_url": "https://host.example", "cert_port": 8443}, ["https://host.example:8443"]),
        # An explicit cert_host is the user's request for a TLS scan, also next to a plain-HTTP site.
        ({"http_url": "http://h.example", "cert_host": "c.example", "cert_port": 8443}, ["https://c.example:8443"]),
        ({"http_url": "https://h.example:9443/", "cert_host": "c.example"}, ["https://c.example:9443"]),
    ],
)
def test_ssl_scan_never_turns_http_into_https_and_never_exposes_url_secrets(monkeypatch, fields, expected):
    assert _scan_targets(monkeypatch, fields) == expected


def test_plain_http_target_gets_a_neutral_marking_and_no_tls_section_or_secrets(monkeypatch):
    url = "http://user:hunter2@plain.example:8080/path?token=SECRET#frag"
    b = _full_builder(
        monkeypatch, ssl=None, target_extra={"http_url": url, "host": None, "ping_host": None, "cert_host": None}
    )
    b._assemble()
    text = _texts(b)
    assert "Certificate" not in text and "Not applicable" not in text
    assert "HTTP" in text and "no TLS scan" in text
    assert "plain.example" in text
    for leaked in ("user", "hunter2", "SECRET", "frag", "token="):
        assert leaked not in text
    uris = set(re.findall(rb"/URI \(([^)]*)\)", b.build()))
    assert uris <= {b"https://github.com/Gill-Bates/justUp"}  # no link from the monitored URL


def test_secure_target_still_shows_the_tls_sections(monkeypatch):
    b = _full_builder(monkeypatch, ssl=_tls_result())
    b._assemble()
    assert "Certificate" in _texts(b)


# ─── Layout contracts: chapters, page breaks, repeated headers, A4/Letter ──────────────────────


def _series(n=120, *, days=7, fail=()):
    start = datetime(2026, 10, 21, tzinfo=UTC)
    step = timedelta(days=days) / n
    up = [{"ts": (start + step * i).isoformat(), "value": 0 if i in fail else 1} for i in range(n)]
    http = [{"ts": (start + step * i).isoformat(), "value": 150 + i % 40} for i in range(n)]
    codes = [{"ts": (start + step * i).isoformat(), "value": 200 if i % 5 else 301} for i in range(n)]
    return start, start + timedelta(days=days), up, http, codes


def _hop(i, *, long=False, status="ok"):
    return {
        "hop": i,
        "ip": f"2001:db8:85a3:{i:x}:8a2e:370:7334:{i * 17:x}",
        "hostname": f"ae-{i}-very-long-router-name.backbone.some-carrier-network.example.com"
        if long
        else f"r{i}.example.net",
        "latency_ms": {"avg": 1.5 * i},
        "loss_pct": 0.0,
        "status": status,
        "city": "Frankfurt",
        "country": "DE",
    }


def _full_builder(
    monkeypatch,
    *,
    size="a4",
    hops=(),
    ssl=None,
    fail=(),
    nodata=False,
    target_extra=None,
    ping=False,
    tcp_ports=0,
    sla=False,
):
    start, end, up, http, codes = _series(fail=fail)
    if nodata:
        up = http = codes = []
    target = {
        "name": "Example",
        "host": "www.example.com",
        "http_url": "https://www.example.com/",
        "enable_http_check": True,
        "enable_ping": ping,
        "enable_tcp_connect": bool(tcp_ports),
        "interval_seconds": 300,
        "sla_enabled": sla,
        "sla_availability_pct": 99.9,
        "sla_response_time_ms": 400,
        **(target_extra or {}),
    }
    port_data = {443 + k: http for k in range(tcp_ports)} or None
    data = ReportData(
        target=target,
        from_date=start,
        to_date=end,
        http_data=http,
        uptime_data=up,
        ping_data=http if ping else [],
        tcp_data=[],
        cert_data=[],
        status_code_data=codes,
        tcp_port_data=port_data,
    )
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: _info())
    b = PDFReportBuilder(data, size)
    monkeypatch.setattr(b, "_get_whatweb_data", lambda: None)
    monkeypatch.setattr(b, "_get_ssl_data", lambda: ssl)
    hop_list = list(hops)
    monkeypatch.setattr(b, "_get_route_data", lambda **_k: (hop_list, [], False))
    return b


CHAPTERS = ["Cover", "Technical Summary", "Network Path", "Availability & Performance", "Downtime Log"]


def _assert_chapter_layout(b: PDFReportBuilder) -> None:
    from reportlab.platypus import PageBreak

    # Without incidents the downtime summary shares the performance page instead of owning one
    merged = not b.downtime_events
    names = CHAPTERS[:-1] if merged else CHAPTERS
    pages = [b.chapter_pages[name] for name in names]
    assert pages[0] == 1
    assert pages == sorted(set(pages)), pages  # strictly increasing: no two chapters share a page
    if merged:
        assert b.chapter_pages["Downtime Log"] == b.page_count or b.chapter_pages["Downtime Log"] >= pages[-1]
    b._assemble()  # build() consumes the flowables; re-compose them for the structural checks
    heads = [i for i, el in enumerate(b.elements) if getattr(el, "chapter_name", None) in names[1:]]
    assert len(heads) == len(names) - 1
    assert all(isinstance(b.elements[i - 1], PageBreak) for i in heads)
    assert not any(
        isinstance(a, PageBreak) and isinstance(c, PageBreak) for a, c in zip(b.elements, b.elements[1:], strict=False)
    )


def test_cover_is_page_one_and_each_chapter_starts_on_a_new_page(monkeypatch):
    b = _full_builder(
        monkeypatch, hops=[_hop(i) for i in range(1, 6)], ssl=_tls_result(), fail=(10, 11), sla=True, ping=True
    )
    assert b.build().startswith(b"%PDF")
    _assert_chapter_layout(b)
    assert b.chapter_pages["Technical Summary"] == 2  # the cover fits on exactly one page


@pytest.mark.parametrize("size", ["a4", "letter"])
def test_a4_and_letter_keep_the_chapter_layout(monkeypatch, size):
    b = _full_builder(
        monkeypatch,
        size=size,
        hops=[_hop(i, long=True) for i in range(1, 30)],
        ssl=_tls_result(),
        fail=(3, 4),
        sla=True,
        ping=True,
        tcp_ports=3,
    )
    pdf = b.build()
    box = re.search(rb"/MediaBox \[ ?0 0 ([\d.]+) ([\d.]+)", pdf)
    assert (round(float(box.group(1))), round(float(box.group(2)))) == ((612, 792) if size == "letter" else (595, 842))
    _assert_chapter_layout(b)
    assert b.chapter_pages["Technical Summary"] == 2  # SLA table included, cover still one page


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"ping": True},
        {"tcp_ports": 1},
        {"ping": True, "tcp_ports": 8, "sla": True},
        {"nodata": True},
        {"target_extra": {"http_url": "http://plain.example/", "enable_ping": False}},
    ],
)
def test_varying_enabled_checks_build_without_layout_errors(monkeypatch, kw):
    hops = [] if kw.get("nodata") else [_hop(i) for i in range(1, 4)]
    b = _full_builder(monkeypatch, hops=hops, ssl=None if kw.get("nodata") else _tls_result(), **kw)
    assert b.build().startswith(b"%PDF")
    _assert_chapter_layout(b)


def _hop_table(b: PDFReportBuilder):
    return next(
        el
        for el in b.elements
        if getattr(el, "repeatRows", 0)
        and len(getattr(el, "_cellvalues", [])) > 1
        and getattr(el._cellvalues[0][0], "text", "") == "Hop"
    )


def test_traceroute_table_starts_on_a_new_page(monkeypatch):
    from reportlab.platypus import PageBreak

    b = _full_builder(monkeypatch, hops=[_hop(1)], ssl=_tls_result())
    b._assemble()
    table = _hop_table(b)
    table_index = next(i for i, element in enumerate(b.elements) if element is table)

    assert isinstance(b.elements[table_index - 1], PageBreak)


def _split_all(table, avail_h=150 * 2.83465):
    """Split a table like the frame does; returns the parts that would land on successive pages."""
    width = sum(table._colWidths)
    parts, rest = [], table
    for _ in range(50):
        rest.wrap(width, avail_h)
        if rest._height <= avail_h:
            parts.append(rest)
            return parts
        split = rest.split(width, avail_h)
        assert len(split) == 2, "table must split by rows"
        parts.append(split[0])
        rest = split[1]
    raise AssertionError("table did not terminate")


def _first_cell_text(table) -> str:
    cell = table._cellvalues[0][0]
    while isinstance(cell, tuple | list):  # split parts wrap cells in _ExpandedCellTuple
        cell = cell[0]
    return cell.text


@pytest.mark.parametrize("n_hops", [45, 60])
def test_long_traceroute_repeats_header_and_keeps_every_row(monkeypatch, n_hops):
    b = _full_builder(monkeypatch, hops=[_hop(i, long=True) for i in range(1, n_hops + 1)], ssl=_tls_result())
    b._assemble()
    table = _hop_table(b)
    assert table.repeatRows == 1
    parts = _split_all(table)
    assert len(parts) >= 2
    assert all(_first_cell_text(p) == "Hop" for p in parts)  # header on every page
    assert all(len(p._cellvalues) > 1 for p in parts)  # header never stands alone
    assert sum(len(p._cellvalues) - 1 for p in parts) == n_hops + 1  # every hop plus the footnote row


def test_long_downtime_log_repeats_header_and_keeps_every_event(monkeypatch):
    b = _full_builder(monkeypatch, hops=[_hop(1)], ssl=_tls_result(), fail=tuple(range(2, 118, 2)))
    b._assemble()
    table = next(el for el in b.elements if getattr(el, "repeatRows", 0) and _first_cell_text(el) == "#")
    n_events = len(b.downtime_events)
    assert n_events > 50 and table.repeatRows == 1
    parts = _split_all(table)
    assert len(parts) >= 2 and all(_first_cell_text(p) == "#" for p in parts)
    assert sum(len(p._cellvalues) - 1 for p in parts) == n_events + 1  # every event plus the note row


def test_long_ipv6_hostnames_and_tls_values_are_wrapped_not_shortened(monkeypatch):
    v6 = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
    host = "ae-1-" + "very-long-label-" * 6 + "example.com"
    hop = {**_hop(1), "ip": v6, "hostname": host}
    base = _tls_result()
    long_dn = "CN=" + "x" * 150 + ".example.com"
    cert = dataclasses.replace(
        base.certificate,
        subject=long_dn,
        issuer="O=" + "Issuer " * 30,
        san=tuple(f"{'s' * 60}{i}.example" for i in range(25)),
    )
    b = _full_builder(monkeypatch, hops=[hop], ssl=dataclasses.replace(base, certificate=cert))
    b._assemble()
    out: list[str] = []
    for el in b.elements:
        _walk_text(el, out)
    text = " | ".join(out)
    assert v6 in text and host in text and long_dn in text and "s" * 60 + "24.example" in text
    assert "..." not in text and "…" not in text.replace("—", "")
    assert _hop_table(b)._cellvalues[1][1].style.wordWrap == "CJK"


def test_missing_measurements_are_never_rendered_as_zero_or_operational(monkeypatch):
    b = _full_builder(monkeypatch, nodata=True, hops=[], ssl=None, sla=True)
    assert b.kpis.uptime_percent is None and b.kpis.avg_response_ms is None and b.kpis.max_response_ms is None
    b._assemble()
    out: list[str] = []
    for el in b.elements:
        _walk_text(el, out)
    text = " | ".join(out)
    assert "No data" in text and "n/a" in text
    for forbidden in ("Operational", "0%", "0 ms", "Compliant", "No downtime recorded"):
        assert forbidden not in text.replace("Non-compliant", ""), forbidden
    assert "No availability data" in text


def test_availability_never_rounds_up_to_100_when_checks_failed():
    assert pdf_report._availability_pct(29999, 30000) == 99.99
    assert pdf_report._fmt_pct(99.99) == "99.99%" and pdf_report._fmt_pct(100.0) == "100%"


def test_incomplete_tls_scan_is_flagged_and_unknown_never_shows_as_pass(monkeypatch):
    unknown = Check.unknown()
    result = _tls_result(
        status=ScanStatus.PARTIAL, heartbleed=unknown, command_errors={"heartbleed": "CONNECTIVITY_ISSUE"}
    )
    b = _full_builder(monkeypatch, hops=[_hop(1)], ssl=result)
    b._assemble()
    table = next(el for el in b.elements if "Heartbleed" in " ".join(_cells(el)))
    row = next(r for r in table._cellvalues if getattr(r[0], "text", "") == "Heartbleed")
    assert row[2].text == "NOT TESTED"


def _cells(el) -> list[str]:
    out: list[str] = []
    _walk_text(el, out)
    return out


def test_cover_stays_on_one_letter_page_with_all_slas_and_a_very_long_name(monkeypatch):
    b = _full_builder(
        monkeypatch,
        size="letter",
        hops=[_hop(1)],
        ssl=_tls_result(),
        fail=(5,),
        sla=True,
        ping=True,
        target_extra={"name": "Customer portal " * 8, "sla_ping_latency_ms": 60},
    )
    b.build()
    _assert_chapter_layout(b)
    assert b.chapter_pages["Technical Summary"] == 2


_LONG_SITE_TITLE = "Internationale Kundenportal-Plattform für Verträge, Rechnungen und Support " * 3


@pytest.mark.parametrize("size", ["a4", "letter"])
@pytest.mark.parametrize(
    ("name", "site_title"),
    [
        ("Customer portal " * 8, _LONG_SITE_TITLE),  # worst case: wrapped name, 2-line title
        ("Customer portal " * 8, None),
        ("Example", _LONG_SITE_TITLE),
        ("Example", "Short title"),
        ("Example", None),
    ],
)
def test_cover_with_website_title_stays_on_one_page(monkeypatch, size, name, site_title):
    b = _full_builder(
        monkeypatch,
        size=size,
        hops=[_hop(1)],
        ssl=_tls_result(),
        fail=(5,),  # 6 % coverage: banner uses the longest "Insufficient data" text
        sla=True,
        ping=True,
        tcp_ports=3,
        target_extra={"name": name, "sla_ping_latency_ms": 60},
    )
    monkeypatch.setattr(b, "_get_whatweb_data", lambda: {"title": site_title} if site_title else None)
    b.build()
    _assert_no_blank_pages(b)
    assert b.chapter_pages["Technical Summary"] == 2  # first chapter starts right after a one-page cover


def test_odd_measurement_values_and_naive_dates_do_not_abort_the_report(monkeypatch):
    b = _full_builder(monkeypatch, hops=[_hop(1)], ssl=_tls_result(), ping=True, tcp_ports=1)
    bad = [None, float("nan"), "abc", float("inf"), True]
    for series in (b.report_data.http_data, b.report_data.ping_data, b.report_data.status_code_data):
        for i, value in enumerate(bad):
            series[i] = {**series[i], "value": value}
    naive = dataclasses.replace(
        b.report_data,
        from_date=b.report_data.from_date.replace(tzinfo=None),
        to_date=b.report_data.to_date.replace(tzinfo=None),
    )
    b2 = PDFReportBuilder(naive)
    monkeypatch.setattr(b2, "_get_whatweb_data", lambda: None)
    monkeypatch.setattr(b2, "_get_ssl_data", lambda: None)
    monkeypatch.setattr(b2, "_get_route_data", lambda **_k: ([], [], False))
    assert b.build().startswith(b"%PDF") and b2.build().startswith(b"%PDF")
    assert b.kpis.avg_response_ms is not None  # valid points still count, bad ones are skipped


def test_ongoing_outage_duration_is_measured_to_the_last_sample_not_the_period_end(monkeypatch):
    b = _full_builder(monkeypatch, hops=[], ssl=None, fail=(118, 119))
    b._assemble()
    assert b.downtime_events[-1].end is None
    out: list[str] = []
    for el in b.elements:
        _walk_text(el, out)
    text = " | ".join(out)
    assert "ONGOING" in text and "≥ " in text
    assert b._last_uptime_sample() < b.report_data.to_date


# ─── Blank pages: a chapter that ends exactly at the page end must not leave an empty page ─────


def _no_charts(monkeypatch):
    """Skip the slow matplotlib rendering; page flow is driven by the tables and text."""
    monkeypatch.setattr(pdf_report, "_create_uptime_chart", lambda *a, **k: None)
    monkeypatch.setattr(pdf_report, "_create_response_time_chart", lambda *a, **k: None)
    monkeypatch.setattr(pdf_report, "_create_status_code_chart", lambda *a, **k: None)


def _assert_no_blank_pages(b: PDFReportBuilder) -> None:
    b.build()
    blank = sorted(set(range(1, b.page_count + 1)) - b.content_pages)
    assert not blank, f"blank pages {blank} of {b.page_count}"
    _assert_chapter_layout(b)


@pytest.mark.parametrize("size", ["a4", "letter"])
@pytest.mark.parametrize(
    "n_hops", [0, 1, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 30, 45]
)
def test_no_blank_page_for_any_traceroute_length(monkeypatch, size, n_hops):
    _no_charts(monkeypatch)
    hops = [_hop(i, long=i % 2 == 0) for i in range(1, n_hops + 1)]
    _assert_no_blank_pages(_full_builder(monkeypatch, size=size, hops=hops, ssl=_tls_result(), fail=(4,)))


@pytest.mark.parametrize("size", ["a4", "letter"])
@pytest.mark.parametrize("n_events", [0, 1, 2, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 59])
def test_no_blank_page_for_any_downtime_log_length(monkeypatch, size, n_events):
    _no_charts(monkeypatch)
    fail = tuple(range(1, 2 * n_events, 2))  # every other check fails: one event per failure
    b = _full_builder(monkeypatch, size=size, hops=[_hop(1)], ssl=_tls_result(), fail=fail)
    _assert_no_blank_pages(b)
    assert len(b.downtime_events) == n_events


@pytest.mark.parametrize("size", ["a4", "letter"])
@pytest.mark.parametrize("chapter", ["Technical Summary", "Network Path", "Availability & Performance", "Downtime Log"])
@pytest.mark.parametrize("slack", [0.0, 0.01, 1.0, 6.0])
def test_chapter_filling_its_page_exactly_does_not_add_a_blank_page(monkeypatch, size, chapter, slack):
    from reportlab.platypus import PageBreak, Spacer

    _no_charts(monkeypatch)
    b = _full_builder(monkeypatch, size=size, hops=[_hop(1)], ssl=None, fail=(4,))
    b._assemble()
    start = next(i for i, el in enumerate(b.elements) if getattr(el, "chapter_name", None) == chapter)
    end = next((i for i in range(start, len(b.elements)) if isinstance(b.elements[i], PageBreak)), len(b.elements))
    frame_h = b.height - 2 * b.margin - 12
    used = 0.0
    for el in b.elements[start : start + 3]:  # chapter title, subtitle, rule
        _w, h = el.wrap(b.content_width, 1e6)
        used += h + el.getSpaceAfter() + (el.getSpaceBefore() if used else 0)
    b.elements[start + 3 : end] = [Spacer(1, frame_h - used - slack)]  # chapter body fills the page
    doctored = b.elements
    b._assemble = lambda: setattr(b, "elements", doctored)  # keep the doctored chapter
    b.build()
    blank = sorted(set(range(1, b.page_count + 1)) - b.content_pages)
    assert not blank, f"{chapter}: blank pages {blank} of {b.page_count}"
    pages = [b.chapter_pages[name] for name in CHAPTERS]
    k = CHAPTERS.index(chapter)
    expected_page_span = 2 if chapter == "Network Path" else 1
    assert pages[k + 1] - pages[k] == expected_page_span if k + 1 < len(CHAPTERS) else b.page_count == pages[k]


@pytest.mark.parametrize("size", ["a4", "letter"])
def test_cover_with_maximal_sla_rows_and_chapters_without_data_have_no_blank_pages(monkeypatch, size):
    full = _full_builder(
        monkeypatch,
        size=size,
        hops=[_hop(1)],
        ssl=_tls_result(),
        fail=(5,),
        sla=True,
        ping=True,
        target_extra={"name": "Customer portal " * 8, "sla_ping_latency_ms": 60},
    )
    _assert_no_blank_pages(full)
    assert full.chapter_pages["Technical Summary"] == 2
    empty = _full_builder(monkeypatch, size=size, nodata=True, hops=[], ssl=None, sla=True)
    _assert_no_blank_pages(empty)


# ─── Data semantics: estimates, defaults, thresholds, incomplete routes ────────────────────────


def _all_text(b: PDFReportBuilder) -> str:
    b._assemble()
    return " | ".join(_cells_of(b.elements))


def _cells_of(elements) -> list[str]:
    out: list[str] = []
    for el in elements:
        _walk_text(el, out)
    return out


def test_downtime_is_labelled_as_an_estimate_everywhere(monkeypatch):
    b = _full_builder(monkeypatch, hops=[_hop(1)], ssl=None, fail=(10, 11, 40))
    text = _all_text(b)
    assert "ESTIMATED DOWNTIME" in text and "≈ " in text
    assert "Est. duration" in text and "Estimated downtime:" in text
    assert "estimated downtime is about" not in text
    assert "| DOWNTIME" not in f"| {text}" and "| Duration" not in f"| {text}"
    # The series is thinned to 5040 s, beyond the 3 x 300 s gap boundary. Each failure
    # proves one check interval; the unmeasured time between them stays UNKNOWN.
    assert b.kpis.downtime_minutes == 3 * 5


FULL_COVERAGE = {"interval_seconds": 5040}  # 120 checks fill the 7-day series


@pytest.mark.parametrize(
    ("fails", "label", "kind"),
    [
        (0, "Operational", "success"),
        (1, "Operational", "success"),
        (2, "Degraded", "warning"),
        (7, "Critical", "danger"),
    ],
)
def test_status_without_sla_uses_the_fixed_uptime_limits(monkeypatch, fails, label, kind):
    # 120 checks: 1 failure = 99.17 %, 2 = 98.33 %, 7 = 94.17 %
    b = _full_builder(monkeypatch, hops=[], ssl=None, fail=tuple(range(fails)), target_extra=FULL_COVERAGE)
    assert b._status_info()[0::2] == (label, kind)


@pytest.mark.parametrize(
    ("sla_pct", "label", "kind"),
    [(99.0, "Operational", "success"), (99.9, "Below SLA", "danger")],
)
def test_status_with_sla_follows_the_goal_without_warn_band(monkeypatch, sla_pct, label, kind):
    extra = {**FULL_COVERAGE, "sla_availability_pct": sla_pct}
    b = _full_builder(monkeypatch, hops=[], ssl=None, fail=(0,), sla=True, target_extra=extra)  # 99.17 %
    assert b._status_info()[0::2] == (label, kind)


def test_low_coverage_never_shows_a_green_operational_status(monkeypatch):
    b = _full_builder(monkeypatch, hops=[], ssl=None)  # 120 checks of 300 s in 7 days: 6 % coverage
    label, detail, kind = b._status_info()
    assert (label, kind) == ("Insufficient data", "unknown")
    assert "of recorded checks successful" in detail and "remaining 94.0%" in detail
    text = _all_text(b)
    assert "RECORDED CHECKS SUCCESSFUL" in text and "120/120 measurements · data coverage 6%" in text
    assert "ESTIMATED DOWNTIME" in text and "None detected" not in text
    assert "in observed checks" in text and "≈ 0 min" not in text
    assert "AVAILABILITY" not in text


def test_low_coverage_does_not_hide_failed_checks(monkeypatch):
    b = _full_builder(monkeypatch, hops=[], ssl=None, fail=tuple(range(10)))  # 8.3 % failed, 6 % coverage
    assert b._status_info()[0::2] == ("Critical", "danger")
    assert "Low data coverage" in b._status_info()[1]


def test_sla_row_is_not_compliant_on_thin_data(monkeypatch):
    b = _full_builder(
        monkeypatch,
        hops=[],
        ssl=None,
        sla=True,
        target_extra={"sla_availability_pct": 99.0, "sla_response_time_ms": None},
    )
    text = _all_text(b)
    assert "Not enough data" in text and ">Compliant<" not in text


ALLOWED_UPTIME_CELLS = {"uptime &amp; performance report", "uptime monitoring report"}


def test_full_coverage_keeps_availability_wording(monkeypatch):
    b = _full_builder(
        monkeypatch,
        hops=[],
        ssl=None,
        sla=True,
        fail=(0,),
        target_extra={**FULL_COVERAGE, "sla_availability_pct": 99.0},
    )  # 99.17 % with full coverage
    assert not b._is_low_coverage()
    text = _all_text(b)
    assert "Availability" in text.split(" | ") and "Check success rate" not in text
    assert "of recorded checks" not in text and "RECORDED CHECKS" not in text
    assert "Compliant" in text and "Not enough data" not in text and "+0.17 pp" in text
    assert "checks succeeded" not in text  # the cover summary sentence under the KPI cards is gone


def test_perfect_success_rate_on_thin_coverage_never_claims_availability(monkeypatch):
    # 120 checks of 85 s in 7 days: about 1.7 % coverage, every recorded check succeeded
    b = _full_builder(
        monkeypatch,
        hops=[],
        ssl=None,
        sla=True,
        target_extra={"interval_seconds": 85, "sla_availability_pct": 99.0},
    )
    assert b.kpis.coverage_pct < 2 and b.kpis.uptime_percent == 100.0
    cells = _all_text(b).split(" | ")
    text = " | ".join(cells)
    assert "RECORDED CHECKS SUCCESSFUL" in text and "Not enough data" in text
    assert "Check success rate" in text
    assert "Availability" not in cells and "AVAILABILITY" not in text
    for cell in cells:
        low = cell.lower()
        assert not ("100" in low and "availability" in low and "recorded" not in low), cell
        assert "uptime" not in low or low in ALLOWED_UPTIME_CELLS, cell
    assert "SLA met" not in text


def test_measurements_without_value_are_not_counted_as_checks(monkeypatch):
    b = _full_builder(monkeypatch, hops=[], ssl=None)
    data = [{"ts": "2026-10-21T00:00:00+00:00", "value": v} for v in (1, None, None, 0)]
    b = PDFReportBuilder(dataclasses.replace(b.report_data, uptime_data=data))
    assert (b.kpis.total_checks, b.kpis.up_count, b.kpis.down_count) == (2, 1, 1)
    assert len(b.downtime_events) == 1


def test_unconfigured_sla_defaults_are_marked_as_defaults_on_charts(monkeypatch):
    seen: dict[str, dict] = {}

    def fake(name):
        def _chart(*_a, **kw):
            seen[name] = kw

        return _chart

    monkeypatch.setattr(pdf_report, "_create_response_time_chart", fake("response"))
    monkeypatch.setattr(pdf_report, "_create_ping_chart", fake("ping"))
    monkeypatch.setattr(pdf_report, "_create_uptime_chart", lambda *a, **k: None)
    monkeypatch.setattr(pdf_report, "_create_status_code_chart", lambda *a, **k: None)
    only_availability = {"sla_response_time_ms": None, "sla_ping_latency_ms": None}
    b = _full_builder(monkeypatch, ping=True, sla=True, target_extra=only_availability, hops=[], ssl=None)
    b._assemble()
    assert seen["response"]["sla_threshold_ms"] == 500 and "Default" in seen["response"]["threshold_label"]
    assert seen["ping"]["sla_threshold_ms"] == 100 and "no SLA set" in seen["ping"]["threshold_label"]
    # Configured values win and are labelled as SLA; a disabled SLA draws no threshold at all
    b = _full_builder(monkeypatch, ping=True, sla=True, target_extra={"sla_ping_latency_ms": 60}, hops=[], ssl=None)
    b._assemble()
    assert seen["response"]["sla_threshold_ms"] == 400 and seen["response"]["threshold_label"] is None
    assert seen["ping"]["sla_threshold_ms"] == 60 and seen["ping"]["threshold_label"] is None
    b = _full_builder(monkeypatch, ping=True, sla=False, hops=[], ssl=None)
    b._assemble()
    assert seen["response"]["sla_threshold_ms"] is None and seen["ping"]["sla_threshold_ms"] is None


def test_cover_sla_table_lists_only_configured_targets(monkeypatch):
    extra = {"sla_response_time_ms": None, "sla_ping_latency_ms": None}
    b = _full_builder(monkeypatch, sla=True, ping=True, target_extra=extra, hops=[], ssl=None)
    b._assemble()
    stop = next(i for i, el in enumerate(b.elements) if getattr(el, "chapter_name", None) == "Technical Summary")
    cover = " | ".join(_cells_of(b.elements[:stop]))
    # Thin coverage (6 %): the availability row is labelled as a success rate of recorded checks
    assert "SLA Compliance" in cover and "Check success rate" in cover
    assert "Response Time" not in cover and "Ping Latency" not in cover
    assert "500" not in cover and "100 ms" not in cover


def test_http_status_chart_is_described_as_samples_not_exact_counts(monkeypatch):
    b = _full_builder(monkeypatch, hops=[], ssl=None)
    text = _all_text(b)
    assert "stored samples" in text and "not exact per-check counts" in text


def test_pdf_http_status_colors_match_dashboard_light_palette():
    source = (Path(__file__).resolve().parents[1] / "app/static/js/charts/combined-chart.js").read_text(
        encoding="utf-8"
    )
    light = re.search(r"light:\s*\{(.*?)\}\s*,\s*dark:", source, re.DOTALL)
    assert light is not None
    palette = {
        quoted or plain: color
        for quoted, plain, color in re.findall(r'(?:"([^"]+)"|([a-z]+)):\s*"(#[0-9a-fA-F]{6})"', light.group(1))
    }
    for status_class, key in {0: "err", 1: "info", 2: "2xx", 3: "3xx", 4: "4xx", 5: "5xx"}.items():
        assert pdf_report._STATUS_CLASS_COLORS[status_class] == palette[key]
    assert pdf_report._STATUS_CLASS_COLORS[3] == pdf_report._STATUS_CLASS_COLORS[2]

    builder = _builder({"name": "status-color"})
    builder.report_data.status_code_data = [{"value": 301}]
    assert builder._add_status_codes(width=6.7)
    bar = next(
        el for group in builder.elements for el in getattr(group, "_content", []) if isinstance(el, pdf_report.ShareBar)
    )
    assert bar.shares[0][0].hexval().replace("0x", "#") == palette["3xx"]


def _route_builder(monkeypatch, hops, *, host="dest.example", size="a4"):
    b = _full_builder(monkeypatch, hops=[], ssl=None, size=size)
    monkeypatch.setattr(
        b,
        "_get_route_data",
        lambda *, max_hops=pdf_report._ROUTE_MAX_HOPS: PDFReportBuilder._get_route_data(b, max_hops=max_hops),
    )
    monkeypatch.setattr(b, "_get_traceroute_host", lambda: host)
    monkeypatch.setattr(pdf_report, "_run_traceroute_with_interpolation", lambda *_a, **_k: (list(hops), [], False))
    return b


def _synthetic_destination(n):
    return {
        "hop": n + 1,
        "hop_display": str(n + 1),
        "ip": "203.0.113.9",
        "status": "partial",
        "_synthetic_destination": True,
    }


def test_route_cut_at_the_hop_limit_is_flagged_incomplete(monkeypatch):
    limit = pdf_report._ROUTE_MAX_HOPS
    hops = [_hop(i) for i in range(1, limit - 1)] + [_hop(i, status="timeout") for i in (limit - 1, limit)]
    b = _route_builder(monkeypatch, [*hops, _synthetic_destination(limit)])
    text = _all_text(b)
    assert text.count(f"Route incomplete – {limit}-hop limit reached") == 2  # chapter subtitle and warning note
    assert "not shown completely" in text and "NO REPLY" in text


def test_route_reaching_its_destination_is_not_flagged_incomplete(monkeypatch):
    b = _route_builder(monkeypatch, [_hop(i) for i in range(1, 9)])
    assert "Route incomplete" not in _all_text(b)
    # a destination that stayed silent before the limit is not a limit problem either
    b = _route_builder(monkeypatch, [_hop(i) for i in range(1, 9)] + [_synthetic_destination(8)])
    assert "Route incomplete" not in _all_text(b)


def test_route_ending_in_silent_hops_at_the_limit_is_incomplete_without_a_resolved_destination(monkeypatch):
    limit = pdf_report._ROUTE_MAX_HOPS
    hops = [_hop(i) for i in range(1, limit - 1)] + [_hop(i, status="timeout") for i in (limit - 1, limit)]
    text = _all_text(_route_builder(monkeypatch, hops))
    assert f"Route incomplete – {limit}-hop limit reached" in text
    short = [_hop(i) for i in range(1, 9)] + [_hop(i, status="timeout") for i in (9, 10)]
    assert "Route incomplete" not in _all_text(_route_builder(monkeypatch, short))


def _mhop(n, ip="10.0.0.1", lat=1.0, city=None, status="ok", **kw):
    hop = {"hop": n, "ip": ip, "status": status, "latency_ms": {"avg": lat}, **kw}
    if city:
        hop.update(city=city, country="DE")
    return hop


def _sample_route():
    return [
        *[_mhop(i, f"10.0.0.{i}", float(i)) for i in range(1, 5)],
        _mhop(5, city="Dortmund", lat=10.2),
        _mhop(6, city="Frankfurt"),
        _mhop(7, city="Frankfurt"),
        _mhop(8, "*", status="timeout"),
        _mhop(9, city="Frankfurt", lat=26.0),
        _mhop(10, city="Wuppertal"),
        _mhop(11, "*", status="timeout"),
        _mhop(12, "94.79.165.210", 30.5, city="Ratingen"),
    ]


def test_route_entries_group_consecutive_hops_and_keep_silent_hops_single():
    model = pdf_report._build_route_entries(
        _sample_route(), "94.79.165.210", "Ratingen, DE · approximate GeoIP location"
    )
    spans = [e["span"] for e in model["entries"]]
    assert spans == ["1–4", "5", "6–7", "8", "9", "10", "11", "12"]
    assert model["header"] == "NETWORK PATH · 12 HOPS · 10 REPLIES · DESTINATION REACHED"
    states = [e["state"] for e in model["entries"]]
    assert states == ["measured"] * 3 + ["noreply", "measured", "measured", "noreply", "destination"]
    first, silent, dest = model["entries"][0], model["entries"][3], model["entries"][-1]
    assert first["title"] == "4 measured hops" and "Location unknown" in first["detail"][0]
    assert "first observed hop" in first["detail"][0]
    assert silent["title"] == "NO REPLY" and "BLOCKED" not in str(model).upper()
    assert dest["detail"][-1].startswith("Ratingen, DE · approximate GeoIP location")


def test_route_entries_keep_known_country_for_consecutive_country_only_hops():
    hops = [
        _mhop(1),
        _mhop(2, "198.51.100.2", country="DE"),
        _mhop(3, "198.51.100.3", country="DE"),
        _mhop(4, "198.51.100.4", city="Ratingen"),
    ]

    entries = pdf_report._build_route_entries(hops, "198.51.100.4")["entries"]

    assert [(entry["span"], entry["title"]) for entry in entries] == [
        ("1", "First observed hop"),
        ("2–3", "DE (country only)"),
        ("4", "Destination"),
    ]
    assert entries[1]["flag"] == "DE"
    assert entries[1]["detail"] == ["2 hops measured"]


def test_route_groups_never_span_a_gap_in_the_hop_numbers():
    hops = [_mhop(1), _mhop(2), _mhop(5), _mhop(6), _mhop(7, "94.79.165.210")]
    spans = [e["span"] for e in pdf_report._build_route_entries(hops, "94.79.165.210")["entries"]]
    assert spans == ["1–2", "5–6", "7"]


def test_unreached_destination_is_marked_and_never_shown_as_reached():
    hops = [_mhop(1), _mhop(2, city="Dortmund"), {**_synthetic_destination(2), "ip": "203.0.113.9"}]
    model = pdf_report._build_route_entries(hops, "203.0.113.9")
    assert model["reached"] == "unreached" and "DESTINATION NOT REACHED" in model["header"]
    last = model["entries"][-1]
    assert last["state"] == "unreached" and "NOT REACHED" in last["title"] and last["right"] == "NO REPLY"
    assert "11 HOPS" not in model["header"] and "2 REPLIES" in model["header"]


def test_destination_without_known_address_is_unconfirmed_not_unreached():
    model = pdf_report._build_route_entries([_mhop(1), _mhop(2, city="Dortmund")], None)
    assert model["reached"] == "unconfirmed" and "NOT CONFIRMED" in model["header"]
    assert all(e["state"] != "unreached" for e in model["entries"])


@pytest.mark.parametrize("size", ["a4", "letter"])
@pytest.mark.parametrize("count", [15, 20, 30])
def test_long_ungroupable_route_splits_over_pages_and_keeps_every_hop(monkeypatch, size, count):
    hops = [_mhop(i, f"10.0.{i}.1", float(i), city=f"City{i}") for i in range(1, count + 1)]
    drawn = []
    original = pdf_report.RouteDiagram.draw

    def record(self):
        drawn.append(self)
        original(self)

    monkeypatch.setattr(pdf_report.RouteDiagram, "draw", record)
    builder = _route_builder(monkeypatch, hops, size=size)
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: _info(ip="-"))  # no destination address
    pdf = builder.build()  # a LayoutError would fail the job here
    assert pdf.startswith(b"%PDF")
    spans = [e["span"] for part in drawn for e in part.entries]
    assert spans == [str(i) for i in range(1, count + 1)]  # order kept, nothing dropped or repeated
    if count >= 30:
        assert len(drawn) > 1
    for before, after in itertools.pairwise(drawn):
        assert before.continues == after.entries[0]["span"] and after.continued
    assert drawn[-1].continues is None and not drawn[0].continued


def test_route_ending_in_silent_hops_is_not_truncated_in_the_pdf(monkeypatch):
    hops = [_hop(i) for i in range(1, 9)] + [_hop(i, status="timeout") for i in (9, 10)]
    b = _route_builder(monkeypatch, hops)
    assert [h["hop"] for h in b._get_route_data()[0]][-1] == 10
    spans = [e["span"] for e in pdf_report._build_route_entries(hops, None)["entries"]]
    assert spans[-1] in ("10", "9–10")


def test_single_first_hop_is_labelled_first_observed_hop():
    entries = pdf_report._build_route_entries([_mhop(1, "172.30.176.1"), _mhop(2, city="Dortmund")], "9.9.9.9")[
        "entries"
    ]
    assert entries[0]["title"] == "First observed hop" and "START" not in str(entries).upper()


def test_route_chapter_uses_measurement_wording_and_geoip_footnote(monkeypatch):
    b = _route_builder(monkeypatch, _sample_route())
    monkeypatch.setattr(
        pdf_report,
        "lookup_provider_info",
        lambda host: _info(ip="94.79.165.210", location="Ratingen", location_accuracy="100 km radius"),
    )
    text = _all_text(b)
    assert "NO REPLY" in text and "BLOCKED" not in text.upper() and "ICMP" not in text
    assert "Location unknown" in text and "first observed hop" in text
    assert "Locations are GeoIP estimates; schematic, not geographically scaled." in text
    assert "Ratingen, DE (approx.)" in text
    b._assemble()
    diagram = next(el for el in b.elements if isinstance(el, pdf_report.RouteDiagram))
    assert "100 km radius" in str(diagram.entries[-1]["detail"])


def test_provider_location_is_marked_approximate_with_radius(monkeypatch):
    b = _full_builder(monkeypatch, hops=[], ssl=None)
    monkeypatch.setattr(
        pdf_report, "lookup_provider_info", lambda host: _info(location="Ratingen", location_accuracy="100 km radius")
    )
    b.__dict__.pop("_provider_cache", None)  # drop the lookup cached by the builder fixture
    assert b._destination_place() == "Ratingen, DE · approximate GeoIP location · 100 km radius"
    assert b._destination_place(with_accuracy=False) == "Ratingen, DE · approximate GeoIP location"


def test_merged_downtime_summary_without_incidents_and_own_page_with_incidents(monkeypatch):
    quiet = _full_builder(monkeypatch, hops=[_hop(1)], ssl=None, target_extra=FULL_COVERAGE)
    quiet.build()
    assert quiet.chapter_pages["Downtime Log"] == quiet.chapter_pages["Availability & Performance"]
    assert "No downtime recorded" in _all_text(quiet)
    thin = _full_builder(monkeypatch, hops=[_hop(1)], ssl=None)
    assert "No downtime detected in observed checks" in _all_text(thin)
    busy = _full_builder(monkeypatch, hops=[_hop(1)], ssl=None, fail=(4,))
    busy.build()
    assert busy.chapter_pages["Downtime Log"] == busy.chapter_pages["Availability & Performance"] + 1


def test_tls_leads_with_versions_and_findings_and_names_the_scan_time(monkeypatch):
    b = _builder({"http_url": "https://host.example"})
    b._ssl_scanned_at = datetime(2026, 10, 30, 12, 5, tzinfo=UTC)
    monkeypatch.setattr(b, "_get_ssl_data", lambda: _tls_result())
    b._add_ssl_summary()
    text = _texts(b)
    assert "TLS Scan Time" in text and "2026-10-30 12:05" in text and "not part of the reporting period" in text
    assert text.index("Supported Versions") < text.index("Heartbleed") < text.index("Cipher Suite Example")
    assert text.index("Cipher Suite Example") < text.index("Cipher Suites by Protocol (technical detail)")
    assert "Preferred Cipher" not in text


def test_report_declares_title_and_language(monkeypatch):
    pdf = _full_builder(monkeypatch, hops=[], ssl=None).build()
    assert b"/Lang (en)" in pdf and b"/DisplayDocTitle true" in pdf


def test_coverage_bar_uses_the_configured_interval_not_the_spacing_of_measurements():
    start = datetime(2026, 10, 21, tzinfo=UTC)
    end = start + timedelta(days=1)
    offsets = [0, 55, 61, 125, 400, 3600, 7300, 7360, 20000, 40000, 40030, 70000]  # irregular, in minutes*1s
    stamps = [start + timedelta(seconds=o) for o in offsets]
    blocks = pdf_report._coverage_blocks(stamps, timedelta(seconds=60), start, end)
    drawn_share = sum(w for _, w in blocks) / (mdates.date2num(end) - mdates.date2num(start)) * 100
    data = ReportData(
        target={"interval_seconds": 60},
        from_date=start,
        to_date=end,
        http_data=[],
        uptime_data=[{"ts": t.isoformat(), "value": 1} for t in stamps],
        ping_data=[],
        tcp_data=[],
        cert_data=[],
        status_code_data=[],
    )
    kpi = pdf_report._compute_kpis(data).coverage_pct
    assert abs(drawn_share - kpi) < 0.5  # overlapping checks merge, so the bar never exceeds the figure by much
    assert len(blocks) > 1  # gaps stay gaps
    hourly = [start + timedelta(hours=h) for h in range(25)]
    one_day = pdf_report._coverage_blocks(hourly[:24], timedelta(seconds=60), start, end)
    assert sum(w for _, w in one_day) / (mdates.date2num(end) - mdates.date2num(start)) < 0.03
    png = pdf_report._create_coverage_bar(
        [{"ts": t.isoformat(), "value": 1} for t in hourly[:24]], start, end, interval_seconds=60
    )
    assert png and png.startswith(b"\x89PNG")


def test_coverage_indicator_and_low_coverage_caveat():
    from app.services.pdf_report import LOW_COVERAGE_PCT, _coverage_pct

    now = datetime.now(UTC)
    data = ReportData(
        target={"interval_seconds": 60},
        from_date=now - timedelta(hours=10),
        to_date=now,
        http_data=[],
        uptime_data=[],
        ping_data=[],
        tcp_data=[],
        cert_data=[],
        status_code_data=[],
    )
    assert _coverage_pct(data, 600, 60) == 100.0  # 600 one-minute checks fill 10 h
    assert _coverage_pct(data, 300, 60) == 50.0
    assert _coverage_pct(data, 3000, 60) == 100.0  # capped
    assert LOW_COVERAGE_PCT > 50.0

    b = PDFReportBuilder(data)
    b.kpis.uptime_percent, b.kpis.coverage_pct = 100.0, LOW_COVERAGE_PCT - 0.1
    assert b._is_low_coverage() and b._effective_level() == "none"
    b.kpis.coverage_pct = LOW_COVERAGE_PCT
    assert not b._is_low_coverage() and b._effective_level() == "ok"


def test_empty_route_is_cached_so_the_trace_runs_once(monkeypatch):
    b = _route_builder(monkeypatch, [])
    calls = []

    def _silent_route(*_a, **_k):
        calls.append(1)
        return [], [], False

    monkeypatch.setattr(pdf_report, "_run_traceroute_with_interpolation", _silent_route)
    assert b._get_route_data() == ([], [], False)
    assert b._get_route_data() == ([], [], False)
    assert len(calls) == 1


@pytest.mark.parametrize(("location", "expected"), [("Berlin", "Berlin, DE"), ("Berlin, DE", "Berlin, DE")])
def test_destination_place_repeats_no_country_code(monkeypatch, location, expected):
    monkeypatch.setattr(pdf_report, "lookup_provider_info", lambda host: _info(location=location))
    text = _builder({"http_url": "https://example.com/"})._destination_place()
    assert text.startswith(f"{expected} · approximate GeoIP location") and ", DE, DE" not in text


# ─── Cover: website title subheadline ──────────────────────────────────────────────────────────
def _cover(monkeypatch, title_data, target=None):
    b = _builder(target or {"name": "example.com", "http_url": "https://example.com/"})
    monkeypatch.setattr(b, "_get_whatweb_data", lambda: title_data)
    b._build_cover_page()
    return b


def _cover_texts(b) -> list[str]:
    return [el.text for el in b.elements if hasattr(el, "text") and isinstance(el.text, str)]


def test_cover_site_title_sits_between_name_and_report_title(monkeypatch):
    b = _cover(monkeypatch, {"title": "  Welcome\n to  <b>Example</b> & Co "})
    texts = _cover_texts(b)
    name_i, report_i = texts.index("example.com"), texts.index("Uptime &amp; Performance Report")
    assert texts[name_i + 1] == "Welcome to &lt;b&gt;Example&lt;/b&gt; &amp; Co" and name_i + 1 < report_i
    assert _render(b).startswith(b"%PDF")


@pytest.mark.parametrize(
    "data", [None, {}, {"title": None}, {"title": ""}, {"title": " \n\t "}, {"title": "\u202e\u200b"}]
)
def test_cover_without_title_has_unchanged_flowables(monkeypatch, data):
    with_data = _cover(monkeypatch, data)
    empty = _cover(monkeypatch, None)
    assert [type(e) for e in with_data.elements] == [type(e) for e in empty.elements]
    assert len(_cover_texts(with_data)) == len(_cover_texts(empty))


def test_cover_without_http_url_has_no_subheadline():
    b = _builder({"name": "host", "ping_host": "192.0.2.1"})
    b._build_cover_page()
    assert not any(getattr(getattr(e, "style", None), "name", "") == "CoverSiteTitle" for e in b.elements)


@pytest.mark.parametrize(
    "raw",
    ["日本語のタイトル 😀 Mixed", "x" * 5000, "Ünïcödé — Tïtle", "a\u202eb\x00c\x07d"],
)
def test_cover_site_title_odd_input_renders(monkeypatch, raw):
    b = _cover(monkeypatch, {"title": raw})
    assert _render(b).startswith(b"%PDF")


def test_clean_site_title_normalises_and_drops_unsupported_glyphs():
    font = pdf_report._get_font()
    clean = pdf_report._clean_site_title
    assert clean("a\u202eb\u200bc  \n d", font) == "abc d"
    assert clean("Hi 😀 日本", font) == "Hi"
    assert clean(None, font) == "" and clean(42, font) == ""
    long = clean("w" * 500, font)
    assert len(long) == pdf_report._SITE_TITLE_MAX_LEN and long.endswith("…")


# --- country flags (decorative, fail-soft) -------------------------------------------------------


def _flag_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (48, 36), (0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def _route_flag_builder(monkeypatch, *, flags: bool):
    from app.services import flag_images

    hops = [_hop(1), {**_hop(2), "city": "Dortmund", "country": "de"}, {**_hop(3), "city": "Paris", "country": "XYZ"}]
    b = _full_builder(monkeypatch, hops=hops)
    monkeypatch.setattr(
        pdf_report, "load_flags", (lambda codes: {"DE": _flag_png()}) if flags else flag_images.load_flags
    )
    return b


def test_flags_precede_places_in_path_and_table_and_keep_country_code_text(monkeypatch):
    b = _route_flag_builder(monkeypatch, flags=True)
    b.prefetch_external_data()
    assert set(b._flags) == {"DE"}
    b._assemble()
    diagram = next(el for el in b.elements if isinstance(el, pdf_report.RouteDiagram))
    assert diagram.flags is b._flags
    assert any(e.get("flag") == "DE" and e["title"].endswith("DE") for e in diagram.entries)
    assert any(
        isinstance(c, pdf_report.FlagLabel) for el in b.elements for r in getattr(el, "_cellvalues", []) for c in r
    )
    data = _render(b)
    assert data.startswith(b"%PDF")


def test_pdf_builds_offline_with_text_only_when_flags_are_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("JUSTUP_DATA_DIR", str(tmp_path))
    from app.services import flag_images

    def no_network(**_kw):
        raise OSError("offline")

    monkeypatch.setattr(flag_images.httpx, "Client", no_network)
    b = _route_flag_builder(monkeypatch, flags=False)
    b.prefetch_external_data()
    assert b._flags == {}
    b._assemble()
    diagram = next(el for el in b.elements if isinstance(el, pdf_report.RouteDiagram))
    assert any("DE" in e["title"] for e in diagram.entries)
    assert not any(
        isinstance(c, pdf_report.FlagLabel) for el in b.elements for r in getattr(el, "_cellvalues", []) for c in r
    )
    b.build()
    assert b.page_count >= 1


def test_hop_table_dashes_location_and_latency_of_silent_hops_only(monkeypatch):
    silent_interpolated = _mhop(8, "*", 20.3, status="interpolated", _interpolated=True)
    hops = [_mhop(1, "172.30.176.1"), _mhop(2, city="Dortmund"), silent_interpolated, _mhop(9, "9.9.9.9", 26.0)]
    b = _route_builder(monkeypatch, hops)
    b._add_traceroute(display_hops=hops)
    rows = next(el for el in b.elements if hasattr(el, "_cellvalues"))._cellvalues
    cells = {int(_cells_of([r[0]])[0]): [" ".join(_cells_of([c])) for c in r] for r in rows[1:-1]}
    assert "Location unknown" in cells[1][2]  # replied, but private address: no location
    assert cells[8][2].strip() == "—" and cells[8][3].strip() == "—" and "20.3" not in " ".join(cells[8])
    assert "NO REPLY" in cells[8][-1].upper() and "stimated" not in " ".join(cells[8])  # same wording as the web table


# ─── Rendering review: orphan notes, wording and country-only locations ─────────────────────────
_GERMAN_WORDS = ("Ziel", "Verfügbarkeit", "Datenabdeckung", "Keine Antwort", "Messung", "Betriebs")


@pytest.mark.parametrize("size", ["a4", "letter"])
@pytest.mark.parametrize("n_hops", [10, 12, 13, 14, 16, 18, 20])
def test_hop_table_note_never_stands_alone_on_a_page(monkeypatch, size, n_hops):
    _no_charts(monkeypatch)
    hops = [_hop(i, status="timeout" if i % 4 == 0 else "ok") for i in range(1, n_hops + 1)]
    _assert_no_blank_pages(_full_builder(monkeypatch, size=size, hops=hops, ssl=_tls_result()))


@pytest.mark.parametrize(
    "kw",
    [
        {"hops": [_hop(1), _hop(2, status="timeout")], "ssl": None},
        {"hops": [_hop(1)], "ssl": None, "fail": (5,), "sla": True, "ping": True, "tcp_ports": 2},
        {"hops": [], "ssl": None, "nodata": True},
    ],
)
def test_report_text_is_english_only(monkeypatch, kw):
    text = _all_text(_full_builder(monkeypatch, **kw))
    assert not [w for w in _GERMAN_WORDS if w.lower() in text.lower()]


def test_single_failed_check_uses_singular_wording(monkeypatch):
    text = _all_text(_full_builder(monkeypatch, hops=[_hop(1)], ssl=None, fail=(10,)))
    assert "sum of measured outages" in text and "forms 1 downtime event" not in text
    assert "× interval" not in text


def test_country_without_city_is_not_shown_as_a_place(monkeypatch):
    hop = _hop(2) | {"city": "", "country": "DE"}
    b = _route_builder(monkeypatch, [_hop(1), hop, _hop(3)])
    b._add_traceroute(display_hops=[_hop(1), hop, _hop(3)])
    rows = next(el for el in b.elements if hasattr(el, "_cellvalues"))._cellvalues
    location = " ".join(_cells_of([rows[2][2]]))
    assert "DE (country only)" in location and "()" not in location and "(DE)" not in location


# ---------------------------------------------------------------------------
# Latency charts: linear readable axes, peaks within intervals, consistent captions
# ---------------------------------------------------------------------------

_CH_START = datetime(2026, 5, 4, tzinfo=UTC)
_CH_END = _CH_START + timedelta(days=7)


def _display(n=168, base=80.0, *, peaks=None, rollup_until=0, bad=()):
    """Hourly display buckets: median near `base`, maximum 1.1-1.4x, selected maxima overridden."""
    out = []
    for i in range(n):
        t = _CH_START + timedelta(hours=i)
        med = base + (i % 7) - 3
        point = {
            "ts": t.isoformat(),
            "value": med,
            "max": (peaks or {}).get(i, med * (1.1 + (i % 4) * 0.1)),
            "bucket_end": (t + timedelta(hours=1)).isoformat(),
            "rollup": i < rollup_until,
        }
        if i < rollup_until:
            point["max"] = None
        if i in bad:
            point["value"], point["max"] = None, "n/a"
        out.append(point)
    return out


def _capture_axes(monkeypatch, make):
    figs = []
    monkeypatch.setattr(pdf_report, "_fig_png", lambda fig: figs.append(fig) or b"png")
    make()
    return figs


def _tick_labels(ax) -> list[str]:
    fmt = ax.yaxis.get_major_formatter()
    return [fmt(t, i) for i, t in enumerate(ax.get_yticks())]


def test_response_time_legend_is_below_plot(monkeypatch):
    (fig,) = _capture_axes(
        monkeypatch,
        lambda: pdf_report._create_response_time_chart(_display(), _CH_START, _CH_END, sla_threshold_ms=500),
    )
    ax = fig.axes[0]
    canvas = FigureCanvasAgg(fig)
    canvas.draw()
    legend = ax.get_legend().get_window_extent(canvas.get_renderer())
    assert legend.y1 < ax.get_window_extent().y0
    assert legend.y0 >= 0


@pytest.mark.parametrize(
    ("top", "expected"),
    [
        (6, [0, 2, 4, 6]),
        (140, [0, 50, 100, 150]),
        (1250, [0, 250, 500, 750, 1000, 1250]),
        (0.8, [0, 0.2, 0.4, 0.6, 0.8, 1.0]),
    ],
)
def test_nice_ticks_are_round_ascending_numbers(top, expected):
    ticks = pdf_report._nice_ticks(top, 4 if top == 140 else 5)
    assert ticks[0] == 0 and ticks[-1] >= top and ticks == sorted(set(ticks))
    assert ticks == pytest.approx(expected)


@pytest.mark.parametrize(
    ("base", "peaks", "sla", "label"),
    [
        (80, None, None, "normal"),
        (4, None, None, "under 10 ms"),
        (100, {10: 150}, None, "50-150 ms"),
        (80, {40: 2400}, None, "isolated extreme peak"),
        (80, {40: 2400, 41: 450, 42: 460, 43: 470, 44: 480}, 400, "adjacent peaks and SLA"),
        (80, {40: 900}, 400, "SLA configured"),
        (5, None, 500, "SLA far above data"),
    ],
)
def test_latency_axis_is_linear_short_and_plain_numbers(monkeypatch, base, peaks, sla, label):
    data = _display(base=base, peaks=peaks)
    (fig,) = _capture_axes(
        monkeypatch,
        lambda: pdf_report._create_response_time_chart(data, _CH_START, _CH_END, sla_threshold_ms=sla),
    )
    ax = fig.axes[0]
    assert ax.get_yscale() == "linear", label
    assert ax.get_ylabel() == "Response time (ms)", label
    assert ax.yaxis.label.get_rotation() == 0, label
    assert ax.get_position().x0 <= 0.06, label  # keep only tick-label space outside the plot
    labels = _tick_labels(ax)
    assert all(re.fullmatch(r"\d{1,3}(,\d{3})*(\.\d+)?", t) for t in labels), (label, labels)
    assert len(labels) <= 8 and ax.get_ylim()[0] == 0
    # An extreme spike must not flatten the normal values: the axis stays near the typical range.
    if peaks and max(peaks.values()) >= 2000:
        assert ax.get_ylim()[1] < max(3 * (base + 10) * 1.5, (sla or 0) * 1.5)
        assert any("2,400 ms" in t.get_text() for t in ax.texts)
    legend = [t.get_text() for t in ax.get_legend().get_texts()]
    assert not any("Raw peak" in t for t in legend)
    if peaks and max(peaks.values()) >= 400:
        assert any("Peak within interval" in t for t in legend)


def test_peak_bars_span_only_their_interval_and_distant_sla_stays_off_the_scale(monkeypatch):
    minutes = [
        {
            "ts": (_CH_START + timedelta(minutes=i)).isoformat(),
            "value": 80.0,
            "max": 900.0 if i == 700 else 90.0,
            "bucket_end": (_CH_START + timedelta(minutes=i + 1)).isoformat(),
        }
        for i in range(1440)
    ]
    (fig,) = _capture_axes(
        monkeypatch,
        lambda: pdf_report._create_response_time_chart(
            minutes, _CH_START, _CH_START + timedelta(days=1), sla_threshold_ms=5000
        ),
    )
    ax = fig.axes[0]
    peak_rgb = mcolors.to_rgb(pdf_report.PEAK_COLOR)
    peak_bars = [
        c
        for c in ax.collections
        if isinstance(c, LineCollection) and tuple(c.get_colors()[0][:3]) == pytest.approx(peak_rgb)
    ]
    horizontal = [seg for c in peak_bars for seg in c.get_segments() if seg[0][1] == seg[1][1]]
    assert horizontal and all(
        mdates.num2date(seg[1][0]) - mdates.num2date(seg[0][0]) <= timedelta(minutes=1, seconds=1) for seg in horizontal
    )
    assert ax.get_ylim()[1] < 5000 / 2
    assert any("SLA 5,000 ms (above scale)" in t.get_text() for t in ax.texts)


def test_normal_variation_draws_no_peak_markers():
    view = pdf_report._build_view(_display(), UTC)
    assert view.peaks == [] and view.highest_max is not None


def test_peaks_are_prominent_intervals_not_points_in_time():
    view = pdf_report._build_view(_display(peaks={40: 2400, 41: 450, 100: 520}), UTC)
    assert round(view.peaks[0].value) == 2400
    top = view.peaks[0]
    # The peak keeps its whole bucket instead of claiming the bucket start as its time.
    assert top.end - top.start == timedelta(hours=1)


def test_rollups_invalid_and_missing_points_never_become_peaks():
    data = _display(rollup_until=100, bad=(120, 121), peaks={150: 900})
    data.append({"ts": (_CH_START + timedelta(hours=200)).isoformat(), "value": 80})  # no max/bucket_end at all
    view = pdf_report._build_view(data, UTC)
    assert [round(p.value) for p in view.peaks] == [900]
    assert view.no_max_intervals >= 100
    note = PDFReportBuilder._latency_note([{"ts": d["ts"], "value": d["value"]} for d in data], data, UTC)
    assert "rolled-up history" in note and "exact time unknown" in note
    only_rollups = _display(n=20, rollup_until=20)
    assert pdf_report._build_view(only_rollups, UTC).peaks == []
    assert pdf_report._create_response_time_chart([], _CH_START, _CH_END) is None
    assert (
        pdf_report._create_response_time_chart([{"ts": _CH_START.isoformat(), "value": None}], _CH_START, _CH_END)
        is None
    )


def test_caption_lists_peaks_and_explains_a_higher_interval_maximum():
    data = _display(peaks={40: 2400})
    samples = [{"ts": d["ts"], "value": d["value"]} for d in data]
    note = PDFReportBuilder._latency_note(samples, data, UTC, None)
    assert "2,400 ms (05-05 16:00–17:00)" in note
    assert "above the maximum of the plotted sample series" in note
    assert "above the axis" in note and "marked at the top edge" in note
    # Without display data the caption is just the summary statistics
    assert PDFReportBuilder._latency_note(samples).count("·") == 2 and "peak" not in PDFReportBuilder._latency_note(
        samples
    )
    consistent = PDFReportBuilder._latency_note([{**s, "value": 3000} for s in samples[:1]], data, UTC)
    assert "above the maximum" not in consistent


def test_multi_port_uses_independent_scales_when_ranges_differ_widely(monkeypatch):
    ports = {22: _display(base=5), 443: _display(base=25, peaks={30: 300}), 8443: _display(base=400, peaks={80: 1500})}
    (fig,) = _capture_axes(monkeypatch, lambda: pdf_report._create_multi_port_tcp_chart(ports, _CH_START, _CH_END))
    panels = fig.axes
    assert [ax.get_yscale() for ax in panels] == ["linear"] * 3
    assert all(ax.get_position().x0 <= 0.06 for ax in panels)
    assert len({ax.get_ylim()[1] for ax in panels}) == 3
    assert all(re.fullmatch(r"[\d,]+", t) for ax in panels for t in _tick_labels(ax))
    assert any("300 ms" in t.get_text() for t in panels[1].texts) and any(
        "1,500 ms" in t.get_text() for t in panels[2].texts
    )
    note = PDFReportBuilder._multi_port_note(ports, UTC)
    assert "independent Y scales" in note and "port 8443 1,500 ms" in note
    close = {22: _display(base=20), 443: _display(base=40)}
    (fig,) = _capture_axes(monkeypatch, lambda: pdf_report._create_multi_port_tcp_chart(close, _CH_START, _CH_END))
    assert fig.axes[0].get_ylim() == fig.axes[1].get_ylim()
    assert "shared time axis and Y scale" in PDFReportBuilder._multi_port_note(close, UTC)


@pytest.mark.parametrize("size", ["a4", "letter"])
def test_pdf_with_extreme_peaks_and_short_window_renders_on_both_paper_sizes(monkeypatch, size):
    b = _full_builder(monkeypatch, size=size, hops=[], ssl=None, ping=True, tcp_ports=2, sla=True)
    display = {
        "http_ms": _display(peaks={40: 2400}),
        "ping_ms": _display(base=4, peaks={60: 38}),
        "tcp_ms_443": _display(base=25, peaks={30: 300}),
        "tcp_ms_444": _display(base=400),
    }
    b.report_data = dataclasses.replace(b.report_data, display_data=display)
    b._assemble()
    text = _all_text(b)
    assert "Highest peaks within an interval" in text and "Raw peak" not in text
    assert "Availability Timeline" in text and "Response Time" in text
    chart_sections = {
        section[0].getPlainText().split(" ·", 1)[0]: section
        for element in b.elements
        if isinstance(element, pdf_report.KeepTogether)
        and (section := element._content)
        and section[0].getPlainText().startswith(("Availability Timeline", "Response Time"))
    }
    for title in ("Availability Timeline", "Response Time"):
        assert chart_sections[title][2].getPlainText().strip()
    rendered = _render(b)
    assert rendered.startswith(b"%PDF")


def _logo_curve_count() -> int:
    """Curve segments in the shared logo SVG, counted from its raw path data."""
    svg = pdf_logo.LOGO_SVG_PATH.read_text(encoding="utf-8")
    return sum(1 for d in re.findall(r' d="([^"]*)"', svg) for seg in pdf_logo._parse_path(d) if seg[0] == "C")


def test_pdf_logo_comes_from_the_single_web_logo_asset():
    repo = Path(pdf_report.__file__).resolve().parents[2]
    web_logo = repo / "app" / "static" / "img" / "justup_1c.svg"
    assert web_logo == pdf_logo.LOGO_SVG_PATH
    assert "/static/img/justup_1c.svg" in (repo / "app" / "templates" / "base.html").read_text(encoding="utf-8")
    # No second, stale copy of the wordmark and no hand-drawn footer signet.
    assert not list((repo / "app" / "static").glob("justup_1c.*"))
    source = Path(pdf_report.__file__).read_text(encoding="utf-8")
    assert "justup_1c.png" not in source
    assert "heartbeat" not in source.lower()


def test_pdf_embeds_the_shared_logo_vectors_on_cover_and_footer(monkeypatch):
    monkeypatch.setattr("reportlab.rl_config.pageCompression", 0)
    b = _builder({"name": "t", "http_url": "https://example.org"})
    b.elements = []
    b._build_cover_page()
    buf = io.BytesIO()
    SimpleDocTemplate(buf).build(list(b.elements), onFirstPage=b._add_header_footer)
    pdf = buf.getvalue()
    curves = _logo_curve_count()
    assert curves > 50
    # The cover flowable and the footer callback each draw every curve of the SVG once.
    assert pdf.count(b" c\n") + pdf.count(b" c ") >= 2 * curves


def test_logo_aspect_and_bounds_match_the_svg_viewbox():
    w, h, paths = pdf_logo.load_logo()
    xs = [v for segs in paths for _, pts in segs for v in pts[0::2]]
    ys = [v for segs in paths for _, pts in segs for v in pts[1::2]]
    assert min(xs) >= -1 and max(xs) <= w + 1
    assert min(ys) >= -1 and max(ys) <= h + 1
    assert pdf_logo.logo_aspect() == pytest.approx(h / w)
