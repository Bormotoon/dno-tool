"""Щиты, ключи, доступ.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    check,
    fi,
    key_shape,
    shield_shape,
    st,
)

PAINTERS: dict = {}

PAINTERS["shield"] = lambda m, c: shield_shape(m, c, "check")
PAINTERS["menu_polkit"] = lambda m, c: shield_shape(m, c, "key")
PAINTERS["menu_credentials"] = lambda m, c: (
    key_shape(m, c)
    + (
        f'<circle cx="40" cy="92" r="5" {fi(c)}/>'
        f'<circle cx="56" cy="92" r="5" {fi(c)}/>'
        f'<circle cx="72" cy="92" r="5" {fi(c)}/>'
    )
)
PAINTERS["key_exists"] = lambda m, c: (
    key_shape(m, c)
    + (f'<circle cx="90" cy="88" r="14" {fi("#22C55E")}/>' + check("line", INK, 6))
)
PAINTERS["key_missing"] = lambda m, c: (
    key_shape(m, c) + (f'<path d="M76 74 L102 100" {st("#EF4444", 8)}/>')
)

COLORS = {
    "shield": "#6366F1",
    "menu_polkit": "#6366F1",
    "menu_credentials": "#CA8A04",
    "key_exists": "#CA8A04",
    "key_missing": "#CA8A04",
}
