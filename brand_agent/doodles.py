"""Hand-drawn (Excalidraw-style) explainer doodles that mirror a post's content.

The AI describes the idea as a small structured diagram (flow / compare / bars);
this module draws it with sketchy strokes, hatching and a handwritten font.
Drawing is deterministic (seeded by the title), so previews match the post.
"""
from __future__ import annotations

import hashlib
import io
import math
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).resolve().parent / "static" / "fonts"
WIDTH, HEIGHT = 1080, 1350
MARGIN = 90

PAPER = (250, 247, 240)
DOT = (226, 220, 208)
INK = (31, 36, 48)
MUTED = (110, 116, 128)
GREEN = (47, 158, 68)
RED = (224, 49, 49)
BLUE = (28, 126, 214)
HIGHLIGHT = (255, 214, 64, 150)
FILLS = {"good": (211, 249, 216), "bad": (255, 227, 227), "neutral": (231, 245, 255)}
STROKES = {"good": GREEN, "bad": RED, "neutral": BLUE}
LAYOUTS = ("flow", "compare", "bars")


@lru_cache(maxsize=32)
def _hand(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "PatrickHand-Regular.ttf"), size)


@lru_cache(maxsize=8)
def _sans(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / "Inter-Regular.ttf"), size)


class Sketch:
    """Rough strokes in the spirit of rough.js: every line is drawn twice with jitter."""

    def __init__(self, image: Image.Image, seed: str):
        self.image = image
        self.draw = ImageDraw.Draw(image, "RGBA")
        self.rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16))

    def _j(self, amount: float = 2.5) -> float:
        return self.rng.uniform(-amount, amount)

    def line(self, x0, y0, x1, y1, color=INK, width=3, passes=2, bow=3.0):
        for _ in range(passes):
            mx, my = (x0 + x1) / 2 + self._j(bow), (y0 + y1) / 2 + self._j(bow)
            points = [(x0 + self._j(), y0 + self._j()), (mx, my), (x1 + self._j(), y1 + self._j())]
            self.draw.line(points, fill=color, width=width, joint="curve")

    def rect(self, x0, y0, x1, y1, color=INK, fill=None, hachure=None, width=3):
        if fill:
            self.draw.rectangle([x0 + 4, y0 + 4, x1 - 2, y1 - 2], fill=fill)
        if hachure:
            self.hachure(x0, y0, x1, y1, hachure)
        self.line(x0, y0, x1, y0, color, width)
        self.line(x1, y0, x1, y1, color, width)
        self.line(x1, y1, x0, y1, color, width)
        self.line(x0, y1, x0, y0, color, width)

    def hachure(self, x0, y0, x1, y1, color, gap=16):
        w, h = x1 - x0, y1 - y0
        t = gap
        while t < w + h:
            ax, ay = (x0 + t, y0) if t <= w else (x1, y0 + t - w)
            bx, by = (x0, y0 + t) if t <= h else (x0 + t - h, y1)
            self.line(ax, ay, bx, by, color, width=2, passes=1, bow=1.5)
            t += gap

    def arrow(self, x0, y0, x1, y1, color=INK):
        self.line(x0, y0, x1, y1, color, width=4)
        angle = math.atan2(y1 - y0, x1 - x0)
        for side in (-1, 1):
            a = angle + math.pi - side * 0.5
            self.line(x1, y1, x1 + 26 * math.cos(a), y1 + 26 * math.sin(a), color, width=4, passes=1, bow=0.5)

    def check(self, cx, cy, size=26):
        self.line(cx - size, cy, cx - size * 0.3, cy + size * 0.8, GREEN, width=7, passes=1, bow=1)
        self.line(cx - size * 0.3, cy + size * 0.8, cx + size, cy - size * 0.9, GREEN, width=7, passes=1, bow=1)

    def cross(self, cx, cy, size=24):
        self.line(cx - size, cy - size, cx + size, cy + size, RED, width=7, passes=1, bow=1)
        self.line(cx + size, cy - size, cx - size, cy + size, RED, width=7, passes=1, bow=1)

    def highlight(self, x0, y0, x1, y1):
        self.draw.rounded_rectangle([x0 - 8, y0 + self._j(3), x1 + 8, y1 + self._j(3)], radius=10, fill=HIGHLIGHT)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines, current = [], ""
    for word in str(text).split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _text_block(sketch: Sketch, text: str, x: int, y: int, width: int, size: int, color=INK, max_lines=3, center=False) -> int:
    font = _hand(size)
    lines = _wrap(sketch.draw, text, font, width)[:max_lines]
    for line in lines:
        lx = x + (width - sketch.draw.textlength(line, font=font)) / 2 if center else x
        sketch.draw.text((lx, y), line, font=font, fill=color)
        y += int(size * 1.15)
    return y


