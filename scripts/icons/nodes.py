"""Ноды федераций.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    fi,
    link_nodes,
)

PAINTERS: dict = {}

PAINTERS["menu_nodes"] = lambda m, c: link_nodes(m, c)
PAINTERS["toolbar_nodes"] = lambda m, c: link_nodes(m, c)
PAINTERS["toolbar_nodes_on"] = lambda m, c: (
    link_nodes(m, c) + f'<circle cx="64" cy="64" r="6" {fi("#22C55E")}/>'
)

COLORS = {
    "menu_nodes": "#06B6D4",
    "toolbar_nodes": "#06B6D4",
    "toolbar_nodes_on": "#06B6D4",
}
