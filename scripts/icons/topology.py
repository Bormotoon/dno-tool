"""Карта сети (пункт меню).

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    topology_graph,
)

PAINTERS: dict = {}

PAINTERS["menu_topology"] = lambda m, c: topology_graph(m, c)

COLORS = {
    "menu_topology": "#06B6D4",
}
