# Copyright 2026 The pubkit Authors
# SPDX-License-Identifier: Apache-2.0
"""Render a semantic Table into a PNG.

Medium has no tables. Neither does X, nor LinkedIn articles, nor most editors
that were designed around prose. The planner spots this and says
`table_to_image`; this module is what actually makes the image.

Two things it is careful about, both learned by hand:

* **Legibility beats fidelity.** These are read at ~680 CSS pixels inside a
  column of text. Rendering at 2x and letting the platform downscale is what
  makes small figures readable on a retina screen and acceptable on a laptop.

* **Flat colour compresses.** A table is large areas of one colour and thin
  strokes of another. Quantising to a small palette with no dither takes a
  150 KB PNG to 25 KB with no visible loss, and upload time is the slowest step
  of a browser publish.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ..core.ir import Table

try:  # pragma: no cover - optional dependency
    from PIL import Image, ImageDraw, ImageFont

    _HAVE_PIL = True
except Exception:  # pragma: no cover
    _HAVE_PIL = False


FONT_CANDIDATES = {
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ],
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "mono": [
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "C:/Windows/Fonts/consola.ttf",
    ],
}


@dataclass
class TableStyle:
    """Muted, print-ish defaults that sit quietly inside an article."""

    scale: int = 2
    width: int = 700               # CSS pixels; the PNG is scale× this
    font_size: int = 15
    header_size: int = 14
    padding: int = 14
    row_gap: int = 11
    bg: tuple = (255, 255, 255)
    fg: tuple = (26, 26, 26)
    muted: tuple = (110, 110, 110)
    rule: tuple = (222, 222, 222)
    header_rule: tuple = (40, 40, 40)
    accent: tuple = (12, 111, 121)
    zebra: tuple = (250, 250, 249)
    colors: int = 32               # palette size after quantisation
    font_paths: dict = field(default_factory=dict)


class RenderUnavailable(RuntimeError):
    """Pillow is not installed. `pip install "pubkit[render]"`."""


def _font(kind: str, size: int, style: TableStyle):
    for p in [style.font_paths.get(kind)] + FONT_CANDIDATES[kind]:
        if p and Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # pragma: no cover - bad font file
                continue
    return ImageFont.load_default(size)


def _wrap(text: str, font, max_w: int, draw) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def render_table(table: Table, out: Path, style: TableStyle | None = None) -> Path:
    """Render `table` to `out` as a PNG. Returns the path."""
    if not _HAVE_PIL:
        raise RenderUnavailable(
            'rendering tables as images needs Pillow: pip install "pubkit[render]"'
        )
    style = style or TableStyle()
    s = style.scale
    W = style.width * s
    pad = style.padding * s

    f_head = _font("bold", style.header_size * s, style)
    f_cell = _font("regular", style.font_size * s, style)
    f_cap = _font("regular", int(style.font_size * 0.9) * s, style)

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    ncols = len(table.header)
    align = table.align or ["l"] * ncols

    # Column widths from natural content width, then scaled to fit.
    natural = []
    for c in range(ncols):
        cells = [table.header[c]] + [r[c] for r in table.rows]
        natural.append(max(probe.textlength(x, font=f_cell) for x in cells) + pad * 1.6)
    total = sum(natural)
    avail = W - pad * 2
    widths = [max(int(n * avail / total), int(60 * s)) for n in natural]
    # Give any rounding slack to the widest column rather than the last one.
    slack = avail - sum(widths)
    widths[natural.index(max(natural))] += slack

    def row_height(cells: list[str], font) -> tuple[int, list[list[str]]]:
        wrapped = [
            _wrap(cell, font, widths[c] - int(pad * 0.9), probe) for c, cell in enumerate(cells)
        ]
        lines = max(len(w) for w in wrapped)
        return lines * (font.size + style.row_gap * s // 2) + style.row_gap * s, wrapped

    head_h, head_wrapped = row_height(table.header, f_head)
    body = [row_height(r, f_cell) for r in table.rows]
    cap_h = (f_cap.size + style.row_gap * s) if table.caption else 0
    H = pad + head_h + sum(h for h, _ in body) + pad + cap_h

    img = Image.new("RGB", (W, int(H)), style.bg)
    d = ImageDraw.Draw(img)

    def draw_row(y: int, wrapped: list[list[str]], font, color) -> None:
        x = pad
        for c, lines in enumerate(wrapped):
            ly = y + style.row_gap * s // 2
            for line in lines:
                tw = d.textlength(line, font=font)
                if align[c] == "r":
                    tx = x + widths[c] - tw - pad * 0.45
                elif align[c] == "c":
                    tx = x + (widths[c] - tw) / 2
                else:
                    tx = x + pad * 0.45
                d.text((tx, ly), line, font=font, fill=color)
                ly += font.size + style.row_gap * s // 2
            x += widths[c]

    y = pad
    draw_row(y, head_wrapped, f_head, style.accent)
    y += head_h
    d.line([(pad, y), (W - pad, y)], fill=style.header_rule, width=max(1, s))

    for i, (h, wrapped) in enumerate(body):
        if i % 2 == 1:
            d.rectangle([pad, y, W - pad, y + h], fill=style.zebra)
        draw_row(y, wrapped, f_cell, style.fg)
        y += h
        if i < len(body) - 1:
            d.line([(pad, y), (W - pad, y)], fill=style.rule, width=max(1, s // 2))

    d.line([(pad, y), (W - pad, y)], fill=style.header_rule, width=max(1, s))

    if table.caption:
        y += style.row_gap * s
        d.text((pad, y), table.caption, font=f_cap, fill=style.muted)

    # Flat-colour art quantises extremely well, and upload time is the slowest
    # step of a browser publish.
    out.parent.mkdir(parents=True, exist_ok=True)
    img.quantize(colors=style.colors, dither=Image.Dither.NONE).save(out, optimize=True)
    return out


def table_asset_id(table: Table, index: int) -> str:
    """Stable id so a re-run reuses the same rendered file."""
    payload = f"{table.header}|{table.rows}|{table.caption}"
    digest = hashlib.blake2b(payload.encode(), digest_size=4).hexdigest()
    return f"table-{index:02d}-{digest}"
