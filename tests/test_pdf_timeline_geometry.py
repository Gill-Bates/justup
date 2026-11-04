#!/usr/bin/env python3
#
# tests/test_pdf_timeline_geometry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""PDF availability timeline: the bars run over the full text width (the page's content width), edge to edge."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from PIL import Image

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


def _chart() -> tuple[pdf_report.PDFReportBuilder, float, bytes]:
    builder = _builder()
    width_in = builder._chart_width_in()  # noqa: SLF001 - the width every chart is built for
    png = pdf_report._create_uptime_chart(_series(), START, END, width=width_in, height=1.45)  # noqa: SLF001
    return builder, width_in, png


def test_timeline_image_is_exactly_as_wide_as_the_text_area() -> None:
    builder, width_in, png = _chart()
    image = Image.open(io.BytesIO(png))
    assert abs(image.width / pdf_report.PDF_DPI - width_in) < 0.01
    assert abs(width_in * 72 - builder.content_width) < 0.01


def test_timeline_bars_start_and_end_at_the_image_edges() -> None:
    _, _, png = _chart()
    image = Image.open(io.BytesIO(png)).convert("RGB")
    band = range(int(image.height * 0.45), int(image.height * 0.6))  # inside the bar strip, clear of labels
    inked = [x for x in range(image.width) if any(image.getpixel((x, y)) != (255, 255, 255) for y in band)]
    assert inked[0] == 0, f"bars start {inked[0]} px inside the text area"
    assert inked[-1] == image.width - 1, f"bars stop {image.width - 1 - inked[-1]} px short of the text area"


def test_chart_section_places_the_image_at_the_content_width() -> None:
    builder, _, png = _chart()
    builder._add_chart_section("Availability Timeline", png, "note")  # noqa: SLF001
    block = builder.elements[-1]
    image = next(flowable for flowable in block._content if hasattr(flowable, "drawWidth"))  # noqa: SLF001
    assert abs(image.drawWidth - builder.content_width) < 0.01
    assert image.hAlign == "LEFT"
