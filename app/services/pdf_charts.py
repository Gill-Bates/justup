#!/usr/bin/env python3
"""Native ReportLab vector charts for PDF reports."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo

from reportlab.lib import colors
from reportlab.platypus import Flowable

from app.services.pdf_locale import DEFAULT_PDF_LOCALE, PDFLocale

DrawText = Callable[..., None]
TextWidth = Callable[[str, str, float], float]


class ChartFlowable(Flowable):
    """A vector chart whose drawing scales to the width and height Platypus grants it."""

    def __init__(self, width: float, height: float, draw: Callable, *, max_height: float | None = None) -> None:
        super().__init__()
        self.base_width = width
        self.base_height = height
        self.max_height = max_height
        self._draw_chart = draw
        self._scale = 1.0
        self.width = width
        self.height = height

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:  # noqa: N803
        scale = min(1.0, availWidth / self.base_width)
        if availHeight > 0:
            scale = min(scale, availHeight / self.base_height)
        if self.max_height is not None:
            scale = min(scale, self.max_height / self.base_height)
        self._scale = scale
        self.width = self.base_width * scale
        self.height = self.base_height * scale
        return self.width, self.height

    def draw(self) -> None:
        self.canv.saveState()
        self.canv.scale(self._scale, self._scale)
        self._draw_chart(self.canv)
        self.canv.restoreState()


def _color(value: str) -> colors.Color:
    return colors.HexColor(value)


def _mix(hex_color: str, amount: float) -> str:
    value = hex_color.lstrip("#")
    rgb = [int(value[index : index + 2], 16) for index in (0, 2, 4)]
    return "#" + "".join(f"{round(channel + (255 - channel) * amount):02x}" for channel in rgb)


def _date_ticks(start: datetime, end: datetime, count: int = 5) -> list[datetime]:
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    span = end_utc - start_utc
    if span <= timedelta(0):
        return [start]
    return [(start_utc + span * index / (count - 1)).astimezone(start.tzinfo or UTC) for index in range(count)]


def _date_label(value: datetime, span: timedelta, locale: PDFLocale) -> str:
    if span <= timedelta(days=1):
        style = "time"
    elif span <= timedelta(days=3):
        style = "monthDayTime"
    else:
        style = "monthDay"
    return locale.format_datetime(value, style)


def _text(
    canvas,
    draw_text: DrawText,
    x: float,
    y: float,
    value: str,
    *,
    font: str,
    size: float,
    color: str = "#495057",
    align: str = "left",
) -> None:
    canvas.setFillColor(_color(color))
    canvas.setFont(font, size)
    draw_text(canvas, x, y, value, align=align)


def _axis_x(
    canvas,
    *,
    x: float,
    y: float,
    width: float,
    start: datetime,
    end: datetime,
    tz: tzinfo,
    draw_text: DrawText,
    font: str,
    locale: PDFLocale,
    xlabel: bool = True,
    grid: bool = True,
) -> None:
    span = end.astimezone(UTC) - start.astimezone(UTC)
    canvas.setStrokeColor(_color("#adb5bd"))
    canvas.setLineWidth(0.5)
    canvas.line(x, y, x + width, y)
    for index, tick in enumerate(_date_ticks(start, end)):
        px = x + width * (tick.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / max(span.total_seconds(), 1)
        canvas.setStrokeColor(_color("#e9ecef"))
        if grid:
            canvas.setDash(1, 0)
            canvas.line(px, y, px, y + 2)
        canvas.setStrokeColor(_color("#495057"))
        canvas.line(px, y, px, y - 2.5)
        align = "left" if index == 0 else "right" if index == 4 else "center"
        _text(
            canvas,
            draw_text,
            px,
            y - 13,
            _date_label(tick.astimezone(tz), span, locale),
            font=font,
            size=7,
            align=align,
        )
    if xlabel:
        local_start, local_end = start.astimezone(tz), end.astimezone(tz)
        label = (
            f"{locale.format_datetime(local_start)} – {locale.format_datetime(local_end)} "
            f"({local_start.tzname() or 'UTC'})"
        )
        _text(canvas, draw_text, x + width / 2, y - 29, label, font=font, size=7, color="#6c757d", align="center")


def _legend(
    canvas,
    entries: list[tuple[str, str, str]],
    *,
    x: float,
    max_width: float,
    y: float,
    font: str,
    size: float,
    text_width: TextWidth,
    draw_text: DrawText,
    columns: int = 2,
    align: str = "left",
) -> None:
    if not entries:
        return
    columns = min(columns, len(entries))
    widths = []
    while columns > 1:
        widths = [
            max((text_width(entries[i][0], font, size) + 22 for i in range(column, len(entries), columns)), default=54)
            for column in range(columns)
        ]
        if sum(widths) <= max_width:
            break
        columns -= 1
    if columns == 1:
        widths = [min(max_width, max(text_width(label, font, size) + 22 for label, _, _ in entries))]
    total_width = sum(widths)
    if align == "right":
        x -= total_width
    elif align == "center":
        x += (max_width - total_width) / 2
    for index, (label, kind, shade) in enumerate(entries):
        row, column = divmod(index, columns)
        left = x + sum(widths[:column])
        baseline = y - row * 11
        canvas.setStrokeColor(_color(shade))
        canvas.setFillColor(_color(shade))
        if kind == "line":
            canvas.setLineWidth(1.2)
            canvas.line(left, baseline + 2, left + 12, baseline + 2)
        elif kind == "dash":
            canvas.setLineWidth(1)
            canvas.setDash(4, 2)
            canvas.line(left, baseline + 2, left + 12, baseline + 2)
            canvas.setDash()
        elif kind == "triangle":
            path = canvas.beginPath()
            path.moveTo(left + 6, baseline + 6)
            path.lineTo(left + 11, baseline - 2)
            path.lineTo(left + 1, baseline - 2)
            path.close()
            canvas.drawPath(path, fill=1, stroke=0)
        else:
            canvas.rect(left + 2, baseline - 1, 8, 6, fill=1, stroke=0)
        _text(canvas, draw_text, left + 16, baseline, label, font=font, size=size)


def _segments(times: list[datetime], values: list[float]) -> list[list[tuple[datetime, float]]]:
    groups: list[list[tuple[datetime, float]]] = []
    current: list[tuple[datetime, float]] = []
    for stamp, value in zip(times, values, strict=True):
        if math.isnan(value):
            if current:
                groups.append(current)
                current = []
        else:
            current.append((stamp, value))
    if current:
        groups.append(current)
    return groups


def _draw_series(
    canvas,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    start: datetime,
    end: datetime,
    times: list[datetime],
    values: list[float],
    color: str,
    top: float,
    peaks: list[tuple[datetime, datetime, float, float]],
    overflow: list[tuple[datetime, float]],
    max_labels: int,
    draw_text: DrawText,
    font: str,
    locale: PDFLocale,
) -> list[tuple[str, str, str]]:
    span = max((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds(), 1)

    def px(stamp: datetime) -> float:
        return x + width * (stamp.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / span

    def py(value: float) -> float:
        return y + height * min(max(value, 0), top) / top

    line_color = _color(color)
    fill_hex = _mix(color, 0.86)
    canvas.setStrokeColor(line_color)
    canvas.setFillColor(_color(fill_hex))
    canvas.setLineWidth(1.1)
    for group in _segments(times, values):
        canvas.setFillColor(_color(fill_hex))
        path = canvas.beginPath()
        path.moveTo(px(group[0][0]), y)
        path.lineTo(px(group[0][0]), py(group[0][1]))
        for stamp, value in group[1:]:
            path.lineTo(px(stamp), py(value))
        path.lineTo(px(group[-1][0]), y)
        path.close()
        canvas.drawPath(path, fill=1, stroke=0)
        if len(group) > 1:
            path = canvas.beginPath()
            path.moveTo(px(group[0][0]), py(group[0][1]))
            for stamp, value in group[1:]:
                path.lineTo(px(stamp), py(value))
            canvas.setStrokeColor(line_color)
            canvas.setLineWidth(1.1)
            canvas.drawPath(path, stroke=1, fill=0)
        else:
            canvas.setFillColor(line_color)
            canvas.circle(px(group[0][0]), py(group[0][1]), 1.5, fill=1, stroke=0)

    marks = []
    for first, last, peak, median in peaks[:6]:
        center = first + (last - first) / 2
        value_y = py(peak)
        canvas.setStrokeColor(_color("#b02a5b"))
        canvas.setLineWidth(1.6)
        canvas.line(px(first), value_y, px(last), value_y)
        canvas.setLineWidth(0.9)
        canvas.line(px(center), py(median), px(center), value_y)
        canvas.setFillColor(_color("#b02a5b"))
        canvas.circle(px(center), value_y, 1.6, fill=1, stroke=0)
        marks.append((center, peak))

    overflow_marks = list(overflow)
    if overflow_marks:
        canvas.setFillColor(_color("#343a40"))
        for stamp, _real in overflow_marks:
            cx = px(stamp)
            cy = y + height
            path = canvas.beginPath()
            path.moveTo(cx, cy + 4)
            path.lineTo(cx + 3.5, cy - 2)
            path.lineTo(cx - 3.5, cy - 2)
            path.close()
            canvas.drawPath(path, fill=1, stroke=0)

    placed: list[tuple[datetime, bool]] = []
    candidates = list(dict.fromkeys((*marks, *overflow_marks)))
    for stamp, real in sorted(candidates, key=lambda item: -item[1]):
        above = real > top
        nearby = [
            other
            for other, other_above in placed
            if abs(stamp - other) < (end - start) * 0.12 and (other_above or not above)
        ]
        if not above and (sum(not other_above for _, other_above in placed) >= max_labels or nearby):
            continue
        stack = len(nearby) if above else 0
        label_y = y + height - 8 - 9 * stack if py(real) >= y + height * 0.88 else py(real) + 4 + 9 * stack
        align = "right" if (stamp - start) / max(end - start, timedelta(seconds=1)) > 0.5 else "left"
        shade = "#b02a5b" if real > max(top * 3, 1.0) else "#343a40"
        number = locale.format_number(real, max_fraction=0 if real >= 10 else 1, min_fraction=0 if real >= 10 else 1)
        _text(
            canvas,
            draw_text,
            px(stamp) + (-4 if align == "right" else 4),
            label_y,
            f"{number} ms",
            font=font,
            size=7,
            color=shade,
            align=align,
        )
        placed.append((stamp, above))

    entries = [("Median per interval" if peaks else "Measured", "line", color)]
    if peaks:
        entries.append(("Peak within interval (bucket maximum)", "line", "#b02a5b"))
    if overflow_marks:
        entries.append(("Above scale (value labelled)", "triangle", "#343a40"))
    return entries


def line_chart(
    *,
    width: float,
    height: float,
    times: list[datetime],
    values: list[float],
    start: datetime,
    end: datetime,
    color: str,
    ylabel: str,
    top: float,
    ticks: list[float],
    peaks: list[tuple[datetime, datetime, float, float]],
    overflow: list[tuple[datetime, float]],
    sla: float | None,
    sla_label: str | None,
    plot_left: float,
    legend_below: bool,
    timezone: tzinfo,
    font: str,
    draw_text: DrawText,
    text_width: TextWidth,
    locale: PDFLocale = DEFAULT_PDF_LOCALE,
    max_labels: int = 3,
) -> ChartFlowable:
    def draw(canvas):
        plot_x = max(plot_left, 18)
        plot_width = width - plot_x
        bottom = 58 if legend_below else 43
        plot_y = bottom
        plot_height = height - bottom - (18 if legend_below else 26)
        axis_color = _color("#e9ecef")
        canvas.setStrokeColor(axis_color)
        canvas.setLineWidth(0.45)
        step = ticks[1] - ticks[0] if len(ticks) > 1 else 1
        for value in ticks:
            y = plot_y + plot_height * value / top
            canvas.line(plot_x, y, width, y)
            label = locale.format_number(value) if step >= 1 else locale.format_number(value, max_fraction=6)
            _text(canvas, draw_text, plot_x - 4, y - 2, label, font=font, size=8, align="right")
        canvas.setStrokeColor(_color("#adb5bd"))
        canvas.line(plot_x, plot_y, plot_x, plot_y + plot_height)
        canvas.line(plot_x, plot_y, width, plot_y)
        _text(canvas, draw_text, plot_x, plot_y + plot_height + 5, ylabel, font=font, size=8, color="#495057")

        entries = _draw_series(
            canvas,
            x=plot_x,
            y=plot_y,
            width=plot_width,
            height=plot_height,
            start=start,
            end=end,
            times=times,
            values=values,
            color=color,
            top=top,
            peaks=peaks,
            overflow=overflow,
            max_labels=max_labels,
            draw_text=draw_text,
            font=font,
            locale=locale,
        )
        if sla is not None:
            name = sla_label or f"SLA threshold ({locale.format_number(sla, max_fraction=2)} ms)"
            if sla <= top:
                y = plot_y + plot_height * sla / top
                canvas.setStrokeColor(_color("#f0a202"))
                canvas.setLineWidth(1.1)
                canvas.setDash(5, 3)
                canvas.line(plot_x, y, width, y)
                canvas.setDash()
                entries.append((name, "dash", "#f0a202"))
            else:
                entries.append((f"{name} – above scale", "dash", "#f0a202"))
                cx, cy = width - 4, plot_y + plot_height
                path = canvas.beginPath()
                path.moveTo(cx, cy + 4)
                path.lineTo(cx + 3.5, cy - 2)
                path.lineTo(cx - 3.5, cy - 2)
                path.close()
                canvas.setFillColor(_color("#f0a202"))
                canvas.drawPath(path, fill=1, stroke=0)
                _text(
                    canvas,
                    draw_text,
                    width - 8,
                    cy - 8,
                    f"SLA {locale.format_number(sla)} ms (above scale)",
                    font=font,
                    size=7,
                    color="#8a5200",
                    align="right",
                )
        _axis_x(
            canvas,
            x=plot_x,
            y=plot_y,
            width=plot_width,
            start=start,
            end=end,
            tz=timezone,
            draw_text=draw_text,
            font=font,
            locale=locale,
        )
        if legend_below:
            _legend(
                canvas,
                entries,
                x=plot_x,
                max_width=width - plot_x,
                y=18,
                font=font,
                size=8,
                text_width=text_width,
                draw_text=draw_text,
                columns=2,
                align="center",
            )
        else:
            columns = 2 if len(entries) > 3 else len(entries)
            _legend(
                canvas,
                entries,
                x=width,
                max_width=width - plot_x,
                y=height - 13,
                font=font,
                size=8,
                text_width=text_width,
                draw_text=draw_text,
                columns=columns,
                align="right",
            )

    return ChartFlowable(width, height, draw)


def uptime_chart(
    *,
    width: float,
    height: float,
    start: datetime,
    end: datetime,
    blocks: dict[str, list[tuple[datetime, datetime]]],
    plot_left: float,
    timezone: tzinfo,
    font: str,
    draw_text: DrawText,
    text_width: TextWidth,
    locale: PDFLocale = DEFAULT_PDF_LOCALE,
) -> ChartFlowable:
    def draw(canvas):
        plot_x, plot_width = max(0, plot_left), width - max(0, plot_left)
        plot_y, bar_height = 36, 22
        span = max((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds(), 1)
        canvas.setFillColor(colors.white)
        canvas.setStrokeColor(_color("#adb5bd"))
        canvas.setLineWidth(0.6)
        canvas.rect(plot_x, plot_y, plot_width, bar_height, fill=1, stroke=1)
        for key, shade in (("up", "#198754"), ("down", "#dc3545")):
            canvas.setFillColor(_color(shade))
            for first, last in blocks[key]:
                left = plot_x + plot_width * (first.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / span
                right = plot_x + plot_width * (last.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / span
                canvas.rect(left, plot_y, max(0, right - left), bar_height, fill=1, stroke=0)
        _axis_x(
            canvas,
            x=plot_x,
            y=plot_y,
            width=plot_width,
            start=start,
            end=end,
            tz=timezone,
            draw_text=draw_text,
            font=font,
            locale=locale,
        )
        entries = [("Up", "square", "#198754"), ("Down", "square", "#dc3545"), ("No measurement", "outline", "#adb5bd")]
        _legend(
            canvas,
            entries,
            x=width,
            max_width=width - plot_x,
            y=height - 14,
            font=font,
            size=8,
            text_width=text_width,
            draw_text=draw_text,
            columns=3,
            align="right",
        )

    return ChartFlowable(width, height, draw)


def coverage_chart(
    *,
    width: float,
    height: float,
    start: datetime,
    end: datetime,
    blocks: list[tuple[datetime, datetime]],
    plot_left: float,
    timezone: tzinfo,
    font: str,
    draw_text: DrawText,
    text_width: TextWidth,
    locale: PDFLocale = DEFAULT_PDF_LOCALE,
) -> ChartFlowable:
    def draw(canvas):
        plot_x, plot_width = max(0, plot_left), width - max(0, plot_left)
        plot_y, bar_height = 20, 12
        canvas.saveState()
        clip = canvas.beginPath()
        clip.rect(plot_x, plot_y, plot_width, bar_height)
        canvas.clipPath(clip, stroke=0, fill=0)
        canvas.setFillColor(colors.white)
        canvas.setStrokeColor(_color("#adb5bd"))
        canvas.setLineWidth(0.6)
        canvas.rect(plot_x, plot_y, plot_width, bar_height, fill=1, stroke=1)
        canvas.setStrokeColor(_color("#adb5bd"))
        for x in range(int(plot_x - bar_height), int(plot_x + plot_width + bar_height), 6):
            canvas.line(x, plot_y, x + bar_height, plot_y + bar_height)
        span = max((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds(), 1)
        canvas.setFillColor(_color("#2b5fa8"))
        canvas.setStrokeColor(_color("#2b5fa8"))
        for first, last in blocks:
            left = plot_x + plot_width * (first.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / span
            right = plot_x + plot_width * (last.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / span
            canvas.rect(left, plot_y, max(0, right - left), bar_height, fill=1, stroke=0)
        canvas.restoreState()
        canvas.setStrokeColor(_color("#adb5bd"))
        canvas.rect(plot_x, plot_y, plot_width, bar_height, fill=0, stroke=1)
        _axis_x(
            canvas,
            x=plot_x,
            y=plot_y,
            width=plot_width,
            start=start,
            end=end,
            tz=timezone,
            draw_text=draw_text,
            font=font,
            locale=locale,
            xlabel=False,
        )
        entries = [("Checks recorded", "square", "#2b5fa8"), ("No measurement", "outline", "#adb5bd")]
        _legend(
            canvas,
            entries,
            x=width,
            max_width=width - plot_x,
            y=height - 6,
            font=font,
            size=8,
            text_width=text_width,
            draw_text=draw_text,
            columns=2,
            align="right",
        )

    return ChartFlowable(width, height, draw)


def multi_port_chart(
    *,
    width: float,
    height: float,
    ports: list[dict],
    start: datetime,
    end: datetime,
    plot_left: float,
    timezone: tzinfo,
    font: str,
    draw_text: DrawText,
    text_width: TextWidth,
    locale: PDFLocale = DEFAULT_PDF_LOCALE,
) -> ChartFlowable:
    def draw(canvas):
        plot_x = max(plot_left, 18)
        plot_width = width - plot_x
        top_margin, bottom_margin = 25, 44
        gap = 13
        panel_height = max(18, (height - top_margin - bottom_margin - gap * (len(ports) - 1)) / len(ports))
        _text(canvas, draw_text, 0, height - 9, "TCP latency (ms)", font=font, size=8)
        shared_entries: list[tuple[str, str, str]] = []
        for index, port in enumerate(ports):
            y = height - top_margin - (index + 1) * panel_height - index * gap
            top = port["top"]
            ticks = port["ticks"]
            step = ticks[1] - ticks[0] if len(ticks) > 1 else 1
            for value in ticks:
                py = y + panel_height * value / top
                canvas.setStrokeColor(_color("#e9ecef"))
                canvas.setLineWidth(0.4)
                canvas.line(plot_x, py, width, py)
                _text(
                    canvas,
                    draw_text,
                    plot_x - 4,
                    py - 2,
                    locale.format_number(value) if step >= 1 else locale.format_number(value, max_fraction=6),
                    font=font,
                    size=7,
                    align="right",
                )
            color = port["color"]
            _text(
                canvas, draw_text, plot_x, y + panel_height + 3, f"Port {port['port']}", font=font, size=8, color=color
            )
            entries = _draw_series(
                canvas,
                x=plot_x,
                y=y,
                width=plot_width,
                height=panel_height,
                start=start,
                end=end,
                times=port["times"],
                values=port["values"],
                color=color,
                top=top,
                peaks=port["peaks"],
                overflow=port["overflow"],
                max_labels=1,
                draw_text=draw_text,
                font=font,
                locale=locale,
            )
            for entry in entries:
                keep = entry[0] in ("Peak within interval (bucket maximum)", "Above scale (value labelled)")
                if keep and entry not in shared_entries:
                    shared_entries.append(entry)
            canvas.setStrokeColor(_color("#adb5bd"))
            canvas.line(plot_x, y, width, y)
        last_y = height - top_margin - len(ports) * (panel_height + gap) + gap
        _axis_x(
            canvas,
            x=plot_x,
            y=last_y,
            width=plot_width,
            start=start,
            end=end,
            tz=timezone,
            draw_text=draw_text,
            font=font,
            locale=locale,
        )
        if shared_entries:
            _legend(
                canvas,
                shared_entries,
                x=width,
                max_width=width - plot_x,
                y=height - 13,
                font=font,
                size=8,
                text_width=text_width,
                draw_text=draw_text,
                columns=2,
                align="right",
            )

    return ChartFlowable(width, height, draw)


def status_code_chart(
    *,
    width: float,
    height: float,
    codes: list[tuple[int, int, str]],
    total: int,
    exact: bool,
    font: str,
    font_bold: str,
    draw_text: DrawText,
    locale: PDFLocale = DEFAULT_PDF_LOCALE,
) -> ChartFlowable:
    def draw(canvas):
        side = min(height * 0.92, width * 0.36)
        cx, cy = side / 2 + 3, height / 2
        radius = side * 0.43
        angle = 90.0
        for _code, count, shade in codes:
            extent = -360.0 * count / max(total, 1)
            canvas.setFillColor(_color(shade))
            canvas.setStrokeColor(colors.white)
            canvas.setLineWidth(1.2)
            canvas.wedge(cx - radius, cy - radius, cx + radius, cy + radius, angle, extent, stroke=1, fill=1)
            angle += extent
        canvas.setFillColor(colors.white)
        canvas.circle(cx, cy, radius * 0.58, fill=1, stroke=0)
        _text(
            canvas,
            draw_text,
            cx,
            cy + 3,
            locale.format_number(total),
            font=font_bold,
            size=13,
            color="#12294a",
            align="center",
        )
        _text(
            canvas,
            draw_text,
            cx,
            cy - 12,
            "checks" if exact else "samples",
            font=font,
            size=8,
            color="#6c757d",
            align="center",
        )
        x0 = side + 25
        row_h = min(14.4, height * 0.84 / max(len(codes), 1))
        row_y = cy + row_h * (len(codes) - 1) / 2
        _text(canvas, draw_text, x0 + 20, row_y + row_h, "Status code", font=font, size=8, color="#6c757d")
        _text(
            canvas,
            draw_text,
            x0 + width * 0.39,
            row_y + row_h,
            "Checks" if exact else "Samples",
            font=font,
            size=8,
            color="#6c757d",
            align="right",
        )
        _text(canvas, draw_text, width - 2, row_y + row_h, "Share", font=font, size=8, color="#6c757d", align="right")
        for code, count, shade in codes:
            canvas.setFillColor(_color(shade))
            canvas.rect(x0, row_y - 1, 9, 9, fill=1, stroke=0)
            label = f"HTTP {code}  {status_label(code)}" if code else "Error  No response"
            _text(canvas, draw_text, x0 + 15, row_y, label, font=font, size=8.5, color="#212529")
            _text(
                canvas,
                draw_text,
                x0 + width * 0.39,
                row_y,
                locale.format_number(count),
                font=font,
                size=8.5,
                color="#212529",
                align="right",
            )
            _text(
                canvas,
                draw_text,
                width - 2,
                row_y,
                locale.format_percent(count / max(total, 1) * 100, 1),
                font=font,
                size=8.5,
                color="#212529",
                align="right",
            )
            row_y -= row_h

    return ChartFlowable(width, height, draw)


def status_label(code: int) -> str:
    return {
        0: "No response",
        1: "1xx Informational",
        2: "2xx Success",
        3: "3xx Redirect",
        4: "4xx Client error",
        5: "5xx Server error",
    }[code // 100]
