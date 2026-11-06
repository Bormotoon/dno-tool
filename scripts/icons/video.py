"""Видеоредактор.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    arrow_lr,
    bent_arrow,
    clear_list_glyph,
    doc_shape,
    fi,
    mark_bracket,
    range_brackets,
    scissors,
    speaker_body,
    st,
    text_add,
    transport_glyph,
    trash,
    video_editing_table,
)

PAINTERS: dict = {}

PAINTERS["menu_video_editor"] = lambda m, c: video_editing_table(m, c)


# --- video editor transport / editing (ve_*): то же ядро, что у
# toolbar/menu (без ve_frame-масштаба x1.5) - однородность с тулбарами ---
def media_transfer(mode: str, color: str, exporting: bool = False) -> str:
    """Медиаклип со стрелкой внутрь или наружу; геометрия различима без цвета."""
    frame = "M94 46 V32 H34 V96 H94 V82" if exporting else "M34 46 V32 H94 V96 H34 V82"
    weight = 9 if mode in ("solid", "badge") else 6
    body = f'<path d="{frame}" {st(color, weight)}/>'
    for x in (46, 72):
        for y in (40, 81):
            body += f'<rect x="{x}" y="{y}" width="10" height="7" rx="1" {fi(color)}/>'
    arrow = arrow_lr(
        "solid" if mode in ("solid", "badge") else "line",
        color,
        "right",
        y=64,
        x1=66 if exporting else 16,
        x2=112 if exporting else 62,
    )
    glyph = body + arrow
    return (
        f'<g transform="translate(11.52 11.52) scale(0.82)">{glyph}</g>'
        if mode == "badge"
        else glyph
    )


PAINTERS["ve_import"] = lambda m, c: media_transfer(m, c)
PAINTERS["ve_export"] = lambda m, c: media_transfer(m, c, True)


def add_to_timeline(mode: str, color: str) -> str:
    """Фрагмент со стрелкой вниз к дорожке, отдельно от импорта файла."""
    weight = 9 if mode in ("solid", "badge") else 6
    return (
        f'<rect x="34" y="24" width="60" height="28" rx="4" {st(color, weight)}/>'
        f'<path d="M64 58 V86 M50 74 L64 88 L78 74" {st(color, weight)}/>'
        f'<path d="M24 88 V104 H104 V88" {st(color, weight)}/>'
    )


PAINTERS["ve_add_timeline"] = add_to_timeline
PAINTERS["ve_fit_project"] = lambda m, c: (
    f'<path d="M26 40 V88 M102 40 V88 M28 64 H100 M42 50 L28 64 L42 78 M88 50 L102 64 L88 78" {st(c, 7)}/>'
)
PAINTERS["ve_fit_selection"] = lambda m, c: (
    f'<rect x="28" y="30" width="54" height="44" rx="5" {st(c, 7)}/>'
    f'<path d="M20 20 V84 M90 20 V72" {st(c, 5)}/>'
    f'<circle cx="82" cy="84" r="17" {st(c, 7)}/>'
    f'<path d="M94 96 L108 110" {st(c, 8)}/>'
)


def monitor_layout(mode: str, color: str, selected: str) -> str:
    """Два монитора с выделенной левой, правой или обеими областями."""
    body = ""
    for x, key in ((18, "source"), (70, "program")):
        body += f'<rect x="{x}" y="32" width="40" height="52" rx="4" {st(color, 6)}/>'
        if selected in ("dual", key):
            body += (
                f'<rect x="{x + 9}" y="41" width="22" height="34" rx="2" {fi(color)}/>'
            )
        body += f'<path d="M{x + 20} 84 V96 M{x + 8} 96 H{x + 32}" {st(color, 6)}/>'
    return body


for _monitor in ("dual", "source", "program"):
    PAINTERS[f"ve_view_{_monitor}"] = lambda m, c, key=_monitor: monitor_layout(
        m, c, key
    )
    PAINTERS[f"ve_view_{_monitor}_on"] = PAINTERS[f"ve_view_{_monitor}"]
PAINTERS["ve_undo"] = lambda m, c: bent_arrow(m, c, "undo")
PAINTERS["ve_redo"] = lambda m, c: bent_arrow(m, c, "redo")
PAINTERS["ve_play"] = lambda m, c: transport_glyph(m, c, "play")
PAINTERS["ve_pause"] = lambda m, c: transport_glyph(m, c, "pause")
PAINTERS["ve_stop"] = lambda m, c: transport_glyph(m, c, "stop")
PAINTERS["ve_prev"] = lambda m, c: transport_glyph(m, c, "prev")
PAINTERS["ve_next"] = lambda m, c: transport_glyph(m, c, "next")
PAINTERS["ve_to_start"] = lambda m, c: transport_glyph(m, c, "to_start")
PAINTERS["ve_mark_in"] = lambda m, c: mark_bracket(m, c, "in")
PAINTERS["ve_mark_out"] = lambda m, c: mark_bracket(m, c, "out")
PAINTERS["ve_split"] = lambda m, c: scissors(m, c)
PAINTERS["ve_delete"] = lambda m, c: trash(m, c)
PAINTERS["ve_cut_range"] = lambda m, c: range_brackets(m, c, "cut")
PAINTERS["ve_keep_range"] = lambda m, c: range_brackets(m, c, "keep")


def selection_cursor(mode: str, color: str) -> str:
    """Стрелка инструмента выделения, различимая в размере кнопки."""
    return f'<path d="M34 22 V94 L53 76 L68 108 L82 101 L67 70 H94 Z" {st(color, 6) if mode == "line" else fi(color)}/>'


def razor_blade(mode: str, color: str) -> str:
    """Бритвенное лезвие с центральной прорезью, отдельно от общего разреза."""
    weight = 8 if mode in ("solid", "badge") else 6
    return (
        '<g transform="rotate(-35 64 64)">'
        f'<path d="M27 39 H43 V31 H85 V39 H101 V89 H85 V97 H43 V89 H27 Z" {st(color, weight)}/>'
        f'<path d="M44 64 H84 M54 57 V71 M74 57 V71" {st(color, weight)}/></g>'
    )


def ripple_delete(mode: str, color: str) -> str:
    """Два клипа сходятся к удаляемому промежутку на общей оси времени."""
    weight = 9 if mode in ("solid", "badge") else 6
    return (
        f'<rect x="19" y="28" width="25" height="32" rx="3" {st(color, weight)}/>'
        f'<rect x="84" y="28" width="25" height="32" rx="3" {st(color, weight)}/>'
        f'<path d="M56 32 L72 54 M72 32 L56 54" {st(color, 6)}/>'
        f'<path d="M20 88 H52 M40 76 L52 88 L40 100 M108 88 H76 M88 76 L76 88 L88 100" {st(color, weight)}/>'
    )


def snapping_magnet(mode: str, color: str) -> str:
    """Подковообразный магнит с обозначенными полюсами привязки."""
    weight = 10 if mode in ("solid", "badge") else 7
    return (
        f'<path d="M32 29 V72 A32 32 0 0 0 96 72 V29 H76 V72 A12 12 0 0 1 52 72 V29 Z" {st(color, weight)}/>'
        f'<path d="M32 48 H52 M76 48 H96" {st(color, weight)}/>'
    )


for _name, _painter in (
    ("ve_select", selection_cursor),
    ("ve_blade", razor_blade),
    ("ve_snap", snapping_magnet),
):
    PAINTERS[_name] = _painter
    PAINTERS[_name + "_on"] = _painter
PAINTERS["ve_ripple_delete"] = ripple_delete
PAINTERS["ve_remove_file"] = lambda m, c: (
    doc_shape(m, c, 40, 26, 40, 68, 15)
    + f'<rect x="52" y="60" width="26" height="9" rx="4.5" {fi("#EF4444")}/>'
)
PAINTERS["ve_clear_list"] = lambda m, c: clear_list_glyph(m, c)
PAINTERS["ve_volume"] = lambda m, c: (
    speaker_body(m, c)
    + f'<path d="M76 56 Q84 64 76 72" {st(c, 6)}/>'
    + f'<path d="M82 48 Q96 64 82 80" {st(c, 6)}/>'
)
PAINTERS["ve_mute"] = lambda m, c: (
    speaker_body(m, c) + f'<path d="M76 54 L98 76 M98 54 L76 76" {st("#EF4444", 8)}/>'
)
PAINTERS["ve_add_text"] = lambda m, c: text_add(m, c)


def add_overlay(mode: str, color: str) -> str:
    """Слои изображения с плюсом: добавление текста, картинки или фигуры."""
    weight = 8 if mode in ("solid", "badge") else 6
    return (
        f'<path d="M20 43 V96 H76" {st(color, weight)}/>'
        f'<rect x="34" y="24" width="64" height="58" rx="5" {st(color, weight)}/>'
        f'<circle cx="51" cy="42" r="5" {fi(color)}/>'
        f'<path d="M43 69 L59 53 L72 65 L83 52 L90 60" {st(color, 5)}/>'
        f'<path d="M98 82 V110 M84 96 H112" {st("#22C55E", weight)}/>'
    )


PAINTERS["ve_add_overlay"] = add_overlay

# --- video editor transport / editing ---

COLORS = {
    "ve_select": "#3B82F6",
    "ve_select_on": "#10B981",
    "ve_blade": "#F59E0B",
    "ve_blade_on": "#10B981",
    "ve_snap": "#64748B",
    "ve_snap_on": "#10B981",
    "ve_ripple_delete": "#EF4444",
    "ve_add_timeline": "#22C55E",
    "ve_fit_project": "#64748B",
    "ve_fit_selection": "#64748B",
    "ve_view_dual": "#64748B",
    "ve_view_source": "#64748B",
    "ve_view_program": "#64748B",
    "ve_view_dual_on": "#10B981",
    "ve_view_source_on": "#10B981",
    "ve_view_program_on": "#10B981",
    "menu_video_editor": "#A855F7",
    "ve_add_text": "#3B82F6",
    "ve_add_overlay": "#3B82F6",
    "ve_clear_list": "#64748B",
    "ve_cut_range": "#EF4444",
    "ve_delete": "#EF4444",
    "ve_export": "#3B82F6",
    "ve_import": "#22C55E",
    "ve_keep_range": "#22C55E",
    "ve_mark_in": "#06B6D4",
    "ve_mark_out": "#06B6D4",
    "ve_mute": "#EF4444",
    "ve_next": "#64748B",
    "ve_pause": "#F59E0B",
    "ve_stop": "#EF4444",
    "ve_play": "#22C55E",
    "ve_prev": "#64748B",
    "ve_redo": "#64748B",
    "ve_remove_file": "#F59E0B",
    "ve_split": "#F59E0B",
    "ve_to_start": "#64748B",
    "ve_undo": "#64748B",
    "ve_volume": "#3B82F6",
}
