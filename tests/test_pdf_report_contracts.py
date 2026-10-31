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
from datetime import UTC, datetime

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
    from datetime import timedelta

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
    assert len(table._cellvalues) == 31  # header + all 30 events, none dropped


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
    for attr in ("label", "value"):  # RoundedKPICard
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
    from datetime import timedelta

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

    pages = [b.chapter_pages[name] for name in CHAPTERS]
    assert pages[0] == 1
    assert pages == sorted(set(pages)), pages  # strictly increasing: no two chapters share a page
    b._assemble()  # build() consumes the flowables; re-compose them for the structural checks
    heads = [i for i, el in enumerate(b.elements) if getattr(el, "chapter_name", None) in CHAPTERS[1:]]
    assert len(heads) == 4
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
    assert sum(len(p._cellvalues) - 1 for p in parts) == n_hops  # no row dropped


def test_long_downtime_log_repeats_header_and_keeps_every_event(monkeypatch):
    b = _full_builder(monkeypatch, hops=[_hop(1)], ssl=_tls_result(), fail=tuple(range(2, 118, 2)))
    b._assemble()
    table = next(el for el in b.elements if getattr(el, "repeatRows", 0) and _first_cell_text(el) == "#")
    n_events = len(b.downtime_events)
    assert n_events > 50 and table.repeatRows == 1
    parts = _split_all(table)
    assert len(parts) >= 2 and all(_first_cell_text(p) == "#" for p in parts)
    assert sum(len(p._cellvalues) - 1 for p in parts) == n_events


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
    assert pages[k + 1] - pages[k] == 1 if k + 1 < len(CHAPTERS) else b.page_count == pages[k]  # exactly one page


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


@pytest.mark.parametrize(
    ("fails", "label", "kind"),
    [
        (0, "Operational", "success"),
        (1, "Operational", "success"),
        (2, "Degraded", "warning"),
        (6, "Degraded", "warning"),
        (7, "Critical", "danger"),
    ],
)
def test_status_thresholds_are_independent_of_the_configured_sla(monkeypatch, fails, label, kind):
    # 120 checks: 1 failure = 99.17 %, 2 = 98.33 %, 6 = 95.00 %, 7 = 94.17 %
    strict = {"sla_availability_pct": 99.99}
    b = _full_builder(monkeypatch, hops=[], ssl=None, fail=tuple(range(fails)), sla=True, target_extra=strict)
    assert b._status_info()[0::2] == (label, kind)


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
    assert "SLA Compliance" in cover and "Availability" in cover
    assert "Response Time" not in cover and "Ping Latency" not in cover
    assert "500" not in cover and "100 ms" not in cover


def test_http_status_chart_is_described_as_samples_not_exact_counts(monkeypatch):
    b = _full_builder(monkeypatch, hops=[], ssl=None)
    text = _all_text(b)
    assert "stored samples" in text and "not exact per-check counts" in text


def _route_builder(monkeypatch, hops, *, host="dest.example"):
    b = _full_builder(monkeypatch, hops=[], ssl=None)
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


def test_schematic_nodes_mark_start_destination_unknown_and_synthetic():
    hops = [
        {"hop": 1, "ip": "10.0.0.1", "status": "ok", "city": "Dortmund", "country": "DE"},
        {"hop": 2, "ip": "192.168.0.1", "status": "ok"},  # unresolved -> unknown
        {"hop": 3, "status": "interpolated", "_interpolated": True},  # ICMP blocked
        {"hop": 4, "ip": "10.0.0.6", "status": "ok", "city": "Frankfurt", "country": "DE"},
        _synthetic_destination(4),  # destination did not reply
    ]
    nodes = PDFReportBuilder._schematic_nodes(hops, "dest.example")
    kinds = [n["kind"] for n in nodes]
    assert kinds[0] == "start"
    assert kinds[-1] == "destination_unknown"
    assert nodes[1]["kind"] == "unknown" and nodes[1]["location_text"] == "position unknown"
    assert nodes[2]["location_text"] == "ICMP blocked"
    assert nodes[-1]["location_text"] == "no reply"
    # Hop order and original numbers are preserved.
    assert [n["label"] for n in nodes] == ["1", "2", "3", "4", "5"]
    # A gap (unknown hop) sets gap_before on the following node for a dashed link.
    assert nodes[2]["gap_before"] is True and nodes[3]["gap_before"] is True


def test_schematic_nodes_located_destination_is_not_unknown():
    hops = [
        {"hop": 1, "ip": "10.0.0.1", "status": "ok", "city": "Dortmund", "country": "DE"},
        {"hop": 2, "ip": "203.0.113.9", "status": "ok", "city": "Munich", "country": "DE"},
    ]
    nodes = PDFReportBuilder._schematic_nodes(hops, "dest.example")
    assert nodes[-1]["kind"] == "destination"
    assert nodes[-1]["location_text"] == "Munich (DE)"


def test_coverage_indicator_and_low_coverage_caveat():
    from datetime import timedelta

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
    b.kpis.uptime_percent, b.kpis.coverage_pct = 97.4, 50.0
    assert b._availability_value(97.4) == "≈ 97 %"
    b.kpis.coverage_pct = 100.0
    assert "≈" not in b._availability_value(97.4)
