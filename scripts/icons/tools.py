"""Настройки, инструменты, SFTP.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    arrow_ud,
    fi,
    gear_shape,
    st,
    wrench,
)

PAINTERS: dict = {}


def settings(mode: str, color: str) -> str:
    """Цельный контур восьмизубой шестерёнки с отверстием в центре."""
    from math import cos, radians, sin

    points = []
    for tooth in range(8):
        for offset, radius in ((-17, 29), (-9, 40), (9, 40), (17, 29)):
            angle = radians(tooth * 45 + offset - 90)
            points.append(f"{64 + radius * cos(angle):.2f} {64 + radius * sin(angle):.2f}")
    outline = "M" + " L".join(points) + " Z"
    hole = "M77 64 A13 13 0 1 0 51 64 A13 13 0 1 0 77 64 Z"
    if mode == "line":
        return f'<path d="{outline} {hole}" {st(color, 5)}/>'
    return f'<path d="{outline} {hole}" fill="{color}" fill-rule="evenodd"/>'


def monitoring(mode: str, color: str) -> str:
    """Панель загрузки ресурсов с тремя столбцами разной высоты."""
    solid = mode == "solid"
    ink = INK if solid else color
    return (
        f'<rect x="30" y="32" width="68" height="64" rx="9" '
        f'{fi(color) if solid else st(color, 6)}/>'
        + "".join(
            f'<rect x="{x}" y="{88 - height}" width="9" height="{height}" '
            f'rx="3" {fi(ink)}/>'
            for x, height in ((42, 18), (60, 32), (78, 44))
        )
    )


PAINTERS["menu_monitoring"] = monitoring
PAINTERS["menu_settings"] = settings
PAINTERS["veyon_restart_service"] = lambda m, c: (
    gear_shape(m, c, 56, 56, 20, 8, 11)
    + (
        f'<path d="M78 84 C88 74 88 60 80 52" {st(c if m != "solid" else INK, 6)}/>'
        f'<path d="M72 50 L81 51 L79 60" {st(c if m != "solid" else INK, 6)}/>'
    )
)
PAINTERS["menu_tools"] = lambda m, c: (
    wrench(m, c)
    + (
        f'<line x1="36" y1="92" x2="58" y2="70" {st(c, 7)}/>'
        if m != "solid"
        else f'<line x1="36" y1="92" x2="58" y2="70" {st(c, 8)}/>'
    )
)
PAINTERS["menu_sftp"] = lambda m, c: (
    (
        f'<ellipse cx="48" cy="42" rx="20" ry="9" {fi(c)}/>'
        f'<path d="M28 42 V86 C28 93 68 93 68 86 V42" {fi(c)}/>'
        f'<ellipse cx="48" cy="42" rx="12" ry="5" {fi(INK)}/>'
        + arrow_ud("solid", c, "down", x=88, y1=38, y2=62)
        + arrow_ud("solid", c, "up", x=88, y1=68, y2=92)
    )
    if m == "solid"
    else (
        f'<ellipse cx="48" cy="42" rx="20" ry="9" {st(c, 6)}/>'
        f'<path d="M28 42 V86 C28 93 68 93 68 86 V42" {st(c, 6)}/>'
        + arrow_ud("line", c, "down", x=88, y1=38, y2=60)
        + arrow_ud("line", c, "up", x=88, y1=68, y2=90)
    )
)

COLORS = {
    "menu_monitoring": "#14B8A6",
    "menu_settings": "#8A8F98",
    "veyon_restart_service": "#0284C7",
    "menu_tools": "#F97316",
    "menu_sftp": "#06B6D4",
}
