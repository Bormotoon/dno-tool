"""Статусы устройств и нод.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    broadcast,
    check,
    fi,
    monitor_shape,
    st,
)

PAINTERS: dict = {}


def _dev(m, c, mode="ok"):
    # Статус несёт сам монитор (цвет экрана/корпуса), без кружка-бейджа:
    # съезжал на подставку и сливался в мелком размере.
    if mode == "ok":
        return monitor_shape(
            m,
            "#22C55E" if m != "badge" else c,
            "#22C55E" if m != "badge" else INK,
        )
    if mode == "bad":
        s = monitor_shape(
            m,
            "#64748B" if m != "badge" else c,
            "#64748B" if m != "badge" else INK,
        )
        return s + (
            f'<path d="M40 92 L94 40" {st("#EF4444" if m != "badge" else INK, 9)}/>'
        )
    return monitor_shape(  # busy: янтарный монитор без часов
        m,
        "#F59E0B" if m != "badge" else c,
        "#F59E0B" if m != "badge" else INK,
    )


PAINTERS["online"] = lambda m, c: _dev(m, c, "ok")
PAINTERS["offline"] = lambda m, c: _dev(m, c, "bad")
PAINTERS["busy"] = lambda m, c: _dev(m, c, "busy")
PAINTERS["node_online"] = lambda m, c: (
    f'<circle cx="56" cy="64" r="18" '
    + (fi("#22C55E") if m != "badge" else fi("#FFFFFF"))
    + "/>"
    + (broadcast(m, "#06B6D4", 56, 64) if False else "")
    + (f'<circle cx="88" cy="88" r="13" {fi("#22C55E")}/>' + check("line", INK, 6))
)
PAINTERS["node_offline"] = lambda m, c: (
    f'<circle cx="56" cy="64" r="18" '
    + (fi("#64748B") if m != "badge" else fi("#FFFFFF"))
    + "/>"
    + f'<path d="M40 92 L94 40" {st("#EF4444" if m != "badge" else INK, 9)}/>'
)

COLORS = {
    "online": "#22C55E",
    "offline": "#64748B",
    "busy": "#F59E0B",
    "node_online": "#06B6D4",
    "node_offline": "#64748B",
}
