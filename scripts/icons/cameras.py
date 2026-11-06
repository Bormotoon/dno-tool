"""Режимы наблюдения камер: список, равномерные сетки и большая камера."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from math import isqrt

from ._base import fi, st

LAYOUTS = {"list": 0, "1": 1, "4": 4, "6": 6, "6_focus": 61, "9": 9, "16": 16}


def layout_inner(count: int, mode: str, color: str, frame: str = "") -> str:
    """Крупные панели с одинаковыми полями, читаемые на кнопке 32×32."""
    if count == 0:
        return "".join(
            f'<rect x="32" y="{y}" width="12" height="12" rx="3" {fi(color)}/>'
            f'<rect x="52" y="{y + 2}" width="44" height="8" rx="4" {fi(color)}/>'
            for y in (36, 58, 80)
        )
    result = (
        f'<rect x="27" y="29" width="74" height="70" rx="8" '
        f'{fi(frame) if frame else st(color, 4)}/>'
    )
    if count == 61:
        rows = columns = 3
        cells = ((1, 1, 2, 2), (0, 0, 1, 1), (0, 1, 1, 1),
                 (0, 2, 1, 1), (1, 0, 1, 1), (2, 0, 1, 1))
    else:
        rows, columns = (2, 3) if count == 6 else (isqrt(count), isqrt(count))
        cells = tuple((index // columns, index % columns, 1, 1) for index in range(count))
    gap = 4
    width = (60 - gap * (columns - 1)) / columns
    height = (56 - gap * (rows - 1)) / rows
    for row, column, row_span, column_span in cells:
        style = st(color, 2.5) if mode == "line" else fi(color)
        result += (
            f'<rect x="{34 + column * (width + gap):g}" '
            f'y="{36 + row * (height + gap):g}" '
            f'width="{width * column_span + gap * (column_span - 1):g}" '
            f'height="{height * row_span + gap * (row_span - 1):g}" '
            f'rx="{min(3, width / 5):g}" {style}/>'
        )
    if mode == "badge":
        return f'<g transform="translate(64 64) scale(0.84) translate(-64 -64)">{result}</g>'
    return result


def _painter(count: int):
    def paint(mode: str, color: str) -> str:
        return layout_inner(count, mode, color)

    return paint


PAINTERS = {}
COLORS = {}
for _suffix, _count in LAYOUTS.items():
    for _selected in (False, True):
        _name = f"camera_view_{_suffix}" + ("_on" if _selected else "")
        PAINTERS[_name] = _painter(_count)
        COLORS[_name] = "#22C55E" if _selected else "#3B82F6"


def legacy_svg(name: str, template: bytes) -> str:
    """Использовать родные подложку и цвета пяти исходных тем."""
    root = ET.fromstring(template)
    color, frame = "#007AFF", "#2B2F42"
    for node in list(root):
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "rect" and node.get("x") == "30":
            color = node.get("fill", color)
        if tag == "rect" and node.get("x") == "28":
            frame = node.get("fill", frame)
        if tag != "defs" and not (tag == "rect" and node.get("x") == "8"):
            root.remove(node)
    suffix = name.removeprefix("camera_view_").removesuffix("_on")
    inner = layout_inner(LAYOUTS[suffix], "legacy", color, frame)
    root.append(ET.fromstring(f'<g xmlns="http://www.w3.org/2000/svg">{inner}</g>'))
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode")
