#!/usr/bin/env python3
#
# app/services/pdf_logo.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vector justUp! wordmark for PDF output, drawn from the web app's logo SVG.

The web UI, docs and the PDF share ``app/static/img/justup_1c.svg`` (black on
transparent, made for light backgrounds). Only the path data is read; the file is
a trusted repository asset, so no general SVG renderer is needed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

from reportlab.platypus import Flowable

LOGO_SVG_PATH = Path(__file__).resolve().parent.parent / "static" / "img" / "justup_1c.svg"

_SVG_NS = "{http://www.w3.org/2000/svg}"
_TOKEN = re.compile(r"[mMlLhHvVcCzZ]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ARGS = {"m": 2, "l": 2, "h": 1, "v": 1, "c": 6, "z": 0}


def _parse_path(data: str) -> list[tuple[str, tuple[float, ...]]]:
    """Return absolute ('M'|'L'|'C'|'Z', coords) segments for an SVG path (m/l/h/v/c/z)."""
    tokens = _TOKEN.findall(data)
    segments: list[tuple[str, tuple[float, ...]]] = []
    x = y = start_x = start_y = 0.0
    i = 0
    cmd = ""
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "zZ":
                segments.append(("Z", ()))
                x, y = start_x, start_y
                continue
        low = cmd.lower()
        n = _ARGS[low]
        args = [float(t) for t in tokens[i : i + n]]
        if len(args) != n:
            raise ValueError("truncated SVG path data")
        i += n
        rel = cmd.islower()
        if low == "h":
            x = x + args[0] if rel else args[0]
            segments.append(("L", (x, y)))
        elif low == "v":
            y = y + args[0] if rel else args[0]
            segments.append(("L", (x, y)))
        elif low == "c":
            pts = [(args[k] + (x if rel else 0.0), args[k + 1] + (y if rel else 0.0)) for k in (0, 2, 4)]
            segments.append(("C", tuple(v for p in pts for v in p)))
            x, y = pts[2]
        else:
            nx = args[0] + (x if rel else 0.0)
            ny = args[1] + (y if rel else 0.0)
            if low == "m":
                start_x, start_y = nx, ny
                segments.append(("M", (nx, ny)))
                # Further coordinate pairs after a moveto are implicit linetos.
                cmd = "l" if rel else "L"
            else:
                segments.append(("L", (nx, ny)))
            x, y = nx, ny
    return segments


@lru_cache(maxsize=1)
def load_logo() -> tuple[float, float, tuple[list[tuple[str, tuple[float, ...]]], ...]]:
    """Return (width, height, subpath lists) in SVG user units with the group offset applied."""
    root = ET.parse(LOGO_SVG_PATH).getroot()  # noqa: S314 - trusted local asset
    _, _, vb_w, vb_h = (float(v) for v in root.attrib["viewBox"].split())
    group = root.find(f"{_SVG_NS}g")
    tx = ty = 0.0
    if group is not None and group.get("transform"):
        m = re.fullmatch(r"translate\(([^,\s]+)[,\s]+([^)]+)\)", group.get("transform", ""))
        if not m:
            raise ValueError("unsupported logo transform")
        tx, ty = float(m.group(1)), float(m.group(2))
    paths = []
    for el in root.iter(f"{_SVG_NS}path"):
        segs = []
        for op, pts in _parse_path(el.attrib["d"]):
            shifted = tuple(v + (tx if k % 2 == 0 else ty) for k, v in enumerate(pts))
            segs.append((op, shifted))
        paths.append(segs)
    return vb_w, vb_h, tuple(paths)


def logo_aspect() -> float:
    """Height divided by width of the wordmark."""
    w, h, _ = load_logo()
    return h / w


def draw_logo(canvas, x: float, y: float, width: float, alpha: float = 1.0) -> float:
    """Draw the wordmark with its lower-left corner at (x, y); return its height."""
    vb_w, vb_h, paths = load_logo()
    scale = width / vb_w
    canvas.saveState()
    if alpha < 1.0:
        canvas.setFillAlpha(alpha)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.translate(x, y + vb_h * scale)
    canvas.scale(scale, -scale)
    for segs in paths:
        p = canvas.beginPath()
        for op, pts in segs:
            if op == "M":
                p.moveTo(*pts)
            elif op == "L":
                p.lineTo(*pts)
            elif op == "C":
                p.curveTo(*pts)
            else:
                p.close()
        canvas.drawPath(p, stroke=0, fill=1, fillMode=1)
    canvas.restoreState()
    return vb_h * scale


class LogoFlowable(Flowable):
    """Left-aligned wordmark of a fixed width for platypus stories."""

    def __init__(self, width: float):
        super().__init__()
        self.width = width
        self.height = width * logo_aspect()
        self.hAlign = "LEFT"

    def wrap(self, *_avail):
        return self.width, self.height

    def draw(self):
        draw_logo(self.canv, 0, 0, self.width)
