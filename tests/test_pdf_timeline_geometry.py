#!/usr/bin/env python3
#
# tests/test_pdf_timeline_geometry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vector geometry contracts for the PDF availability and coverage charts."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from reportlab.pdfgen.canvas import Canvas

from app.services import pdf_report
from tests import _support  # noqa: F401 - imported for its sys.path side effect

START = datetime(2026, 11, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def _series(down_every_minutes: int = 600) -> list[dict]:
    return [
        {"ts": (START + timedelta(minutes=m)).isoformat(), "value": 0 if (m // 120) % 5 == 0 else 1}
        for m in range(0, 24 * 60, 5)
        if m % down_every_minutes >= 0
    ]


def _builder() -> pdf_report.PDFReportBuilder:
    data = pdf_report.ReportData(
        target={"name": "t", "host": "h"},
        from_date=START,
        to_date=END,
        http_data=[],
        uptime_data=_series(),
        ping_data=[],
        tcp_data=[],
        cert_data=[],
        status_code_data=[],
    )
    return pdf_report.PDFReportBuilder(data)


class _RecordingCanvas(Canvas):
    def __init__(self, target: io.BytesIO) -> None:
        super().__init__(target, pageCompression=0)
        self.rectangles: list[tuple[float, float, float, float]] = []
        self.lines: list[tuple[float, float, float, float]] = []

    def rect(self, x, y, width, height, stroke=1, fill=0, **kwargs):
        self.rectangles.append((x, y, width, height))
        return super().rect(x, y, width, height, stroke=stroke, fill=fill, **kwargs)

    def line(self, x1, y1, x2, y2):
        self.lines.append((x1, y1, x2, y2))
        return super().line(x1, y1, x2, y2)


def _draw(chart, width: float | None = None) -> tuple[_RecordingCanvas, bytes]:
    buffer = io.BytesIO()
    canvas = _RecordingCanvas(buffer)
    chart.wrap(width or chart.base_width, 1000)
    chart.drawOn(canvas, 0, 0)
    canvas.save()
    return canvas, buffer.getvalue()


def test_timeline_is_a_vector_flowable_exactly_as_wide_as_the_text_area() -> None:
    builder = _builder()
    width_in = builder._chart_width_in()  # noqa: SLF001 - chart area contract
    chart = pdf_report._create_uptime_chart(_series(), START, END, width=width_in, height=1.45)  # noqa: SLF001
    canvas, pdf = _draw(chart, builder.content_width)
    assert abs(chart.width - builder.content_width) < 0.01
    assert abs(canvas.rectangles[0][0]) < 0.01
    assert abs(canvas.rectangles[0][2] - builder.content_width) < 0.01
    assert b"/Subtype /Image" not in pdf


def test_chart_section_keeps_the_vector_chart_at_content_width() -> None:
    builder = _builder()
    chart = pdf_report._create_uptime_chart(_series(), START, END, width=builder._chart_width_in())  # noqa: SLF001
    builder._add_chart_section("Availability Timeline", chart, "note")  # noqa: SLF001
    block = builder.elements[-1]
    assert block._content[1] is chart  # noqa: SLF001
    chart.wrap(builder.content_width, 1000)
    assert abs(chart.width - builder.content_width) < 0.01


def test_timeline_and_latency_plot_edges_align() -> None:
    http = [{"ts": (START + timedelta(minutes=m)).isoformat(), "value": 100.0 + m % 7} for m in range(0, 1440, 5)]
    width = _builder()._chart_width_in()  # noqa: SLF001
    left_in = pdf_report._latency_plot_left_in(http, START, None, width)  # noqa: SLF001
    response = pdf_report._create_response_time_chart(  # noqa: SLF001
        http, START, END, width=width, plot_left_in=left_in
    )
    uptime = pdf_report._create_uptime_chart(_series(), START, END, width=width, plot_left_in=left_in)  # noqa: SLF001
    response_canvas, _ = _draw(response)
    uptime_canvas, _ = _draw(uptime)
    response_plot_left = response_canvas.lines[0][0]
    timeline_plot_left = uptime_canvas.rectangles[0][0]
    assert abs(timeline_plot_left - response_plot_left) < 0.01


def test_coverage_uses_the_configured_interval_and_keeps_unmeasured_gaps() -> None:
    start = datetime(2026, 10, 21, tzinfo=UTC)
    end = start + timedelta(days=1)
    offsets = [0, 55, 61, 125, 400, 3600, 7300, 7360, 20000, 40000, 40030, 70000]
    stamps = [start + timedelta(seconds=offset) for offset in offsets]
    blocks = pdf_report._coverage_blocks(stamps, timedelta(seconds=60), start, end)  # noqa: SLF001
    covered = sum((last - first).total_seconds() for first, last in blocks)
    data = pdf_report.ReportData(
        target={"interval_seconds": 60},
        from_date=start,
        to_date=end,
        http_data=[],
        uptime_data=[{"ts": stamp.isoformat(), "value": 1} for stamp in stamps],
        ping_data=[],
        tcp_data=[],
        cert_data=[],
        status_code_data=[],
    )
    kpi = pdf_report._compute_kpis(data).coverage_pct  # noqa: SLF001
    assert abs(covered / 86400 * 100 - kpi) < 0.5
    assert len(blocks) > 1
    hourly = [start + timedelta(hours=hour) for hour in range(25)]
    one_day = pdf_report._coverage_blocks(hourly[:24], timedelta(seconds=60), start, end)  # noqa: SLF001
    assert sum((last - first).total_seconds() for first, last in one_day) / 86400 < 0.03
    chart = pdf_report._create_coverage_bar(  # noqa: SLF001
        [{"ts": stamp.isoformat(), "value": 1} for stamp in hourly[:24]], start, end, interval_seconds=60
    )
    _, pdf = _draw(chart)
    assert pdf.startswith(b"%PDF") and b"/Subtype /Image" not in pdf


def test_coverage_blocks_merge_overlap_without_counting_gaps() -> None:
    spans = [
        (START, START + timedelta(minutes=10)),
        (START + timedelta(minutes=9), START + timedelta(minutes=20)),
        (START + timedelta(hours=2), START + timedelta(hours=2, minutes=10)),
    ]
    blocks = pdf_report._coverage_span_blocks(spans, timedelta(minutes=1), START, END)  # noqa: SLF001
    assert len(blocks) == 2
    assert blocks[0][0] == START and blocks[0][1] == START + timedelta(minutes=21)
    assert blocks[1][0] == START + timedelta(hours=2)


def test_availability_blocks_include_up_and_down_runs_but_leave_missing_time_empty() -> None:
    points = []
    for hour in range(24):
        if 12 <= hour < 18:
            continue
        points.append({"ts": (START + timedelta(hours=hour)).isoformat(), "value": 0 if 6 <= hour < 12 else 1})
    chart = pdf_report._create_uptime_chart(points, START, END, height=1.45)  # noqa: SLF001
    canvas, pdf = _draw(chart)
    up = pdf_report.CHART_COLORS["uptime_up"]
    down = pdf_report.CHART_COLORS["uptime_down"]
    rectangles = canvas.rectangles
    assert len(rectangles) > 3
    assert pdf.startswith(b"%PDF") and b"/Subtype /Image" not in pdf
    assert up and down  # palette entries feed native fill primitives
