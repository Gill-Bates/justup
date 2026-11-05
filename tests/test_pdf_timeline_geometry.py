#!/usr/bin/env python3
#
# tests/test_pdf_timeline_geometry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""PDF availability timeline: the bars run over the full text width (the page's content width), edge to edge."""

from __future__ import annotations

import io
from collections import Counter
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


def _plot_edges(png: bytes, *, min_fraction: float = 0.5) -> tuple[int, int]:
    """Left and right pixel of the plot area: the most common ends of the long horizontal ink runs.

    Y tick labels sit left of a gap and tick marks only touch a few rows, so neither moves the mode.
    """
    image = Image.open(io.BytesIO(png)).convert("L")
    width, height = image.size
    runs: list[tuple[int, int]] = []
    for y in range(height):
        start = None
        for x in range(width + 1):
            inked = x < width and image.getpixel((x, y)) < 250
            if inked and start is None:
                start = x
            elif not inked and start is not None:
                if x - start >= width * min_fraction:
                    runs.append((start, x - 1))
                start = None
    assert runs, "no plot rule found"
    left = Counter(a for a, _ in runs).most_common(1)[0][0]
    right = Counter(b for _, b in runs).most_common(1)[0][0]
    return left, right


def _chapter_images(builder: pdf_report.PDFReportBuilder, monkeypatch) -> dict[str, bytes]:
    images: dict[str, bytes] = {}
    monkeypatch.setattr(builder, "_add_chart_section", lambda title, png, *_: images.setdefault(title, png))
    builder._build_charts_page(page_break=False)  # noqa: SLF001
    return images


def test_timeline_plot_area_matches_the_response_time_chart(monkeypatch) -> None:
    data = pdf_report.ReportData(
        target={"name": "t", "host": "h"},
        from_date=START,
        to_date=END,
        http_data=[
            {"ts": (START + timedelta(minutes=m)).isoformat(), "value": 100.0 + m % 7} for m in range(0, 1440, 5)
        ],
        uptime_data=_series(),
        ping_data=[],
        tcp_data=[],
        cert_data=[],
        status_code_data=[],
    )
    images = _chapter_images(pdf_report.PDFReportBuilder(data), monkeypatch)
    response_left, _ = _plot_edges(images["Response Time"])
    timeline = Image.open(io.BytesIO(images["Availability Timeline"])).convert("RGB")
    band = range(int(timeline.height * 0.45), int(timeline.height * 0.6))
    inked = [x for x in range(timeline.width) if any(timeline.getpixel((x, y)) != (255, 255, 255) for y in band)]
    assert response_left > 0
    assert abs(inked[0] - response_left) <= 2, f"bars start at {inked[0]} px, the response plot at {response_left} px"
    assert inked[-1] == timeline.width - 1  # both charts end at the text area edge
