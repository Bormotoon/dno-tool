"""Хосты и сведения о системе.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    chip_shape,
    fi,
    globe,
    monitor_shape,
    p_unknown,
    person,
    question,
    server_rack,
    st,
    status_dot,
)

PAINTERS: dict = {}

PAINTERS["menu_hosts"] = lambda m, c: server_rack(m, c)
PAINTERS["info_computer"] = lambda m, c: monitor_shape(m, c)
PAINTERS["default"] = p_unknown
PAINTERS["menu_info"] = lambda m, c: status_dot(
    m,
    "#3B82F6",
    f'<rect x="59" y="46" width="10" height="10" rx="5" {fi("@@")}/>'
    f'<rect x="59" y="60" width="10" height="24" rx="5" {fi("@@")}/>',
)
PAINTERS["menu_help"] = lambda m, c: status_dot(m, "#F59E0B", question("@@sym@@", "@@"))

def _hw(m, c, kind):
    if kind == "cpu":
        return chip_shape(m, c)
    if kind == "memory":
        if m == "solid":
            return (
                f'<rect x="30" y="52" width="68" height="24" rx="5" {fi(c)}/>'
                + "".join(
                    f'<rect x="{x}" y="76" width="6" height="10" {fi(c)}/>'
                    for x in range(34, 94, 10)
                )
                + f'<rect x="36" y="58" width="56" height="6" rx="3" {fi(INK)}/>'
            )
        return (
            f'<rect x="30" y="52" width="68" height="24" rx="5" {st(c, 6)}/>'
            + "".join(
                f'<line x1="{x}" y1="76" x2="{x}" y2="86" {st(c, 5)}/>'
                for x in range(36, 94, 10)
            )
        )
    if kind == "board":
        if m == "solid":
            return (
                f'<rect x="34" y="34" width="60" height="60" rx="6" {fi(c)}/>'
                f'<rect x="42" y="42" width="20" height="20" rx="3" {fi(INK)}/>'
                f'<rect x="66" y="42" width="20" height="8" rx="2" {fi(INK)}/>'
                f'<rect x="66" y="54" width="20" height="8" rx="2" {fi(INK)} opacity="0.7"/>'
                + f'<rect x="42" y="66" width="44" height="8" rx="2" {fi(INK)} opacity="0.7"/>'
            )
        return (
            f'<rect x="34" y="34" width="60" height="60" rx="6" {st(c, 6)}/>'
            f'<rect x="42" y="42" width="20" height="20" rx="3" {st(c, 5)}/>'
            f'<line x1="66" y1="46" x2="86" y2="46" {st(c, 5)}/>'
            f'<line x1="66" y1="58" x2="86" y2="58" {st(c, 5)}/>'
            f'<line x1="42" y1="70" x2="86" y2="70" {st(c, 5)}/>'
            f'<line x1="42" y1="82" x2="70" y2="82" {st(c, 5)}/>'
        )
    if kind == "gpu":
        fan = (
            f'<circle cx="60" cy="64" r="18" '
            + (fi(INK) if m == "solid" else st(c, 5))
            + "/>"
            + f'<circle cx="60" cy="64" r="5" {fi(c)}/>'
        )
        if m == "solid":
            return (
                f'<rect x="28" y="46" width="72" height="36" rx="6" {fi(c)}/>'
                + fan
                + f'<rect x="88" y="40" width="8" height="48" rx="3" {fi(c)}/>'
            )
        return (
            f'<rect x="28" y="46" width="72" height="36" rx="6" {st(c, 6)}/>'
            + fan
            + f'<line x1="92" y1="40" x2="92" y2="88" {st(c, 6)}/>'
        )
    if kind == "disk":
        if m == "solid":
            return (
                f'<circle cx="64" cy="64" r="32" {fi(c)}/>'
                f'<circle cx="64" cy="64" r="8" {fi(INK)}/>'
                f'<path d="M64 64 L90 44" stroke="{INK}" stroke-width="7" stroke-linecap="round"/>'
            )
        return (
            f'<circle cx="64" cy="64" r="32" {st(c)}/>'
            f'<circle cx="64" cy="64" r="7" {fi(c)}/>'
            f'<path d="M64 64 L90 44" {st(c, 6)}/>'
        )
    if kind == "space":
        if m == "solid":
            return (
                f'<circle cx="64" cy="64" r="32" {fi(c)}/>'
                f'<path d="M64 64 V32 A32 32 0 0 1 96 64 Z" {fi(INK)}/>'
            )
        return (
            f'<circle cx="64" cy="64" r="32" {st(c)}/>'
            f'<path d="M64 64 V36 A28 28 0 0 1 92 64 Z" {fi(c)}/>'
        )
    if kind == "usb":
        if m == "solid":
            return (
                f'<path d="M64 92 V40 M64 56 L46 40 M64 56 L82 40" {st(c, 10)}/>'
                f'<path d="M56 32 L64 40 L72 32" {st(c, 8)}/>'
                f'<circle cx="46" cy="36" r="8" {fi(c)}/>'
                f'<rect x="76" y="30" width="13" height="13" {fi(c)}/>'
            )
        return (
            f'<path d="M64 92 V40 M64 56 L46 40 M64 56 L82 40" {st(c, 6)}/>'
            f'<path d="M56 32 L64 40 L72 32" {st(c, 6)}/>'
            f'<circle cx="46" cy="36" r="6" {fi(c)}/>'
            f'<rect x="76" y="30" width="12" height="12" {st(c, 5)}/>'
        )
    if kind == "sound":
        spk = "M36 56 H50 L68 40 V88 L50 72 H36 Z"
        waves = "".join(
            f'<path d="M{76 + i * 10} {56 - i * 3} Q{84 + i * 10} 64 {76 + i * 10} {72 + i * 3}" {st(c, 6)}/>'
            for i in range(2)
        )
        if m == "solid":
            return f'<path d="{spk}" {fi(c)}/>' + waves
        return f'<path d="{spk}" {st(c, 6)}/>' + waves
    if kind == "optical":
        if m == "solid":
            return (
                f'<circle cx="64" cy="64" r="32" {fi(c)}/>'
                f'<circle cx="64" cy="64" r="12" {fi(INK)}/>'
            )
        return (
            f'<circle cx="64" cy="64" r="32" {st(c)}/>'
            f'<circle cx="64" cy="64" r="18" {st(c, 5)}/>'
            f'<circle cx="64" cy="64" r="5" {fi(c)}/>'
        )
    if kind == "storage":
        if m == "solid":
            return (
                f'<ellipse cx="64" cy="44" rx="30" ry="12" {fi(c)}/>'
                f'<path d="M34 44 V84 C34 92 94 92 94 84 V44" {fi(c)}/>'
                f'<ellipse cx="64" cy="44" rx="18" ry="6" {fi(INK)}/>'
            )
        return (
            f'<ellipse cx="64" cy="44" rx="30" ry="12" {st(c, 6)}/>'
            f'<path d="M34 44 V84 C34 94 94 94 94 84 V44" {st(c, 6)}/>'
        )
    if kind == "system":
        return chip_shape(m, c, 64, 64, 20, 2)
    if kind == "devices":
        if m == "solid":
            return (
                f'<rect x="34" y="34" width="44" height="32" rx="6" {fi(c)}/>'
                f'<rect x="50" y="62" width="44" height="32" rx="6" {fi(c)} '
                f'stroke="{INK}" stroke-width="4"/>'
            )
        return (
            f'<rect x="34" y="34" width="44" height="32" rx="6" {st(c, 6)}/>'
            f'<rect x="50" y="62" width="44" height="32" rx="6" {st(c, 6)}/>'
        )
    if kind == "network":
        return globe(m, c)
    if kind == "user":
        return person(m, c)
    if kind == "users":
        return person(m, c, 50, 60, 0.85) + person(m, c, 80, 62, 0.7)
    if kind == "listening":
        if m == "solid":
            return (
                f'<path d="M36 88 V64 C36 44 92 44 92 64 V88" {st(c, 10)}/>'
                f'<rect x="30" y="60" width="14" height="28" rx="7" {fi(c)}/>'
                f'<rect x="84" y="60" width="14" height="28" rx="7" {fi(c)}/>'
            )
        return (
            f'<path d="M36 88 V64 C36 44 92 44 92 64 V88" {st(c, 7)}/>'
            f'<rect x="30" y="60" width="14" height="28" rx="7" {st(c, 6)}/>'
            f'<rect x="84" y="60" width="14" height="28" rx="7" {st(c, 6)}/>'
        )
    if kind == "connected":
        plug = (
            f'<rect x="40" y="52" width="26" height="24" rx="6" '
            + (fi(c) if m == "solid" else st(c, 6))
            + "/>"
            + f'<line x1="46" y1="52" x2="46" y2="40" {st(c, 6)}/>'
            + f'<line x1="60" y1="52" x2="60" y2="40" {st(c, 6)}/>'
            + f'<path d="M66 64 H92" {st(c, 7)}/>'
            + f'<circle cx="92" cy="64" r="7" {fi(c)}/>'
        )
        return plug
    return ""

PAINTERS["info_cpu"] = lambda m, c: _hw(m, c, "cpu")
PAINTERS["info_system"] = lambda m, c: _hw(m, c, "system")
PAINTERS["info_memory"] = lambda m, c: _hw(m, c, "memory")
PAINTERS["info_motherboard"] = lambda m, c: _hw(m, c, "board")
PAINTERS["info_gpu"] = lambda m, c: _hw(m, c, "gpu")
PAINTERS["info_disk"] = lambda m, c: _hw(m, c, "disk")
PAINTERS["info_disk_space"] = lambda m, c: _hw(m, c, "space")
PAINTERS["info_usb"] = lambda m, c: _hw(m, c, "usb")
PAINTERS["info_sound"] = lambda m, c: _hw(m, c, "sound")
PAINTERS["info_optical"] = lambda m, c: _hw(m, c, "optical")
PAINTERS["info_storage"] = lambda m, c: _hw(m, c, "storage")
PAINTERS["info_devices"] = lambda m, c: _hw(m, c, "devices")
PAINTERS["info_network"] = lambda m, c: _hw(m, c, "network")
PAINTERS["info_user"] = lambda m, c: _hw(m, c, "user")
PAINTERS["info_users"] = lambda m, c: _hw(m, c, "users")
PAINTERS["info_groups"] = lambda m, c: _hw(m, c, "users")
PAINTERS["info_listening"] = lambda m, c: _hw(m, c, "listening")
PAINTERS["info_connected"] = lambda m, c: _hw(m, c, "connected")

COLORS = {
    "menu_hosts": "#3B82F6",
    "info_computer": "#3B82F6",
    "default": "#94A3B8",
    "menu_info": "#3B82F6",
    "menu_help": "#F59E0B",
    "info_cpu": "#64748B",
    "info_system": "#64748B",
    "info_memory": "#64748B",
    "info_motherboard": "#64748B",
    "info_gpu": "#64748B",
    "info_disk": "#64748B",
    "info_disk_space": "#64748B",
    "info_usb": "#0284C7",
    "info_sound": "#A855F7",
    "info_optical": "#64748B",
    "info_storage": "#64748B",
    "info_devices": "#3B82F6",
    "info_network": "#3B82F6",
    "info_user": "#3B82F6",
    "info_users": "#64748B",
    "info_groups": "#3B82F6",
    "info_listening": "#A855F7",
    "info_connected": "#22C55E",
}
