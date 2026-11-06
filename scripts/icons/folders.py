"""Папки.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    fi,
    folder_shape,
    st,
)

PAINTERS: dict = {}

PAINTERS["folder"] = lambda m, c: folder_shape(m, c)
PAINTERS["menu_add_folder"] = lambda m, c: (
    folder_shape(m, c)
    + (
        f'<circle cx="88" cy="88" r="16" {fi("#22C55E")}/>'
        f'<path d="M88 80 V96 M80 88 H96" {st(INK, 6)}/>'
    )
)

COLORS = {
    "folder": "#D99A1F",
    "menu_add_folder": "#D99A1F",
}
