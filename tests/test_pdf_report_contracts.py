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
from datetime import UTC, datetime, timedelta

import matplotlib.dates as mdates
import pytest
from reportlab.platypus import SimpleDocTemplate

from app.services import pdf_report
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
    assert _render(b).startswith(b"%PDF")


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
    assert _render(b).startswith(b"%PDF")


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
    _, pdf = _ssl_section(monkeypatch, dataclasses.replace(base, certificate=cert))
    assert pdf.startswith(b"%PDF")


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
    assert _render(b).startswith(b"%PDF")


def test_get_ssl_data_turns_scanner_crash_into_failed_result(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("secret-detail")

    monkeypatch.setattr(pdf_report, "scan_tls", boom)
    result = _builder({"http_url": "https://host.example"})._get_ssl_data()
    assert result.status is ScanStatus.FAILED and "secret-detail" not in (result.error or "")


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
    monkeypatch.setattr(b, "_get_mtr_data", lambda **_k: (hop_list, [], False))
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
    import re

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
    monkeypatch.setattr(b2, "_get_mtr_data", lambda **_k: ([], [], False))
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
    assert "Est. duration" in text and "Estimated downtime:" in text and "estimated downtime is about" in text
    assert "| DOWNTIME" not in f"| {text}" and "| Duration" not in f"| {text}"
    assert b.kpis.downtime_minutes == 3 * 5  # failed checks x 300 s interval


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
    assert "None detected" in text and "in observed checks" in text and "≈ 0 min" not in text
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
    assert "119 of 120 availability checks succeeded" in text
    assert "recorded checks succeeded" not in text


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


def _route_builder(monkeypatch, hops, *, host="dest.example", size="a4"):
    b = _full_builder(monkeypatch, hops=[], ssl=None, size=size)
    monkeypatch.setattr(
        b,
        "_get_mtr_data",
        lambda *, max_hops=pdf_report._MTR_MAX_HOPS: PDFReportBuilder._get_mtr_data(b, max_hops=max_hops),
    )
    monkeypatch.setattr(b, "_get_traceroute_host", lambda: host)
    monkeypatch.setattr(pdf_report, "_run_mtr_with_interpolation", lambda *_a, **_k: (list(hops), [], False))
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
    hops = [_hop(i) for i in range(1, 19)] + [_hop(i, status="timeout") for i in (19, 20)]
    b = _route_builder(monkeypatch, [*hops, _synthetic_destination(20)])
    text = _all_text(b)
    assert text.count("Route incomplete – 20-hop limit reached") == 2  # chapter subtitle and warning note
    assert "not shown completely" in text and "NO REPLY" in text


def test_route_reaching_its_destination_is_not_flagged_incomplete(monkeypatch):
    b = _route_builder(monkeypatch, [_hop(i) for i in range(1, 9)])
    assert "Route incomplete" not in _all_text(b)
    # a destination that stayed silent before the limit is not a limit problem either
    b = _route_builder(monkeypatch, [_hop(i) for i in range(1, 9)] + [_synthetic_destination(8)])
    assert "Route incomplete" not in _all_text(b)


def test_route_ending_in_silent_hops_at_the_limit_is_incomplete_without_a_resolved_destination(monkeypatch):
    hops = [_hop(i) for i in range(1, 19)] + [_hop(i, status="timeout") for i in (19, 20)]
    text = _all_text(_route_builder(monkeypatch, hops))
    assert "Route incomplete – 20-hop limit reached" in text
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
    assert [h["hop"] for h in b._get_mtr_data()[0]][-1] == 10
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
    b._provider_cache = None
    del b._provider_cache
    assert b._destination_place() == "Ratingen, DE · approximate GeoIP location · 100 km radius"
    assert b._destination_place(with_accuracy=False) == "Ratingen, DE · approximate GeoIP location"


def test_merged_downtime_summary_without_incidents_and_own_page_with_incidents(monkeypatch):
    quiet = _full_builder(monkeypatch, hops=[_hop(1)], ssl=None, target_extra=FULL_COVERAGE)
    quiet.build()
    assert quiet.chapter_pages["Downtime Log"] == quiet.chapter_pages["Availability & Performance"] or (
        quiet.chapter_pages["Downtime Log"] == quiet.page_count
    )
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
    assert text.index("Supported Versions") < text.index("Heartbleed") < text.index("Preferred Cipher")
    assert text.index("Preferred Cipher") < text.index("Cipher Suites by Protocol (technical detail)")


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


def test_empty_mtr_route_is_cached_so_mtr_runs_once(monkeypatch):
    b = _route_builder(monkeypatch, [])
    calls = []

    def _silent_route(*_a, **_k):
        calls.append(1)
        return [], [], False

    monkeypatch.setattr(pdf_report, "_run_mtr_with_interpolation", _silent_route)
    assert b._get_mtr_data() == ([], [], False)
    assert b._get_mtr_data() == ([], [], False)
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
    flag_images.clear_cache()
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
    assert "1 failed check ×" in text and "1 failed check forms 1 downtime event" in text
    assert "1 failed checks" not in text


def test_country_without_city_is_not_shown_as_a_place(monkeypatch):
    hop = _hop(2) | {"city": "", "country": "DE"}
    b = _route_builder(monkeypatch, [_hop(1), hop, _hop(3)])
    b._add_traceroute(display_hops=[_hop(1), hop, _hop(3)])
    rows = next(el for el in b.elements if hasattr(el, "_cellvalues"))._cellvalues
    location = " ".join(_cells_of([rows[2][2]]))
    assert "DE (country only)" in location and "()" not in location and "(DE)" not in location
