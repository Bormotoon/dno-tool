"""Предметные SVG-иконки устройств для сканера и карты сети.

Геометрия оборудования находится в hardware.py. Этот модуль сохраняет
общий интерфейс генераторов тем и отделяет его от статусных мониторов UI.
"""

from .hardware import draw_hardware

DOC_OPEN = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
    'viewBox="0 0 128 128">'
)

TOPO_ACCENTS = {
    "topo_network": "#4F9CFF",
    "topo_router": "#FFB74D",
    "topo_host": "#55D98B",
    "topo_local": "#B388FF",
    "topo_unknown": "#8A93A3",
    "topo_switch": "#38BDF8",
    "topo_ap": "#60A5FA",
    "topo_printer": "#94A3B8",
    "topo_camera": "#EF4444",
    "topo_nas": "#F97316",
    "topo_phone": "#22D3EE",
    "topo_pc": "#3B82F6",
    "topo_linux": "#22C55E",
    "topo_vm": "#818CF8",
    "topo_web": "#0EA5E9",
    "topo_netdev": "#64748B",
    "topo_projector": "#38BDF8",
    "topo_tv": "#3B82F6",
    "topo_media_player": "#818CF8",
    "topo_network_audio": "#22D3EE",
    "topo_smartphone": "#60A5FA",
    "topo_tablet": "#60A5FA",
    "topo_mac": "#94A3B8",
    "topo_server": "#F97316",
    "topo_thin_client": "#55D98B",
    "topo_ups": "#22C55E",
    "topo_pdu": "#64748B",
    "topo_access_control": "#22D3EE",
    "topo_intercom": "#38BDF8",
    "topo_plc": "#55D98B",
    "topo_smart_home": "#60A5FA",
    "topo_3d_printer": "#22D3EE",
    "topo_interactive_panel": "#38BDF8",
}
TOPO_NAMES = sorted(TOPO_ACCENTS)
TOPO_NO_BACKING = set(TOPO_NAMES)


def topo_inner(name: str, mode: str, accent: str = "", ink: str = "#FFFFFF") -> str:
    """Нарисовать оборудование без внешней SVG-обёртки."""
    if name not in TOPO_ACCENTS:
        raise KeyError(name)
    return draw_hardware(name.removeprefix("topo_"), mode)


def topo_svg(name: str, variant: str) -> str:
    """Полный SVG для одного из вариантов темы."""
    return DOC_OPEN + topo_inner(name, variant) + "</svg>"


def topo_painter(name: str):
    """Адаптер для общего генератора: (режим, цвет) -> содержимое SVG."""
    def paint(mode, color):
        return topo_inner(name, mode)
    return paint


PAINTERS = {name: topo_painter(name) for name in TOPO_NAMES}
COLORS = dict(TOPO_ACCENTS)
