"""Раскладка мониторов хоста для окна VNC, если сервер её не сообщает.

Сервер с ExtendedDesktopSize присылает мониторы сам. x11vnc этого не делает,
поэтому окно спрашивает раскладку по SSH у сеанса пользователя: сначала
``xrandr --listmonitors`` (X11, точные пиксели), затем ``kscreen-doctor -j``
(KDE, в том числе Wayland). Раскладка принимается, только если мониторы
целиком покрывают кадр VNC: иначе кадр снят не со всего рабочего стола
(например, kmsvnc отдаёт один монитор) и выбор экрана не нужен.
"""

from __future__ import annotations

import json
import re

from src.logger import logger

# Выполняется от пользователя графического сеанса (KRFBService._exec_as_xuser).
# Маркеры разбиты пустыми кавычками: эхо PTY повторяет текст команды, а в выводе
# должны найтись только напечатанные echo маркеры.
# Окружение сеанса берётся у plasmashell/kwin: без XAUTHORITY xrandr не подключится.
DETECT_SCRIPT = (
    'P=$(pgrep -u "$(id -u)" -x plasmashell | head -1); '
    '[ -z "$P" ] && P=$(pgrep -u "$(id -u)" -f "kwin_(x11|wayland)" | head -1); '
    'if [ -n "$P" ] && [ -r /proc/$P/environ ]; then '
    "for v in DISPLAY WAYLAND_DISPLAY XAUTHORITY; do "
    "val=$(tr '\\0' '\\n' < /proc/$P/environ | sed -n \"s/^$v=//p\" | head -1); "
    '[ -n "$val" ] && export "$v=$val"; '
    "done; fi; "
    '[ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ] && export DISPLAY=:0; '
    "echo DNO_\"\"XRANDR_BEGIN; "
    '[ -n "$DISPLAY" ] && command -v xrandr >/dev/null && xrandr --listmonitors 2>/dev/null; '
    "echo DNO_\"\"XRANDR_END; "
    "echo DNO_\"\"KSCREEN_BEGIN; "
    "command -v kscreen-doctor >/dev/null && timeout 10 kscreen-doctor -j 2>/dev/null; "
    "echo DNO_\"\"KSCREEN_END"
)

Rect = tuple[int, int, int, int]

_XRANDR_LINE = re.compile(r"^\s*\d+:\s+\S+\s+(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)")


def _section(output: str, name: str) -> str:
    begin, end = f"DNO_{name}_BEGIN", f"DNO_{name}_END"
    if begin not in output:
        return ""
    return output.split(begin, 1)[1].split(end, 1)[0]


def parse_xrandr(text: str) -> list[Rect]:
    rects = []
    for line in text.splitlines():
        match = _XRANDR_LINE.match(line)
        if match:
            w, h, x, y = (int(value) for value in match.groups())
            rects.append((x, y, w, h))
    return rects


def parse_kscreen(text: str) -> list[list[Rect]]:
    """Варианты раскладки из JSON kscreen: в пикселях режима и в логических единицах."""
    start = text.find("{")
    if start < 0:
        return []
    try:
        data = json.loads(text[start:])
    except ValueError:
        return []
    physical: list[Rect] = []
    logical: list[Rect] = []
    for output in data.get("outputs") or []:
        if not isinstance(output, dict) or not output.get("enabled") or not output.get("connected"):
            continue
        pos = output.get("pos") or {}
        size = output.get("size") or {}
        width, height = int(size.get("width") or 0), int(size.get("height") or 0)
        for mode in output.get("modes") or []:
            if isinstance(mode, dict) and str(mode.get("id")) == str(output.get("currentModeId")):
                mode_size = mode.get("size") or {}
                width = int(mode_size.get("width") or width)
                height = int(mode_size.get("height") or height)
        if int(output.get("rotation") or 1) in (2, 8):  # Left / Right
            width, height = height, width
        if width <= 0 or height <= 0:
            continue
        x, y = int(pos.get("x") or 0), int(pos.get("y") or 0)
        scale = float(output.get("scale") or 1.0) or 1.0
        physical.append((x, y, width, height))
        logical.append((x, y, round(width / scale), round(height / scale)))
    return [physical, logical]


def fits_framebuffer(rects: list[Rect], width: int, height: int) -> bool:
    """Мониторов больше одного, все внутри кадра, вместе покрывают его по габаритам."""
    if len(rects) < 2:
        return False
    if any(x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height for x, y, w, h in rects):
        return False
    return (
        min(x for x, _, _, _ in rects) == 0
        and min(y for _, y, _, _ in rects) == 0
        and max(x + w for x, _, w, _ in rects) == width
        and max(y + h for _, y, _, h in rects) == height
    )


def parse_screens(output: str, width: int, height: int) -> list[Rect]:
    candidates = [parse_xrandr(_section(output, "XRANDR"))]
    candidates.extend(parse_kscreen(_section(output, "KSCREEN")))
    for rects in candidates:
        if fits_framebuffer(rects, width, height):
            return sorted(rects, key=lambda rect: (rect[0], rect[1]))
    return []


def detect_screens(device, width: int, height: int) -> list[Rect]:
    """Мониторы хоста по SSH; пустой список - определить не удалось."""
    from src.di import get_container
    from src.services.remote.krfb_service import KRFBService

    service = get_container().resolve(KRFBService)
    output, ok = service._exec_as_xuser(device.host, DETECT_SCRIPT, timeout=25, device=device)
    if not ok and "DNO_XRANDR_BEGIN" not in (output or ""):
        logger.debug(f"VncScreens: {device.host}: раскладка не получена")
        return []
    return parse_screens(output or "", width, height)
