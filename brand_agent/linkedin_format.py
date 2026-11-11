"""Converts a draft into LinkedIn's Posts API "little text" commentary.

LinkedIn posts have no real rich text. Two things matter:
1. Reserved characters  | { } @ [ ] ( ) < > # \\ * _ ~  must be backslash-escaped,
   otherwise LinkedIn may treat them as markup and silently truncate the post.
2. Bold is only possible with Unicode "mathematical sans-serif bold" letters.
   Drafts mark bold with **double asterisks**; this module converts them.
"""
from __future__ import annotations

import re

RESERVED = set("\\|{}@[]()<>#*_~")
HASHTAG = re.compile(r"(?<![\w&/])#([A-Za-z][A-Za-z0-9_]{0,60})")
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def unicode_bold(text: str) -> str:
    """Sans-serif bold for ASCII letters and digits; other characters unchanged."""
    out = []
    for char in text:
        if "A" <= char <= "Z":
            out.append(chr(0x1D5D4 + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            out.append(chr(0x1D5EE + ord(char) - ord("a")))
        elif "0" <= char <= "9":
            out.append(chr(0x1D7EC + ord(char) - ord("0")))
        else:
            out.append(char)
    return "".join(out)


def escape_little_text(text: str) -> str:
    return "".join(f"\\{char}" if char in RESERVED else char for char in text)


def to_linkedin_commentary(body: str) -> str:
    """Bold markers -> Unicode bold, #tags -> hashtag templates, everything else escaped."""
    body = BOLD.sub(lambda match: unicode_bold(match.group(1)), body)
    parts: list[str] = []
    position = 0
    for match in HASHTAG.finditer(body):
        parts.append(escape_little_text(body[position : match.start()]))
        parts.append("{hashtag|\\#|" + escape_little_text(match.group(1)) + "}")
        position = match.end()
    parts.append(escape_little_text(body[position:]))
    return "".join(parts)
