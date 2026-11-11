"""Branded insight-card images (1080x1350 portrait) for LinkedIn posts.

Rendered on demand from the draft's card_json, so nothing needs to be stored
on Render's temporary disk. Every number on a card is verified against the
source before the card data is saved (see ai_research.build_card).
"""
from __future__ import annotations

import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).resolve().parent / "static"
FONTS = STATIC / "fonts"
WIDTH, HEIGHT = 1080, 1350
MARGIN = 84

BG_TOP = (9, 14, 27)
BG_BOTTOM = (17, 30, 48)
ACCENT = (114, 224, 185)
TEXT = (240, 245, 250)
MUTED = (150, 168, 190)
LINE = (44, 60, 82)

TOPIC_LABELS = {
    "cloud_for_ai": "CLOUD FOR AI",
    "ai_for_cloud": "AI FOR CLOUD",
    "ai_in_industry": "AI IN INDUSTRY",
    "agent_reliability": "AGENT RELIABILITY",
    "cloud_ai_general": "CLOUD × AI",
}


@lru_cache(maxsize=32)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Inter-{weight}.ttf"), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
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


def _fit(draw, text: str, weight: str, start: int, minimum: int, width: int, max_lines: int):
    """Largest font size at which `text` fits in `max_lines` lines."""
    size = start
    while size > minimum:
        font = _font(weight, size)
        lines = _wrap(draw, text, font, width)
        # A single long token (e.g. "41.3/100") cannot wrap, so also check width.
        if len(lines) <= max_lines and all(draw.textlength(line, font=font) <= width for line in lines):
            return font, lines
        size -= 4
    font = _font(weight, minimum)
    lines = _wrap(draw, text, font, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return font, lines


def _background() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    draw.rectangle([0, 0, 12, HEIGHT], fill=ACCENT)  # brand edge
    return image


def render_card(card: dict[str, Any]) -> bytes:
    image = _background()
    draw = ImageDraw.Draw(image)
    inner = WIDTH - 2 * MARGIN
    y = 80

    # Header: logo + brand, topic chip on the right.
    try:
        logo = Image.open(STATIC / "voiceloop-logo.png").convert("RGBA")
        logo.thumbnail((64, 64), Image.LANCZOS)
        image.paste(logo, (MARGIN, y), logo)
        brand_x = MARGIN + logo.width + 18
    except OSError:
        brand_x = MARGIN
    draw.text((brand_x, y + 32), "VOICELOOP", font=_font("SemiBold", 28), fill=MUTED, anchor="lm")
    topic = card.get("topic", "")
    if topic:
        chip_font = _font("SemiBold", 24)
        chip_w = draw.textlength(topic, font=chip_font) + 40
        x1 = WIDTH - MARGIN
        draw.rounded_rectangle([x1 - chip_w, y + 10, x1, y + 54], radius=22, outline=ACCENT, width=2)
        draw.text((x1 - chip_w / 2, y + 32), topic, font=chip_font, fill=ACCENT, anchor="mm")
    y += 150

    # Big verified number.
    stat = (card.get("stat") or "").strip()
    if stat:
        stat_font, _ = _fit(draw, stat, "ExtraBold", 230, 110, inner, 1)
        draw.text((MARGIN, y), stat, font=stat_font, fill=ACCENT)
        y += stat_font.size + 30
        label = (card.get("stat_label") or "").strip()
        if label:
            label_font, label_lines = _fit(draw, label, "SemiBold", 44, 32, inner, 2)
            for line in label_lines:
                draw.text((MARGIN, y), line, font=label_font, fill=TEXT)
                y += int(label_font.size * 1.3)
        y += 50
        draw.line([(MARGIN, y), (MARGIN + 120, y)], fill=ACCENT, width=6)
        y += 60

    # Headline.
    max_lines = 4 if stat else 7
    headline_font, headline_lines = _fit(draw, card.get("headline", ""), "ExtraBold", 76 if stat else 92, 50, inner, max_lines)
    for line in headline_lines:
        draw.text((MARGIN, y), line, font=headline_font, fill=TEXT)
        y += int(headline_font.size * 1.2)

    # Footer: source + status, author.
    footer_top = HEIGHT - 250
    draw.line([(MARGIN, footer_top), (WIDTH - MARGIN, footer_top)], fill=LINE, width=2)
    fy = footer_top + 34
    source_font = _font("Regular", 28)
    for line in _wrap(draw, f"Source: {card.get('source', '')}", source_font, inner)[:2]:
        draw.text((MARGIN, fy), line, font=source_font, fill=MUTED)
        fy += 40
    status_note = card.get("status_note", "")
    if status_note:
        draw.text((MARGIN, fy + 4), status_note, font=_font("SemiBold", 26), fill=ACCENT)
    author = card.get("author", "")
    if author:
        draw.text((WIDTH - MARGIN, HEIGHT - 70), author, font=_font("SemiBold", 28), fill=TEXT, anchor="rm")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def card_from_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        card = json.loads(value)
    except json.JSONDecodeError:
        return None
    return card if isinstance(card, dict) and card.get("enabled", True) and card.get("headline") else None


def alt_text(card: dict[str, Any]) -> str:
    parts = [card.get("headline", "")]
    if card.get("stat"):
        parts.append(f"{card['stat']}: {card.get('stat_label', '')}".strip(": "))
    parts.append(f"Source: {card.get('source', '')}")
    return ". ".join(part for part in parts if part)[:300]
