"""Логотип приложения.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    p_logo,
)

PAINTERS: dict = {}

PAINTERS["icon"] = lambda m, c: p_logo(m, c, False)
PAINTERS["icon_on"] = lambda m, c: p_logo(m, c, True)

COLORS = {
    "icon": "#2B3245",
    "icon_on": "#2B3245",
}
