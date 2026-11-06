"""Файлы и операции списка.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    arrow_lr,
    arrow_ud,
    box_shape,
    check,
    chev,
    circular_arrows,
    cross,
    doc_shape,
    door,
    eye_off,
    fi,
    floppy,
    pencil,
    st,
    table_shape,
    trash,
    tray,
)

PAINTERS: dict = {}

PAINTERS["menu_file"] = lambda m, c: (
    doc_shape(m, c)
    + (
        f'<line x1="52" y1="62" x2="78" y2="62" {st(c, 5)}/>'
        f'<line x1="52" y1="73" x2="78" y2="73" {st(c, 5)}/>'
        f'<line x1="52" y1="84" x2="70" y2="84" {st(c, 5)}/>'
        if m != "solid"
        else f'<rect x="50" y="56" width="28" height="6" rx="3" {fi(INK)}/>'
        f'<rect x="50" y="67" width="28" height="6" rx="3" {fi(INK)} opacity="0.7"/>'
        f'<rect x="50" y="78" width="20" height="6" rx="3" {fi(INK)} opacity="0.5"/>'
    )
)
PAINTERS["menu_new_file"] = lambda m, c: (
    doc_shape(m, c, 40, 26, 40, 68, 15)
    + (
        f'<circle cx="88" cy="88" r="15" {fi("#22C55E")}/>'
        f'<path d="M88 81 V95 M81 88 H95" {st(INK, 6)}/>'
    )
)
PAINTERS["menu_save"] = lambda m, c: floppy(m, c)
PAINTERS["menu_import"] = lambda m, c: (
    tray(m, c)
    + arrow_ud("solid" if m == "solid" else "line", c, "down", x=64, y1=34, y2=66)
)
PAINTERS["menu_export"] = lambda m, c: (
    tray(m, c)
    + arrow_ud("solid" if m == "solid" else "line", c, "up", x=64, y1=34, y2=66)
)
PAINTERS["menu_download"] = PAINTERS["menu_import"]
PAINTERS["menu_upload"] = PAINTERS["menu_export"]
PAINTERS["menu_archive"] = lambda m, c: box_shape(m, c)
PAINTERS["menu_extract"] = lambda m, c: box_shape(m, c, "up")
PAINTERS["menu_copy_name"] = lambda m, c: (
    doc_shape(m, c, 34, 30, 38, 64, 14)
    + doc_shape(m, "#94A3B8" if m == "line" else c, 56, 40, 38, 64, 14)
)
PAINTERS["menu_copy_path"] = lambda m, c: (
    doc_shape(m, c, 34, 30, 38, 64, 14)
    + (
        arrow_lr(
            "solid" if m == "solid" else "line", c, "right", y=84, x1=56, x2=96
        )
    )
)
PAINTERS["menu_edit_path"] = lambda m, c: pencil(m, c)
PAINTERS["menu_edit_file"] = lambda m, c: (
    f'<rect x="30" y="28" width="52" height="72" rx="6" '
    + (fi(c) if m == "solid" else st(c, 6))
    + "/>"
    + f'<path d="M42 30 V98 M26 42 H36 M26 58 H36 M26 74 H36 M26 90 H36" '
    f'{st(INK if m == "solid" else c, 5)}/>'
    + f'<path d="M52 44 H70 M52 56 H70 M52 68 H62" '
    f'{st(INK if m == "solid" else c, 4)}/>'
    + '<path d="M64 86 L94 56 Q98 52 102 56 L106 60 Q110 64 106 68 '
    'L76 98 L62 102 Z" fill="#F59E0B" stroke="#92400E" stroke-width="3" '
    'stroke-linejoin="round"/>'
    + '<path d="M64 86 L76 98 L62 102 Z" fill="#FEF3C7"/>'
    + '<path d="M64 96 L68 100 L62 102 Z" fill="#334155"/>'
    + '<path d="M94 56 L106 68" stroke="#FFFBEB" stroke-width="4"/>'
)
PAINTERS["menu_rename"] = lambda m, c: pencil(m, c)
PAINTERS["menu_delete"] = lambda m, c: trash(m, c)
PAINTERS["menu_refresh"] = lambda m, c: circular_arrows(m, c, double=False)
PAINTERS["menu_sync"] = lambda m, c: circular_arrows(m, c, double=True)
PAINTERS["menu_abort"] = lambda m, c: (
    (
        f'<path d="M64 32 L92 40 L100 68 L100 68 L92 96 L64 104 L36 96 L28 68 L36 40 Z" '
        f"{fi(c)}/>" + cross("solid", INK)
    )
    if m == "solid"
    else (
        f'<path d="M64 32 L92 40 L100 68 L92 96 L64 104 L36 96 L28 68 L36 40 Z" '
        f"{st(c)}/>" + cross("line", c)
    )
)
PAINTERS["menu_check_all"] = lambda m, c: "".join(
    (
        f'<rect x="{x}" y="{y}" width="24" height="24" rx="6" {fi(c)}/>'
        f'<path d="M{x + 6} {y + 12} L{x + 11} {y + 17} L{x + 19} {y + 8}" {st(INK, 5)}/>'
        if m == "solid"
        else f'<rect x="{x}" y="{y}" width="24" height="24" rx="6" '
        f"{fi('#FFFFFF') if m == 'badge' else st(c, 5)}/>"
        f'<path d="M{x + 6} {y + 12} L{x + 11} {y + 17} L{x + 19} {y + 8}" '
        f"{st('__SYM__' if m == 'badge' else c, 5)}/>"
    )
    for x, y in ((36, 36), (68, 36), (36, 68), (68, 68))
)
PAINTERS["menu_uncheck_all"] = lambda m, c: "".join(
    f'<rect x="{x}" y="{y}" width="24" height="24" rx="6" '
    + (fi(c) if m == "solid" else st(c, 5))
    + "/>"
    for x, y in ((36, 36), (68, 36), (36, 68), (68, 68))
)
PAINTERS["menu_hide"] = lambda m, c: eye_off(m, c)
PAINTERS["menu_sort"] = lambda m, c: (
    f'<rect x="34" y="38" width="34" height="9" rx="4.5" {fi(c)}/>'
    f'<rect x="34" y="55" width="26" height="9" rx="4.5" {fi(c)}/>'
    f'<rect x="34" y="72" width="18" height="9" rx="4.5" {fi(c)}/>'
    + arrow_ud("solid" if m == "solid" else "line", c, "down", x=86, y1=40, y2=90)
)
PAINTERS["menu_exit"] = lambda m, c: door(m, c, enter=False)
PAINTERS["menu_up"] = lambda m, c: chev(m, c, "up", single=True)
PAINTERS["menu_down"] = lambda m, c: chev(m, c, "down", single=True)
PAINTERS["menu_back"] = lambda m, c: chev(m, c, "left", single=True)
PAINTERS["menu_forward"] = lambda m, c: chev(m, c, "right", single=True)
PAINTERS["menu_home"] = lambda m, c: (
    f'<path d="M32 64 L64 36 L96 64 V94 H70 V74 H58 V94 H32 Z" {fi(c)}/>'
    if m == "solid"
    else f'<path d="M32 64 L64 36 L96 64 V94 H70 V74 H58 V94 H32 Z" {st(c, 6)}/>'
)

def _list_toolbar_rows(m, c):
    return "".join(
        f'<circle cx="40" cy="{y}" r="4.5" {fi(c)}/>'
        f'<rect x="52" y="{y - 4}" width="42" height="8" rx="4" {fi(c)} opacity="0.65"/>'
        for y in (44, 64, 84)
    )

PAINTERS["menu_list_toolbar"] = lambda m, c: _list_toolbar_rows(m, c)
PAINTERS["menu_add_panel"] = lambda m, c: (
    _list_toolbar_rows(m, c)
    + f'<circle cx="88" cy="88" r="16" {fi("#22C55E")}/>'
    + f'<path d="M88 80 V96 M80 88 H96" {st(INK, 6)}/>'
)
PAINTERS["menu_results"] = lambda m, c: table_shape(m, c)
PAINTERS["menu_results_on"] = lambda m, c: (
    table_shape(m, c, "#22C55E")
    + (
        f'<circle cx="92" cy="40" r="9" {fi("#FFFFFF" if m == "badge" else "#22C55E")}/>'
        + check("line", "__SYM__" if m == "badge" else INK, 4.5)
    )
)

COLORS = {
    "menu_abort": "#EF4444",
    "menu_add_panel": "#64748B",
    "menu_archive": "#A855F7",
    "menu_back": "#64748B",
    "menu_check_all": "#22C55E",
    "menu_copy_name": "#64748B",
    "menu_copy_path": "#06B6D4",
    "menu_delete": "#EF4444",
    "menu_down": "#64748B",
    "menu_download": "#22C55E",
    "menu_edit_path": "#F59E0B",
    "menu_edit_file": "#3B82F6",
    "menu_exit": "#EF4444",
    "menu_export": "#3B82F6",
    "menu_extract": "#A855F7",
    "menu_file": "#64748B",
    "menu_forward": "#64748B",
    "menu_hide": "#64748B",
    "menu_home": "#3B82F6",
    "menu_import": "#22C55E",
    "menu_list_toolbar": "#64748B",
    "menu_new_file": "#64748B",
    "menu_refresh": "#22C55E",
    "menu_rename": "#F59E0B",
    "menu_results": "#3B82F6",
    "menu_results_on": "#22C55E",
    "menu_save": "#3B82F6",
    "menu_sort": "#64748B",
    "menu_sync": "#3B82F6",
    "menu_uncheck_all": "#94A3B8",
    "menu_up": "#64748B",
    "menu_upload": "#3B82F6",
}
