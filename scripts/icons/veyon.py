"""Veyon, камеры, глазки.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    INK,
    arrow_lr,
    arrow_ud,
    bubble,
    circular_arrows,
    door,
    eye_shape,
    fi,
    film,
    globe,
    keyboard,
    lock_mini,
    lock_shape,
    monitor_shape,
    plane,
    power_symbol,
    pulse,
    ring_circle,
    st,
)
from .hardware import draw_hardware

PAINTERS: dict = {}

PAINTERS["menu_veyon"] = lambda m, c: eye_shape(m, c)
PAINTERS["menu_veyon_view"] = lambda m, c: eye_shape(m, c)
PAINTERS["veyon_down"] = lambda m, c: (
    monitor_shape(m, c)
    + arrow_ud(
        "solid" if m == "solid" else "line",
        "#EF4444" if m != "badge" else INK,
        "down",
        x=64,
        y1=44,
        y2=76,
    )
)
PAINTERS["menu_veyon_control"] = lambda m, c: (
    eye_shape(m, c, 64, 52, 0.8)
    + (
        f'<line x1="36" y1="88" x2="92" y2="88" {st(c, 6)}/>'
        f'<circle cx="64" cy="88" r="9" {fi(c)}/>'
        if m != "solid"
        else f'<rect x="36" y="82" width="56" height="10" rx="5" {fi(c)}/>'
        f'<circle cx="64" cy="87" r="9" {fi(c)} stroke="{INK}" stroke-width="4"/>'
    )
)
PAINTERS["veyon_lock"] = lambda m, c: lock_shape(m, c, True)
PAINTERS["veyon_unlock"] = lambda m, c: lock_shape(m, c, False)
PAINTERS["veyon_powerdown"] = lambda m, c: (
    ring_circle(m, c)
    + power_symbol("line" if m != "solid" else "solid", INK if m == "solid" else c)
)
PAINTERS["veyon_power_on"] = lambda m, c: (
    ring_circle(m, "#22C55E")
    + power_symbol(
        "line" if m != "solid" else "solid", INK if m == "solid" else "#22C55E"
    )
)
PAINTERS["veyon_reboot"] = lambda m, c: circular_arrows(m, c, True)
PAINTERS["veyon_logoff"] = lambda m, c: door(m, c, enter=False)
PAINTERS["veyon_login"] = lambda m, c: door(m, c, enter=True)
PAINTERS["veyon_message"] = lambda m, c: bubble(m, c)
PAINTERS["veyon_website"] = lambda m, c: globe(m, c)
PAINTERS["veyon_start_app"] = lambda m, c: plane(m, c)
PAINTERS["veyon_lock_input"] = lambda m, c: keyboard(m, c) + lock_mini(m, c, True)
PAINTERS["veyon_unlock_input"] = lambda m, c: keyboard(m, c) + lock_mini(m, c, False)
PAINTERS["menu_vnc"] = lambda m, c: (
    monitor_shape(m, c)
    + (
        arrow_lr(
            "solid" if m == "solid" else "line",
            "#22C55E" if m != "badge" else INK,
            "right",
            y=88,
            x1=44,
            x2=84,
        )
    )
)
# menu_krfb: монитор со стрелкой (обе стороны) - KRemote Frame Buffer.
PAINTERS["menu_krfb"] = lambda m, c: (
    monitor_shape(m, c)
    + (
        arrow_lr(
            "solid" if m == "solid" else "line",
            "#F59E0B" if m != "badge" else INK,
            "left",
            y=88,
            x1=44,
            x2=84,
        )
    )
)
PAINTERS["menu_diagnostics"] = lambda m, c: pulse(m, c)
PAINTERS["menu_kde"] = lambda m, c: (
    f'<path d="M48 36 V92 M80 38 L50 62 L82 90" '
    f"{st(c, 11) if m == 'solid' else st(c, 8)}/>"
)
PAINTERS["menu_recording"] = lambda m, c: film(m, c)
PAINTERS["camera"] = lambda m, c: draw_hardware("camera", m)
PAINTERS["menu_camera_view"] = lambda m, c: eye_shape(m, c)
PAINTERS["menu_camera_record"] = lambda m, c: (
    ring_circle(m, "#EF4444", 30)
    + (
        f'<circle cx="64" cy="64" r="17" {fi("#FFFFFF")}/>'
        f'<circle cx="64" cy="64" r="9" {fi("#EF4444")}/>'
        if m == "solid"
        else f'<circle cx="64" cy="64" r="12" {fi("#EF4444")}/>'
    )
    if m != "badge"
    else f'<circle cx="64" cy="64" r="24" {fi("#FFFFFF")}/>'
    f'<circle cx="64" cy="64" r="12" {fi("#EF4444")}/>'
)
PAINTERS["menu_camera_stop"] = lambda m, c: (
    ring_circle(m, c, 30)
    + f'<rect x="54" y="54" width="20" height="20" rx="4" {fi(c if m != "solid" else INK)}/>'
)

COLORS = {
    "camera": "#EF4444",
    "menu_camera_record": "#EF4444",
    "menu_camera_stop": "#64748B",
    "menu_camera_view": "#8B5CF6",
    "menu_diagnostics": "#F59E0B",
    "menu_kde": "#3B82F6",
    "menu_krfb": "#F59E0B",
    "menu_recording": "#EF4444",
    "menu_veyon": "#8B5CF6",
    "menu_veyon_control": "#8B5CF6",
    "menu_veyon_view": "#8B5CF6",
    "menu_vnc": "#0EA5E9",
    "veyon_down": "#EF4444",
    "veyon_lock": "#8B5CF6",
    "veyon_lock_input": "#8B5CF6",
    "veyon_login": "#22C55E",
    "veyon_logoff": "#F59E0B",
    "veyon_message": "#3B82F6",
    "veyon_power_on": "#22C55E",
    "veyon_powerdown": "#EF4444",
    "veyon_reboot": "#F59E0B",
    "veyon_start_app": "#22C55E",
    "veyon_unlock": "#8B5CF6",
    "veyon_unlock_input": "#8B5CF6",
    "veyon_website": "#06B6D4",
}