def _paper(seed: str) -> Sketch:
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    sketch = Sketch(image, seed)
    for gx in range(30, WIDTH, 36):
        for gy in range(30, HEIGHT, 36):
            sketch.draw.ellipse([gx - 1.5, gy - 1.5, gx + 1.5, gy + 1.5], fill=DOT)
    return sketch


def _title(sketch: Sketch, title: str) -> int:
    font = _hand(70)
    lines = _wrap(sketch.draw, title, font, WIDTH - 2 * MARGIN)[:3]
    y = 80
    for index, line in enumerate(lines):
        width = sketch.draw.textlength(line, font=font)
        if index == len(lines) - 1:
            sketch.highlight(MARGIN, y + 42, MARGIN + width, y + 82)
        sketch.draw.text((MARGIN, y), line, font=font, fill=INK)
        y += 80
    return y + 40


def _flow(sketch: Sketch, data: dict[str, Any], top: int, bottom: int) -> None:
    nodes = data.get("nodes", [])[:5]
    count = max(1, len(nodes))
    gap = 70
    box_h = min(170, (bottom - top - gap * (count - 1)) // count)
    x0, x1 = MARGIN + 30, WIDTH - MARGIN - 30
    y = top + max(0, (bottom - top - (box_h * count + gap * (count - 1))) // 2)
    for index, node in enumerate(nodes):
        status = node.get("status", "neutral") if node.get("status") in FILLS else "neutral"
        sketch.rect(x0, y, x1, y + box_h, color=INK, fill=FILLS[status])
        text_w = x1 - x0 - 150
        label_y = _text_block(sketch, node.get("label", ""), x0 + 30, y + 18, text_w, 46, max_lines=2)
        if node.get("note"):
            _text_block(sketch, node["note"], x0 + 30, label_y + 2, text_w, 32, color=MUTED, max_lines=1)
        cx, cy = x1 - 70, y + box_h / 2
        if status == "good":
            sketch.check(cx, cy)
        elif status == "bad":
            sketch.cross(cx, cy)
        if index < len(nodes) - 1:
            ax = (x0 + x1) / 2
            sketch.arrow(ax, y + box_h + 10, ax, y + box_h + gap - 10)
        y += box_h + gap


def _column_height(sketch: Sketch, column: dict[str, Any], col_w: int) -> int:
    height = 24 + len(_wrap(sketch.draw, column.get("title", ""), _hand(52), col_w - 48)[:2]) * 60 + 40
    for point in (column.get("points") or [])[:4]:
        height += len(_wrap(sketch.draw, point, _hand(38), col_w - 82)[:3]) * 44 + 22
    return height + 30


def _compare(sketch: Sketch, data: dict[str, Any], top: int, bottom: int) -> None:
    columns = [data.get("left") or {}, data.get("right") or {}]
    col_w = (WIDTH - 2 * MARGIN - 60) // 2
    # Size both columns to the taller one's content and centre them vertically.
    content = max(_column_height(sketch, column, col_w) for column in columns)
    content = min(bottom - top, max(content, 420))
    top = top + (bottom - top - content) // 2
    bottom = top + content
    for index, column in enumerate(columns):
        x0 = MARGIN + index * (col_w + 60)
        status = column.get("status") if column.get("status") in FILLS else ("bad" if index == 0 else "good")
        sketch.rect(x0, top, x0 + col_w, bottom, color=STROKES[status], fill=FILLS[status])
        y = _text_block(sketch, column.get("title", ""), x0 + 24, top + 24, col_w - 48, 52, color=STROKES[status], max_lines=2, center=True)
        sketch.line(x0 + 40, y + 6, x0 + col_w - 40, y + 6, STROKES[status], width=3, passes=1)
        y += 34
        for point in (column.get("points") or [])[:4]:
            sketch.line(x0 + 26, y + 26, x0 + 46, y + 26, INK, width=4, passes=1, bow=0.5)
            y = _text_block(sketch, point, x0 + 58, y, col_w - 82, 38, max_lines=3) + 22
    vs_font = _hand(56)
    cx = WIDTH / 2
    sketch.draw.ellipse([cx - 38, (top + bottom) / 2 - 38, cx + 38, (top + bottom) / 2 + 38], fill=PAPER, outline=INK, width=3)
    sketch.draw.text((cx, (top + bottom) / 2), "vs", font=vs_font, fill=INK, anchor="mm")


def _bars(sketch: Sketch, data: dict[str, Any], top: int, bottom: int) -> None:
    bars = [bar for bar in (data.get("bars") or []) if isinstance(bar.get("value"), (int, float))][:5]
    if not bars:
        return
    maximum = max([float(data.get("max") or 0)] + [float(bar["value"]) for bar in bars]) or 1
    unit = str(data.get("unit", ""))
    slot = min(210, (bottom - top) / len(bars))
    top = top + (bottom - top - slot * len(bars)) / 2  # keep the group compact and centred
    bar_h = min(90, slot * 0.45)
    full_w = WIDTH - 2 * MARGIN - 160
    for index, bar in enumerate(bars):
        y = top + index * slot + (slot - bar_h - 60) / 2
        _text_block(sketch, bar.get("label", ""), MARGIN, y, WIDTH - 2 * MARGIN, 40, max_lines=1)
        by = y + 56
        length = max(30, full_w * float(bar["value"]) / maximum)
        color = BLUE if index % 2 == 0 else GREEN
        if bar.get("highlight"):
            color = RED
        sketch.rect(MARGIN, by, MARGIN + length, by + bar_h, color=color, hachure=color)
        value = f"{bar['value']:g}{unit}"
        sketch.draw.text((MARGIN + length + 20, by + bar_h / 2), value, font=_hand(48), fill=INK, anchor="lm")


def render_doodle(data: dict[str, Any]) -> bytes:
    layout = data.get("layout") if data.get("layout") in LAYOUTS else "flow"
    sketch = _paper(str(data.get("title", "")))
    top = _title(sketch, data.get("title", ""))
    caption = data.get("caption", "")
    bottom = HEIGHT - (300 if caption else 200)
    {"flow": _flow, "compare": _compare, "bars": _bars}[layout](sketch, data, top, bottom)

    if caption:
        _text_block(sketch, caption, MARGIN, bottom + 40, WIDTH - 2 * MARGIN, 40, color=INK, max_lines=2)
    footer_y = HEIGHT - 110
    sketch.line(MARGIN, footer_y, WIDTH - MARGIN, footer_y, DOT, width=2, passes=1, bow=1)
    source = f"Source: {data.get('source', '')}"
    status_note = data.get("status_note", "")
    font = _sans(22)
    text = source if len(source) < 90 else source[:87] + "..."
    sketch.draw.text((MARGIN, footer_y + 22), text, font=font, fill=MUTED)
    if status_note:
        sketch.draw.text((MARGIN, footer_y + 52), status_note, font=font, fill=MUTED)
    if data.get("author"):
        sketch.draw.text((WIDTH - MARGIN, footer_y + 64), data["author"], font=_hand(32), fill=INK, anchor="rm")

    buffer = io.BytesIO()
    sketch.image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
