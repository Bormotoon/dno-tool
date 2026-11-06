"""Рамка миниатюр и тулбарные переключатели.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    check,
    eye_shape,
    fi,
    grid4,
    image_shape,
    ring_circle,
    st,
    table_card,
    thumb_frame,
)

PAINTERS: dict = {}

# Рамка миниатюры Veyon (оверлей поверх скриншота, экран прозрачный).
# Геометрия единая для новых тем, описана в theme.json:
# viewBox 0 0 128 128, отверстие экрана x=34 y=34 w=60 h=33.75 (16:9).
PAINTERS["thumb_template"] = lambda m, c: thumb_frame(m, c)
PAINTERS["toolbar_thumbnails"] = lambda m, c: grid4(m, c)
PAINTERS["toolbar_thumbnails_on"] = lambda m, c: (
    grid4(m, c)
    + (f'<circle cx="92" cy="92" r="12" {fi("#22C55E")}/>' + check("line", INK, 5))
)
PAINTERS["toolbar_thumbnail_style"] = lambda m, c: image_shape(m, c)
PAINTERS["toolbar_thumbnail_style_on"] = lambda m, c: (
    image_shape(m, c)
    + (f'<circle cx="92" cy="40" r="9" {fi("#22C55E")}/>' + check("line", INK, 4.5))
)
PAINTERS["toolbar_online_only"] = lambda m, c: (
    eye_shape(m, c, 58, 56, 0.85)
    + (
        f'<circle cx="88" cy="86" r="13" {fi("#FFFFFF" if m == "badge" else "#22C55E")}/>'
        + check("line", "__SYM__" if m == "badge" else INK, 6)
    )
)
PAINTERS["toolbar_online_only_on"] = lambda m, c: (
    eye_shape(m, c, 58, 56, 0.85)
    + (ring_circle("solid", "#22C55E", 13, 88, 86) + check("line", INK, 6))
)
PAINTERS["toolbar_host_info"] = lambda m, c: table_card(m, c)
PAINTERS["toolbar_expand_all"] = lambda m, c: (
    f'<path d="M52 60 L40 48 L52 36" {st(c, 8)}/>'
    f'<path d="M76 60 L88 48 L76 36" {st(c, 8)}/>'
    f'<path d="M52 68 L40 80 L52 92" {st(c, 8)}/>'
    f'<path d="M76 68 L88 80 L76 92" {st(c, 8)}/>'
)
PAINTERS["toolbar_collapse_all"] = lambda m, c: (
    f'<path d="M36 36 L48 48 L36 60" {st(c, 8)}/>'
    f'<path d="M92 36 L80 48 L92 60" {st(c, 8)}/>'
    f'<path d="M36 68 L48 80 L36 92" {st(c, 8)}/>'
    f'<path d="M92 68 L80 80 L92 92" {st(c, 8)}/>'
)
# Таблица результатов: режим подписи хоста (только имя / полный путь).
_MINI_MONITOR = lambda c: (
    f'<rect x="32" y="26" width="64" height="40" rx="8" {st(c, 7)}/>'
    f'<path d="M54 66 V76 M74 66 V76 M48 76 H80" {st(c, 6)}/>'
)
PAINTERS["result_host_name"] = lambda m, c: (
    _MINI_MONITOR(c) + f'<rect x="30" y="92" width="68" height="12" rx="6" {fi(c)}/>'
)
PAINTERS["result_host_path"] = lambda m, c: (
    _MINI_MONITOR(c)
    + f'<rect x="14" y="92" width="20" height="12" rx="6" {fi(c, 0.55)}/>'
    + f'<path d="M42 90 L50 98 L42 106" {st(c, 5)}/>'
    + f'<rect x="56" y="92" width="20" height="12" rx="6" {fi(c, 0.55)}/>'
    + f'<path d="M84 90 L92 98 L84 106" {st(c, 5)}/>'
    + f'<rect x="98" y="92" width="16" height="12" rx="6" {fi(c)}/>'
)
PAINTERS["toolbar_overflow"] = lambda m, c: "".join(
    f'<circle cx="{x}" cy="64" r="8" {fi(c)}/>' for x in (38, 64, 90)
)

COLORS = {
    "thumb_template": "#94A3B8",
    "toolbar_collapse_all": "#64748B",
    "toolbar_expand_all": "#64748B",
    "toolbar_host_info": "#3B82F6",
    "toolbar_online_only": "#22C55E",
    "toolbar_online_only_on": "#22C55E",
    "result_host_name": "#64748B",
    "result_host_path": "#64748B",
    "toolbar_overflow": "#94A3B8",
    "toolbar_thumbnail_style": "#A855F7",
    "toolbar_thumbnail_style_on": "#A855F7",
    "toolbar_thumbnails": "#8B5CF6",
    "toolbar_thumbnails_on": "#8B5CF6",
}
