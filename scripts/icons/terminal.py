"""Терминал и команды.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    fi,
    prompt_mark,
    st,
)

PAINTERS: dict = {}

def _termwin(m, c):
    if m == "solid":
        return (
            f'<rect x="30" y="38" width="68" height="48" rx="10" {fi("#2B3245")}/>'
            + prompt_mark("solid", "#22C55E", 62, 58, 1.0)
            + f'<circle cx="42" cy="48" r="3.5" {fi("#EF4444")}/>'
            f'<circle cx="53" cy="48" r="3.5" {fi("#F59E0B")}/>'
        )
    ink = c if m == "badge" else "#2B3245"
    dots = (
        f'<circle cx="42" cy="48" r="3.5" {fi("#EF4444")}/>'
        f'<circle cx="53" cy="48" r="3.5" {fi("#F59E0B")}/>'
        f'<circle cx="64" cy="48" r="3.5" {fi("#22C55E")}/>'
    )
    return (
        f'<rect x="30" y="38" width="68" height="48" rx="10" {st(ink)}/>'
        f'<line x1="30" y1="58" x2="98" y2="58" {st(ink, 4)}/>'
        + dots
        + prompt_mark("line", "#22C55E" if m != "badge" else ink, 62, 72, 0.85)
    )

PAINTERS["menu_terminal"] = _termwin
PAINTERS["command"] = lambda m, c: (
    (
        f'<rect x="34" y="44" width="60" height="40" rx="10" {fi(c)}/>'
        + prompt_mark("solid", INK, 62, 62, 0.9)
    )
    if m == "solid"
    else (
        f'<rect x="34" y="44" width="60" height="40" rx="10" '
        f"{st(c) if m == 'line' else fi('#FFFFFF')}/>"
        + prompt_mark("line", c if m == "line" else "__SYM__", 62, 62, 0.9)
    )
)
PAINTERS["menu_commands"] = PAINTERS["command"]
PAINTERS["menu_add_command"] = lambda m, c: (
    PAINTERS["command"](m, c) + f'<circle cx="90" cy="86" r="15" {fi("#22C55E")}/>'
    f'<path d="M90 79 V93 M83 86 H97" {st(INK, 6)}/>'
)

COLORS = {
    "menu_terminal": "#22C55E",
    "command": "#22C55E",
    "menu_commands": "#22C55E",
    "menu_add_command": "#22C55E",
}
