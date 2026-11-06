"""Сканер, пинг, анализ.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    broadcast,
    fi,
    lens,
    monitor_shape,
    radar,
    st,
)

PAINTERS: dict = {}

PAINTERS["menu_scanner"] = lambda m, c: radar(m, c)
PAINTERS["scan_ping"] = lambda m, c: (
    radar(m, "#22C55E") + (f'<circle cx="86" cy="42" r="6" {fi("#22C55E")}/>')
)
PAINTERS["scan_analyze"] = lambda m, c: (
    f'<rect x="34" y="62" width="10" height="26" rx="3" {fi(c)}/>'
    f'<rect x="48" y="50" width="10" height="38" rx="3" {fi(c)}/>'
    f'<rect x="62" y="70" width="10" height="18" rx="3" {fi(c)} opacity="0.55"/>'
    + lens(m, c, 78, 48, 20)
)
PAINTERS["menu_ping"] = lambda m, c: broadcast(m, c)
PAINTERS["menu_add_ip_range"] = lambda m, c: (
    lens(m, c, 56, 56, 24) + f'<circle cx="90" cy="88" r="15" {fi("#22C55E")}/>'
    f'<path d="M90 81 V95 M83 88 H97" {st(INK, 6)}/>'
)
PAINTERS["menu_add_device"] = lambda m, c: (
    monitor_shape(m, c) + f'<circle cx="88" cy="88" r="15" {fi("#22C55E")}/>'
    f'<path d="M88 81 V95 M83 88 H97" {st(INK, 6)}/>'
)
PAINTERS["menu_import_classroom"] = lambda m, c: (
    monitor_shape(m, c) + f'<circle cx="88" cy="88" r="15" {fi("#0EA5E9")}/>'
    f'<path d="M88 80 V94 M82 89 L88 95 L94 89" {st(INK, 6)}/>'
)

COLORS = {
    "menu_scanner": "#16A34A",
    "scan_ping": "#16A34A",
    "scan_analyze": "#16A34A",
    "menu_ping": "#22C55E",
    "menu_add_ip_range": "#06B6D4",
    "menu_add_device": "#3B82F6",
    "menu_import_classroom": "#0EA5E9",
}
