"""WCAG 2.x contrast utilities for the design-token check (PROMPT §2 rule 3)."""

from __future__ import annotations

import re

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")


def _channel(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    m = _HEX.match(hex_colour)
    if m is None:
        raise ValueError(f"expected #RRGGBB, got {hex_colour!r}")
    v = m.group(1)
    r, g, b = (int(v[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)
