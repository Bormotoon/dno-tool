"""Базовые sub-painters тем иконок (дословно из gen_icon_themes.py).

Глифы рисуют смысл, стили line/solid/badge комбинируются вызывателем.
NO_BACKING_NAMES заполняет реестр (icons/__init__) именами без подложки.
"""

NO_BACKING_NAMES: set = set()

INK = "#FFFFFF"  # glyph ink on badge backing / details on solid

# ---------------------------------------------------------------- helpers ---


def st(c, w=7.0):
    return (
        f'fill="none" stroke="{c}" stroke-width="{w}" '
        f'stroke-linecap="round" stroke-linejoin="round"'
    )


def fi(c, op=None):
    return f'fill="{c}"' + (f' fill-opacity="{op}"' if op else "")


def backing(mode, color, name=""):
    if name in NO_BACKING_NAMES:
        # Имена без подложки (topo_* из scripts/icons/topo.py): чистый глиф.
        # Множество заполняет реестр scripts/icons/__init__.py.
        return ""
    if mode == "line":
        return (
            f'<rect x="14" y="14" width="100" height="100" rx="26" '
            f'fill="#FFFFFF" fill-opacity="0.05" stroke="#94A3B8" '
            f'stroke-opacity="0.4" stroke-width="2"/>'
        )
    if mode == "badge":
        return (
            f'<circle cx="64" cy="64" r="46" {fi(color)}/>'
            f'<ellipse cx="64" cy="46" rx="30" ry="12" '
            f'fill="#FFFFFF" fill-opacity="0.14"/>'
        )
    return ""


def doc(mode, color, name=""):
    """SVG document wrapper. Returns full inner svg."""
    if mode == "badge":
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
            f'viewBox="0 0 128 128">{backing(mode, color, name)}{{}}</svg>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
        f'viewBox="0 0 128 128">{backing(mode, color, name)}{{}}</svg>'
    )


# ------------------------------------------------- shared sub-painters ---


def arrow_lr(m, c, direction="right", y=64, x1=40, x2=88):
    """Horizontal arrow. m: line|solid|badge(ink handled by caller passing c)."""
    if direction == "right":
        head = f"M{x2 - 16} {y - 14} L{x2} {y} L{x2 - 16} {y + 14}"
        shaft = f'x1="{x1}" y1="{y}" x2="{x2}" y2="{y}"'
    else:
        head = f"M{x1 + 16} {y - 14} L{x1} {y} L{x1 + 16} {y + 14}"
        shaft = f'x1="{x2}" y1="{y}" x2="{x1}" y2="{y}"'
    if m == "solid":
        if direction == "right":
            return (
                f'<rect x="{x1}" y="{y - 6}" width="{x2 - x1 - 12}" height="12" rx="6" {fi(c)}/>'
                f'<path d="M{x2 - 22} {y - 17} L{x2 + 4} {y} L{x2 - 22} {y + 17} Z" {fi(c)}/>'
            )
        return (
            f'<rect x="{x1 + 12}" y="{y - 6}" width="{x2 - x1 - 12}" height="12" rx="6" {fi(c)}/>'
            f'<path d="M{x1 + 22} {y - 17} L{x1 - 4} {y} L{x1 + 22} {y + 17} Z" {fi(c)}/>'
        )
    return f'<line {shaft} {st(c)}/><path d="{head}" {st(c)}/>'


def arrow_ud(m, c, direction="down", x=64, y1=40, y2=88):
    if direction == "down":
        head = f"M{x - 14} {y2 - 16} L{x} {y2} L{x + 14} {y2 - 16}"
    else:
        head = f"M{x - 14} {y1 + 16} L{x} {y1} L{x + 14} {y1 + 16}"
    if m == "solid":
        if direction == "down":
            return (
                f'<rect x="{x - 6}" y="{y1}" width="12" height="{y2 - y1 - 12}" rx="6" {fi(c)}/>'
                f'<path d="M{x - 17} {y2 - 22} L{x} {y2 + 4} L{x + 17} {y2 - 22} Z" {fi(c)}/>'
            )
        return (
            f'<rect x="{x - 6}" y="{y1 + 12}" width="12" height="{y2 - y1 - 12}" rx="6" {fi(c)}/>'
            f'<path d="M{x - 17} {y1 + 22} L{x} {y1 - 4} L{x + 17} {y1 + 22} Z" {fi(c)}/>'
        )
    return (
        f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y2}" {st(c)}/>'
        f'<path d="{head}" {st(c)}/>'
    )


def chev(m, c, direction="right", single=False):
    """Chevron(s): back/forward/up/home-parts."""
    off = 0 if single else 12
    if direction == "right":
        p1 = f"M{52 - off} 40 L{76 - off} 64 L{52 - off} 88"
        p2 = f"M{76 - off} 40 L{100 - off} 64 L{76 - off} 88"
    elif direction == "left":
        p1 = f"M{76 + off} 40 L{52 + off} 64 L{76 + off} 88"
        p2 = f"M{100 + off} 40 L{76 + off} 64 L{100 + off} 88"
        p1, p2 = p2, p1
    elif direction == "up":
        p1 = f"M40 {76 - off} L64 {52 - off} L88 {76 - off}"
        p2 = f"M40 {100 - off} L64 {76 - off} L88 {100 - off}"
        p1, p2 = p2, p1
    else:
        p1 = f"M40 {52 + off} L64 {76 + off} L88 {52 + off}"
        p2 = f"M40 {76 + off} L64 {100 + off} L88 {76 + off}"
    if m == "solid":
        w = 13
        return f'<path d="{p1}" {st(c, w)}/>' + (
            "" if single else f'<path d="{p2}" {st(c, w)}/>'
        )
    w = 8 if single else 7
    return f'<path d="{p1}" {st(c, w)}/>' + (
        "" if single else f'<path d="{p2}" {st(c, w)}/>'
    )


def tray(m, c):
    if m == "solid":
        return f'<path d="M34 74 H94 L84 98 H44 Z" {fi(c)}/>'
    return f'<path d="M34 74 H94 L84 98 H44 Z" {st(c)}/>'


def prompt_mark(m, c, cx=64, cy=64, s=1.0):
    """Terminal prompt '>_': chevron + underscore bar."""
    gt = f"M{cx - 14 * s} {cy - 12 * s} L{cx + 2 * s} {cy} L{cx - 14 * s} {cy + 12 * s}"
    if m == "solid":
        return (
            f'<path d="{gt}" {st(c, 10 * s)}/>'
            f'<rect x="{cx + 8 * s}" y="{cy + 4 * s}" width="{22 * s}" '
            f'height="{9 * s}" rx="{4.5 * s}" {fi(c)}/>'
        )
    w = 8 * s
    return (
        f'<path d="{gt}" {st(c, w)}/>'
        f'<line x1="{cx + 8 * s}" y1="{cy + 10 * s}" x2="{cx + 26 * s}" '
        f'y2="{cy + 10 * s}" {st(c, w)}/>'
    )


def question(m, c):
    q = "M52 52 C52 42 76 42 76 54 C76 63 64 63 64 72"
    if m == "solid":
        return f'<path d="{q}" {st(c, 10)}/><circle cx="64" cy="86" r="7" {fi(c)}/>'
    return f'<path d="{q}" {st(c, 8)}/><circle cx="64" cy="86" r="5.5" {fi(c)}/>'


def exclam(m, c):
    if m == "solid":
        return (
            f'<rect x="58" y="38" width="12" height="34" rx="6" {fi(c)}/>'
            f'<circle cx="64" cy="84" r="7" {fi(c)}/>'
        )
    return (
        f'<line x1="64" y1="38" x2="64" y2="70" {st(c, 9)}/>'
        f'<circle cx="64" cy="84" r="5.5" {fi(c)}/>'
    )


def check(m, c, w=9):
    if m == "solid":
        return f'<path d="M42 66 L58 82 L88 48" {st(c, 13)}/>'
    return f'<path d="M42 66 L58 82 L88 48" {st(c, w)}/>'


def cross(m, c):
    if m == "solid":
        return f'<path d="M46 46 L82 82 M82 46 L46 82" {st(c, 13)}/>'
    return f'<path d="M46 46 L82 82 M82 46 L46 82" {st(c, 9)}/>'


def ring_circle(m, c, r=30, cx=64, cy=64):
    if m == "solid":
        return f'<circle cx="{cx}" cy="{cy}" r="{r}" {fi(c)}/>'
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" {st(c)}/>'


def status_dot(m, c, inner, r=30):
    """Colored ring/circle + white/colored inner symbol."""
    if m == "solid":
        return ring_circle(m, c, r) + inner.replace("@@", INK)
    if m == "badge":
        return inner.replace("@@", "#FFFFFF")
    return ring_circle(m, c, r) + inner.replace("@@", c)


def doc_shape(m, c, x=44, y=28, w=42, h=72, fold=16):
    d = f"M{x} {y} H{x + w - fold} L{x + w} {y + fold} V{y + h} H{x} Z"
    if m == "solid":
        fx, fy = x + w - fold, y
        return (
            f'<path d="{d}" {fi(c)}/>'
            f'<path d="M{fx} {fy} V{y + fold} H{x + w} Z" {fi(INK)}/>'
        )
    return (
        f'<path d="{d}" {st(c)}/>'
        f'<path d="M{x + w - fold} {y} V{y + fold} H{x + w}" {st(c, 5)}/>'
    )


def folder_shape(m, c):
    back = "M28 44 H58 L66 54 H100 V92 H28 Z"
    front = "M22 58 H106 V94 C106 100 101 104 95 104 H33 C27 104 22 100 22 94 Z"
    if m == "solid":
        return (
            f'<path d="{back}" {fi(c)}/>'
            f'<path d="{front}" {fi(c)}/>'
            f'<line x1="30" y1="66" x2="98" y2="66" stroke="{INK}" '
            f'stroke-width="4" stroke-linecap="round" opacity="0.7"/>'
        )
    if m == "badge":
        return (
            f'<path d="{back}" {fi("#FFFFFF", "0.55")}/>'
            f'<path d="{front}" {fi("#FFFFFF")}/>'
        )
    return f'<path d="{back}" {st(c, 6)}/><path d="{front}" {st(c, 6)}/>'


def monitor_shape(m, c, screen=None):
    body = '<rect x="30" y="34" width="68" height="44" rx="8" '
    stand = '<path d="M52 80 L76 80 L70 96 L58 96 Z" '
    base = '<rect x="48" y="96" width="32" height="8" rx="4" '
    scr = screen or c
    if m == "solid":
        return (
            body + fi(c) + "/>"
            f'<rect x="36" y="40" width="56" height="32" rx="5" {fi(INK)}/>'
            f'<rect x="40" y="46" width="30" height="5" rx="2.5" {fi(scr)} opacity="0.85"/>'
            f'<rect x="40" y="55" width="44" height="5" rx="2.5" {fi(scr)} opacity="0.55"/>'
            + stand
            + fi(c)
            + "/>"
            + base
            + fi(c)
            + "/>"
        )
    ink = c
    return (
        body + st(ink) + "/>"
        f'<rect x="38" y="42" width="24" height="5" rx="2.5" {fi(scr)} opacity="0.9"/>'
        f'<rect x="38" y="51" width="40" height="5" rx="2.5" {fi(scr)} opacity="0.6"/>'
        f'<rect x="38" y="60" width="30" height="5" rx="2.5" {fi(scr)} opacity="0.4"/>'
        + stand
        + st(ink, 6)
        + "/>"
        + base
        + fi(ink)
        + "/>"
    )


def thumb_frame(m, c):
    """Рамка миниатюры Veyon для новых тем (оверлей, экран прозрачный).

    Геометрия фиксирована и дублируется в theme.json каждой новой темы:
    корпус x=28 y=28 w=72 h=52, отверстие экрана x=34 y=34 w=60 h=33.75.
    Цвет рамки нейтральный (лежит на любом фоне приложения), стиль -
    под язык темы: контур / заливка / белый на бейдже.
    """
    body = '<rect x="28" y="28" width="72" height="52" rx="8" '
    stand = '<path d="M52 82 L76 82 L70 98 L58 98 Z" '
    base = '<rect x="48" y="98" width="32" height="8" rx="4" '
    if m == "solid":
        fc = "#3A4158"
        # Корпус с выбитым отверстием экрана (evenodd), иначе оверлей
        # закроет скриншот непрозрачной заливкой.
        punched = (
            '<path d="M28 28 H100 V80 H28 Z M34 34 V67.75 H94 V34 Z" '
            f'fill="{fc}" fill-rule="evenodd"/>'
        )
        return punched + stand + fi(fc) + "/>" + base + fi(fc) + "/>"
    if m == "badge":
        return body + st("#FFFFFF", 6) + "/>" + stand + st("#FFFFFF", 5) + "/>"
    fc = "#7C8698"
    return body + st(fc, 6) + "/>" + stand + st(fc, 5) + "/>" + base + fi(fc) + "/>"


def thumb_underlay(m, c):
    """Подложка миниатюры для темы badge: тёмный круг под скриншотом."""
    return '<circle cx="64" cy="62" r="56" fill="#2B3245"/>'


def gear_shape(m, c, cx=64, cy=62, r=26, teeth=8, tooth=13):
    parts = []
    if m == "solid":
        for i in range(teeth):
            parts.append(
                f'<rect x="{cx - 6}" y="{cy - r - tooth}" width="12" '
                f'height="{tooth + 8}" rx="4" {fi(c)} '
                f'transform="rotate({i * 360 / teeth} {cx} {cy})"/>'
            )
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" {fi(c)}/>')
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r - 13}" {fi(INK)}/>')
        return "".join(parts)
    for i in range(teeth):
        parts.append(
            f'<rect x="{cx - 5}" y="{cy - r - tooth + 4}" width="10" '
            f'height="{tooth}" rx="4" {fi(c)} '
            f'transform="rotate({i * 360 / teeth} {cx} {cy})"/>'
        )
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" {st(c)}/>')
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="8" {fi(c)}/>')
    return "".join(parts)


def eye_shape(m, c, cx=64, cy=64, s=1.0):
    almond = (
        f"M{cx - 34 * s} {cy} Q{cx} {cy - 30 * s} "
        f"{cx + 34 * s} {cy} Q{cx} {cy + 30 * s} {cx - 34 * s} {cy} Z"
    )
    if m == "solid":
        return (
            f'<path d="{almond}" {fi(c)}/>'
            f'<circle cx="{cx}" cy="{cy}" r="{11 * s}" {fi(INK)}/>'
        )
    return (
        f'<path d="{almond}" {st(c)}/>'
        f'<circle cx="{cx}" cy="{cy}" r="{10 * s}" {fi(c)}/>'
    )


def eye_off(m, c):
    s = eye_shape(m, c)
    if m == "solid":
        return s + f'<path d="M36 92 L92 36" {st(INK, 9)}/>'
    return s + f'<path d="M36 92 L92 36" {st(c, 8)}/>'


def lock_shape(m, c, locked=True):
    shackle = (
        "M44 62 V50 C44 38 84 38 84 50 V62"
        if locked
        else "M44 62 V50 C44 38 78 36 84 44"
    )
    if m == "solid":
        return (
            f'<path d="{shackle}" {st(c, 11)}/>'
            f'<rect x="38" y="60" width="52" height="40" rx="10" {fi(c)}/>'
            f'<circle cx="64" cy="76" r="6" {fi(INK)}/>'
            f'<rect x="61" y="78" width="6" height="12" rx="3" {fi(INK)}/>'
        )
    return (
        f'<path d="{shackle}" {st(c)}/>'
        f'<rect x="38" y="60" width="52" height="40" rx="10" {st(c)}/>'
        f'<circle cx="64" cy="78" r="5" {fi(c)}/>'
    )


def key_shape(m, c, angle=0):
    g = (
        f'<circle cx="46" cy="64" r="14" '
        + (fi(c) if m == "solid" else st(c))
        + '/><line x1="58" y1="64" x2="94" y2="64" '
        + st(c, 9 if m == "solid" else 7)
        + f'/><line x1="84" y1="64" x2="84" y2="76" {st(c, 7)}/>'
        f'<line x1="94" y1="64" x2="94" y2="76" {st(c, 7)}/>'
    )
    if angle:
        return f'<g transform="rotate({angle} 64 64)">{g}</g>'
    return g


def keyboard(m, c):
    if m == "solid":
        dots = "".join(
            f'<circle cx="{x}" cy="{y}" r="4" {fi(INK)}/>'
            for y in (58, 72, 86)
            for x in (40, 54, 68, 82)
        )
        return f'<rect x="28" y="44" width="72" height="52" rx="10" {fi(c)}/>' + dots
    dots = "".join(
        f'<circle cx="{x}" cy="{y}" r="3.5" {fi(c)}/>'
        for y in (58, 72, 86)
        for x in (40, 54, 68, 82)
    )
    return f'<rect x="28" y="44" width="72" height="52" rx="10" {st(c)}/>' + dots


def power_symbol(m, c):
    arc = "M42 52 C42 38 86 38 86 52 C86 66 76 76 64 76 C52 76 42 66 42 52"
    if m == "solid":
        return (
            f'<path d="{arc}" {st(c, 11)}/>'
            f'<line x1="64" y1="30" x2="64" y2="66" {st(c, 11)}/>'
        )
    return (
        f'<path d="{arc}" {st(c, 8)}/>'
        f'<line x1="64" y1="30" x2="64" y2="66" {st(c, 8)}/>'
    )


def circular_arrows(m, c, double=True):
    a1 = "M40 70 C40 48 88 44 92 60"
    h1 = "M80 48 L93 60 L78 68"
    a2 = "M88 58 C88 80 40 84 36 68"
    h2 = "M48 80 L35 68 L50 60"
    w = 10 if m == "solid" else 7
    s = f'<path d="{a1}" {st(c, w)}/><path d="{h1}" {st(c, w)}/>'
    if double:
        s += f'<path d="{a2}" {st(c, w)}/><path d="{h2}" {st(c, w)}/>'
    return s


def radar(m, c):
    arcs = "".join(
        f'<path d="M{64 - r} 84 A{r} {r} 0 0 1 {64 + r} 84" {st(c, 6)}/>'
        for r in (18, 32, 46)
    )
    sweep = f'<line x1="64" y1="84" x2="92" y2="46" {st(c, 6)}/>'
    if m == "solid":
        return arcs + sweep + f'<circle cx="64" cy="84" r="9" {fi(c)}/>'
    if m == "badge":
        return arcs + sweep + f'<circle cx="64" cy="84" r="7" {fi("#FFFFFF")}/>'
    return arcs + sweep + f'<circle cx="64" cy="84" r="7" {fi(c)}/>'


def lens(m, c, cx=56, cy=56, r=24):
    if m == "solid":
        return (
            f'<circle cx="{cx}" cy="{cy}" r="{r}" {fi(c)}/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r - 11}" {fi(INK)}/>'
            f'<line x1="{cx + r - 6}" y1="{cy + r - 6}" '
            f'x2="{cx + r + 12}" y2="{cy + r + 12}" {st(c, 11)}/>'
        )
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" {st(c)}/>'
        f'<line x1="{cx + r - 6}" y1="{cy + r - 6}" '
        f'x2="{cx + r + 12}" y2="{cy + r + 12}" {st(c, 8)}/>'
    )


def person(m, c, cx=64, cy=58, s=1.0):
    head = (
        f'<circle cx="{cx}" cy="{cy - 14 * s}" r="{13 * s}" '
        + (fi(c) if m == "solid" else st(c, 7 * s))
        + "/>"
    )
    sh = (
        f"M{cx - 24 * s} {cy + 30 * s} C{cx - 24 * s} {cy + 10 * s} "
        f"{cx + 24 * s} {cy + 10 * s} {cx + 24 * s} {cy + 30 * s}"
    )
    w = 11 * s if m == "solid" else 7 * s
    return head + f'<path d="{sh}" {st(c, w)}/>'


def camera_body(m, c, rec=None):
    if m == "solid":
        body = (
            f'<rect x="26" y="48" width="76" height="46" rx="12" {fi(c)}/>'
            f'<rect x="46" y="38" width="26" height="12" rx="5" {fi(c)}/>'
            f'<circle cx="64" cy="71" r="15" {fi(INK)}/>'
        )
        if rec == "dot":
            return body + f'<circle cx="64" cy="71" r="8" {fi("#EF4444")}/>'
        if rec == "stop":
            return body + f'<rect x="56" y="63" width="16" height="16" rx="3" {fi(c)}/>'
        return body + f'<circle cx="64" cy="71" r="8" {fi(c)}/>'
    body = (
        f'<rect x="26" y="48" width="76" height="46" rx="12" {st(c)}/>'
        f'<path d="M46 48 V42 C46 39 48 38 51 38 H77 C80 38 82 39 82 42 V48" {st(c, 6)}/>'
        f'<circle cx="64" cy="71" r="14" {st(c, 6)}/>'
    )
    if rec == "dot":
        return body + f'<circle cx="64" cy="71" r="6" {fi("#EF4444")}/>'
    if rec == "stop":
        return body + f'<rect x="57" y="64" width="14" height="14" rx="3" {fi(c)}/>'
    return body


def bubble(m, c):
    if m == "solid":
        return (
            f'<rect x="30" y="36" width="68" height="44" rx="14" {fi(c)}/>'
            f'<path d="M44 80 L40 96 L58 80 Z" {fi(c)}/>'
            f'<circle cx="52" cy="58" r="5" {fi(INK)}/>'
            f'<circle cx="66" cy="58" r="5" {fi(INK)}/>'
            f'<circle cx="80" cy="58" r="5" {fi(INK)}/>'
        )
    return (
        f'<rect x="30" y="36" width="68" height="44" rx="14" {st(c)}/>'
        f'<path d="M44 80 L40 96 L58 80 Z" {st(c, 6)}/>'
        f'<circle cx="52" cy="58" r="4" {fi(c)}/>'
        f'<circle cx="66" cy="58" r="4" {fi(c)}/>'
        f'<circle cx="80" cy="58" r="4" {fi(c)}/>'
    )


def globe(m, c):
    if m == "solid":
        return (
            f'<circle cx="64" cy="64" r="32" {fi(c)}/>'
            f'<ellipse cx="64" cy="64" rx="14" ry="32" fill="none" '
            f'stroke="{INK}" stroke-width="5"/>'
            f'<line x1="34" y1="52" x2="94" y2="52" stroke="{INK}" stroke-width="5"/>'
            f'<line x1="34" y1="76" x2="94" y2="76" stroke="{INK}" stroke-width="5"/>'
        )
    return (
        f'<circle cx="64" cy="64" r="32" {st(c)}/>'
        f'<ellipse cx="64" cy="64" rx="14" ry="32" {st(c, 5)}/>'
        f'<line x1="34" y1="52" x2="94" y2="52" {st(c, 5)}/>'
        f'<line x1="34" y1="76" x2="94" y2="76" {st(c, 5)}/>'
    )


def plane(m, c):
    if m == "solid":
        return f'<path d="M92 36 L40 66 L60 70 L66 92 Z" {fi(c)}/>'
    return (
        f'<path d="M92 36 L40 66 L60 70 L66 92 Z" {st(c, 6)}/>'
        f'<line x1="60" y1="70" x2="92" y2="36" {st(c, 5)}/>'
    )


def door(m, c, enter=True):
    direction = "right" if enter else "left"
    if m == "solid":
        frame = f'<rect x="36" y="30" width="40" height="68" rx="6" {fi(c)}/>'
        arr = arrow_lr("solid", c, direction, y=64, x1=58, x2=100)
    else:
        frame = f'<rect x="36" y="30" width="40" height="68" rx="6" {st(c)}/>'
        arr = arrow_lr("line", c, direction, y=64, x1=58, x2=100)
    return frame + arr


def trash(m, c):
    """Корзина с отдельной крышкой, ручкой и двумя широкими прорезями."""
    if m == "solid":
        return (
            f'<path d="M54 34 V30 Q54 26 58 26 H70 Q74 26 74 30 V34" {st(c, 6)}/>'
            f'<rect x="34" y="34" width="60" height="8" rx="4" {fi(c)}/>'
            f'<path d="M41 48 H87 L83 92 Q82 98 76 98 H52 Q46 98 45 92 Z" {fi(c)}/>'
            f'<path d="M56 59 L58 85 M72 59 L70 85" {st(INK, 6)}/>'
        )
    return (
        f'<path d="M54 35 V30 Q54 26 58 26 H70 Q74 26 74 30 V35 M35 38 H93" {st(c, 6)}/>'
        f'<path d="M42 48 L46 92 Q47 98 53 98 H75 Q81 98 82 92 L86 48" {st(c, 6)}/>'
        f'<path d="M56 59 L58 84 M72 59 L70 84" {st(c, 5)}/>'
    )


def text_add(m, c):
    """Добавить текст: буква T + зеленый плюс (без <text>: Qt SVG его не рендерит).

    T - заливка в solid, контур в line/badge; плюс-бейдж всегда заливка
    (как у menu_add_folder).
    """
    if m == "solid":
        t = (
            f'<rect x="28" y="34" width="46" height="11" rx="5.5" {fi(c)}/>'
            f'<rect x="45.5" y="34" width="11" height="54" rx="5.5" {fi(c)}/>'
        )
    else:
        t = f'<path d="M28 40 H74" {st(c)}/><path d="M51 40 V88" {st(c)}/>'
    plus = (
        f'<circle cx="88" cy="88" r="16" {fi("#22C55E")}/>'
        f'<path d="M88 80 V96 M80 88 H96" {st(INK, 6)}/>'
    )
    return t + plus


def pencil(m, c):
    g = (
        f'<rect x="42" y="52" width="20" height="44" rx="6" '
        + (fi(c) if m == "solid" else st(c, 6))
        + ' transform="rotate(45 52 74)"/>'
        f'<path d="M72 90 L84 94 L80 82 Z" {fi(c)}/>'
    )
    return f"<g>{g}</g>"


def floppy(m, c):
    if m == "solid":
        return (
            f'<rect x="36" y="32" width="56" height="64" rx="8" {fi(c)}/>'
            f'<rect x="48" y="32" width="24" height="22" rx="3" {fi(INK)}/>'
            f'<rect x="46" y="66" width="36" height="22" rx="3" {fi(INK)}/>'
        )
    return (
        f'<rect x="36" y="32" width="56" height="64" rx="8" {st(c)}/>'
        f'<rect x="48" y="32" width="24" height="22" rx="3" {st(c, 5)}/>'
        f'<rect x="46" y="66" width="36" height="22" rx="3" {st(c, 5)}/>'
    )


def box_shape(m, c, arrow=None):
    if m == "solid":
        s = f'<path d="M36 48 L64 36 L92 48 V88 L64 100 L36 88 Z" {fi(c)}/>'
        s += (
            f'<path d="M36 48 L64 60 L92 48" fill="none" stroke="{INK}" '
            f'stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    else:
        s = (
            f'<path d="M36 48 L64 36 L92 48 V88 L64 100 L36 88 Z" {st(c, 6)}/>'
            f'<path d="M36 48 L64 60 L92 48 M64 60 V100" {st(c, 5)}/>'
        )
    if arrow == "down":
        s += arrow_ud(
            "line" if m != "solid" else "solid",
            INK if m == "solid" else c,
            "down",
            x=64,
            y1=58,
            y2=86,
        )
    if arrow == "up":
        s += arrow_ud(
            "line" if m != "solid" else "solid",
            INK if m == "solid" else c,
            "up",
            x=64,
            y1=58,
            y2=86,
        )
    return s


def server_rack(m, c, lights="#22C55E"):
    units = ""
    for y in (34, 62):
        if m == "solid":
            units += (
                f'<rect x="34" y="{y}" width="60" height="24" rx="6" {fi(c)}/>'
                f'<circle cx="44" cy="{y + 12}" r="4" {fi(lights)}/>'
                f'<rect x="54" y="{y + 9}" width="30" height="6" rx="3" {fi(INK)}/>'
            )
        else:
            units += (
                f'<rect x="34" y="{y}" width="60" height="24" rx="6" {st(c, 6)}/>'
                f'<circle cx="44" cy="{y + 12}" r="3.5" {fi(lights)}/>'
                f'<rect x="54" y="{y + 9}" width="30" height="5" rx="2.5" {fi(c)} opacity="0.6"/>'
            )
    return units


def link_nodes(m, c):
    pts = [(44, 44), (84, 44), (64, 84)]
    s = "".join(
        f'<line x1="{pts[i][0]}" y1="{pts[i][1]}" x2="{pts[j][0]}" y2="{pts[j][1]}" {st(c, 6)}/>'
        for i, j in ((0, 1), (0, 2), (1, 2))
    )
    for x, y in pts:
        s += (
            f'<circle cx="{x}" cy="{y}" r="11" {fi(c)}/>'
            if m == "solid"
            else f'<circle cx="{x}" cy="{y}" r="11" {st(c, 6)}/><circle cx="{x}" cy="{y}" r="4" {fi(c)}/>'
        )
    return s


def chip_shape(m, c, cx=64, cy=64, half=24, pins=3):
    pins_s = ""
    step = (half * 2) // (pins + 1)
    for i in range(1, pins + 1):
        o = -half + i * step
        for x, y, dx, dy in (
            (cx + o, cy - half, 0, -12),
            (cx + o, cy + half, 0, 12),
            (cx - half, cy + o, -12, 0),
            (cx + half, cy + o, 12, 0),
        ):
            pins_s += (
                f'<line x1="{x}" y1="{y}" x2="{x + dx}" y2="{y + dy}" {st(c, 6)}/>'
            )
    if m == "solid":
        return (
            pins_s + f'<rect x="{cx - half}" y="{cy - half}" '
            f'width="{half * 2}" height="{half * 2}" rx="8" {fi(c)}/>'
            f'<rect x="{cx - half + 12}" y="{cy - half + 12}" '
            f'width="{half * 2 - 24}" height="{half * 2 - 24}" rx="4" {fi(INK)}/>'
        )
    return (
        pins_s + f'<rect x="{cx - half}" y="{cy - half}" '
        f'width="{half * 2}" height="{half * 2}" rx="8" {st(c)}/>'
        f'<rect x="{cx - half + 12}" y="{cy - half + 12}" '
        f'width="{half * 2 - 24}" height="{half * 2 - 24}" rx="4" {st(c, 5)}/>'
    )


def shield_shape(m, c, symbol="check"):
    d = (
        "M64 28 C76 28 86 32 90 36 C94 52 92 74 64 100 "
        "C36 74 34 52 38 36 C42 32 52 28 64 28 Z"
    )
    if m == "solid":
        s = f'<path d="{d}" {fi(c)}/>'
    elif m == "badge":
        s = f'<path d="{d}" {fi("#FFFFFF")}/>'
        sym_c = "__SYM__"  # theme color, resolved in render()
        if symbol == "check":
            return s + check("line", sym_c, 8)
        if symbol == "key":
            return s + (
                f'<circle cx="64" cy="56" r="7" {st(sym_c, 6)}/>'
                f'<line x1="64" y1="63" x2="64" y2="80" {st(sym_c, 6)}/>'
            )
        return s
    else:
        s = f'<path d="{d}" {st(c)}/>'
    if symbol == "check":
        return s + check(
            "line" if m != "solid" else "solid", INK if m == "solid" else c, 8
        )
    if symbol == "key":
        kc = INK if m == "solid" else c
        return s + (
            f'<circle cx="64" cy="56" r="7" {st(kc, 6) if m != "solid" else fi(kc)}/>'
            f'<line x1="64" y1="63" x2="64" y2="80" {st(kc, 6)}/>'
        )
    return s


def broadcast(m, c, cx=64, cy=66):
    s = ""
    for r in (20, 36):
        s += f'<path d="M{cx - r} {cy} A{r} {r} 0 0 1 {cx + r} {cy}" {st(c, 6)}/>'
    s += f'<circle cx="{cx}" cy="{cy}" r="9" {fi(c)}/>'
    return s


def sliders(m, c):
    s = ""
    for i, y in enumerate((44, 64, 84)):
        kx = (52, 76, 60)[i]
        if m == "solid":
            s += (
                f'<rect x="34" y="{y - 4}" width="60" height="8" rx="4" {fi(c)}/>'
                f'<circle cx="{kx}" cy="{y}" r="10" {fi(c)} stroke="{INK}" stroke-width="4"/>'
            )
        else:
            s += (
                f'<line x1="34" y1="{y}" x2="94" y2="{y}" {st(c, 6)}/>'
                f'<circle cx="{kx}" cy="{y}" r="9" {fi(c) if m == "line" else "#FFFFFF"}/>'
                f'<circle cx="{kx}" cy="{y}" r="9" {st(c, 5)}/>'
                if m == "line"
                else f'<circle cx="{kx}" cy="{y}" r="9" {fi("#FFFFFF")}/>'
            )
    return s


def wrench(m, c):
    jaw = "M52 38 C44 44 42 56 48 64"
    if m == "solid":
        return (
            f'<path d="{jaw}" {st(c, 12)}/>'
            f'<line x1="56" y1="62" x2="88" y2="94" {st(c, 13)}/>'
            f'<line x1="40" y1="88" x2="72" y2="56" {st(c, 7)}/>'
            if False
            else f'<line x1="56" y1="62" x2="88" y2="94" {st(c, 13)}/>'
        )
    return (
        f'<path d="{jaw}" {st(c, 8)}/>'
        f'<line x1="56" y1="62" x2="88" y2="94" {st(c, 8)}/>'
        f'<line x1="44" y1="84" x2="62" y2="66" {st(c, 6)}/>'
    )


def table_shape(m, c, dot="#22C55E"):
    if m == "solid":
        return (
            f'<rect x="32" y="36" width="64" height="56" rx="8" {fi(c)}/>'
            f'<rect x="32" y="36" width="64" height="18" rx="8" {fi(INK)} opacity="0.85"/>'
            f'<rect x="38" y="62" width="34" height="6" rx="3" {fi(INK)} opacity="0.8"/>'
            f'<rect x="38" y="73" width="44" height="6" rx="3" {fi(INK)} opacity="0.55"/>'
            f'<circle cx="84" cy="76" r="8" {fi(dot)} stroke="{INK}" stroke-width="3"/>'
        )
    return (
        f'<rect x="32" y="36" width="64" height="56" rx="8" {st(c)}/>'
        f'<line x1="32" y1="54" x2="96" y2="54" {st(c, 5)}/>'
        f'<rect x="38" y="62" width="30" height="6" rx="3" {fi(c)} opacity="0.7"/>'
        f'<rect x="38" y="73" width="40" height="6" rx="3" {fi(c)} opacity="0.45"/>'
        f'<circle cx="84" cy="76" r="8" {fi(dot)}/>'
    )


def image_shape(m, c):
    if m == "solid":
        return (
            f'<rect x="32" y="40" width="64" height="50" rx="8" {fi(c)}/>'
            f'<circle cx="48" cy="56" r="7" {fi(INK)}/>'
            f'<path d="M36 86 L56 64 L70 78 L78 70 L92 86 Z" {fi(INK)}/>'
        )
    return (
        f'<rect x="32" y="40" width="64" height="50" rx="8" {st(c)}/>'
        f'<circle cx="48" cy="56" r="6" {fi(c)}/>'
        f'<path d="M36 86 L56 64 L70 78 L78 70 L92 86 Z" {st(c, 5)}/>'
    )


def grid4(m, c):
    s = ""
    for x, y in ((36, 38), (68, 38), (36, 70), (68, 70)):
        if m == "solid":
            s += f'<rect x="{x}" y="{y}" width="24" height="20" rx="5" {fi(c)}/>'
        else:
            s += f'<rect x="{x}" y="{y}" width="24" height="20" rx="5" {st(c, 5)}/>'
    return s


def pulse(m, c):
    return (
        f'<rect x="30" y="38" width="68" height="44" rx="8" {st(c) if m != "solid" else fi(c)}/>'
        + (
            f'<path d="M38 66 H52 L58 52 L68 78 L74 60 H90" '
            f"{st(INK if m == 'solid' else c, 6)}/>"
        )
        + (
            f'<path d="M52 84 L76 84 L70 96 L58 96 Z" {fi(c) if m == "solid" else st(c, 5)}/>'
            if True
            else ""
        )
    )


def film(m, c):
    if m == "solid":
        dots = "".join(
            f'<rect x="{x}" y="{y}" width="7" height="7" rx="2" {fi(INK)}/>'
            for x in (36, 85)
            for y in (44, 58, 72)
        )
        return (
            f'<rect x="30" y="36" width="68" height="52" rx="8" {fi(c)}/>'
            + dots
            + f'<circle cx="64" cy="62" r="9" {fi("#EF4444")}/>'
        )
    dots = "".join(
        f'<rect x="{x}" y="{y}" width="7" height="7" rx="2" {fi(c)}/>'
        for x in (36, 85)
        for y in (44, 58, 72)
    )
    return (
        f'<rect x="30" y="36" width="68" height="52" rx="8" {st(c)}/>'
        + dots
        + f'<circle cx="64" cy="62" r="8" {fi("#EF4444")}/>'
    )


# ============================================================ icon painters ==
# Each painter: (mode, ink) -> inner SVG. ink = theme color, or white in badge.


def p_unknown(m, c):
    if m == "solid":
        return (
            f'<rect x="38" y="30" width="52" height="68" rx="12" {fi(c)}/>'
            + question("solid", INK)
        )
    if m == "badge":
        return (
            f'<rect x="38" y="30" width="52" height="68" rx="12" '
            f"{fi('#FFFFFF')}/>" + question("line", "__SYM__")
        )
    return (
        f'<rect x="38" y="30" width="52" height="68" rx="12" '
        f'{st(c)} stroke-dasharray="10 7"/>' + question("line", c)
    )


def p_logo(m, c, on=False):
    sq = (
        f'<rect x="30" y="34" width="68" height="60" rx="14" '
        + (fi("#2B3245") if m != "badge" else fi("#FFFFFF"))
        + "/>"
    )
    pr = prompt_mark("line", "#22C55E" if m != "badge" else c, 58, 58, 1.1)
    dot = f'<circle cx="64" cy="82" r="6" {fi("#22C55E")}/>' + (
        f'<circle cx="64" cy="82" r="10" fill="none" stroke="#22C55E" '
        f'stroke-width="2.5"/>'
        if on
        else ""
    )
    return sq + pr + dot


# ------------------------------------------------------------- paint table --


def lock_mini(m, c, locked):
    if m == "solid":
        sh = (
            "M78 46 V40 C78 32 98 32 98 40 V46"
            if locked
            else "M78 46 V40 C78 32 94 31 98 36"
        )
        return (
            f'<circle cx="88" cy="44" r="22" {fi(c)}/>'
            f'<path d="{sh}" {st(INK, 6)}/>'
            f'<rect x="74" y="44" width="28" height="22" rx="5" {fi(INK)}/>'
        )
    sh = (
        "M80 46 V40 C80 32 96 32 96 40 V46"
        if locked
        else "M80 46 V40 C80 32 93 31 96 35"
    )
    return (
        f'<rect x="72" y="44" width="32" height="26" rx="7" {st(c, 6)}/>'
        f'<path d="{sh}" {st(c, 6)}/>'
    )


def table_card(m, c):
    if m == "solid":
        return (
            f'<rect x="36" y="32" width="56" height="64" rx="10" {fi(c)}/>'
            f'<circle cx="64" cy="50" r="8" {fi(INK)}/>'
            f'<rect x="46" y="64" width="36" height="6" rx="3" {fi(INK)} opacity="0.85"/>'
            + f'<rect x="46" y="74" width="28" height="6" rx="3" {fi(INK)} opacity="0.6"/>'
        )
    return (
        f'<rect x="36" y="32" width="56" height="64" rx="10" {st(c)}/>'
        f'<circle cx="64" cy="50" r="7" {st(c, 5)}/>'
        f'<rect x="46" y="64" width="36" height="6" rx="3" {fi(c)} opacity="0.7"/>'
        + f'<rect x="46" y="74" width="28" height="6" rx="3" {fi(c)} opacity="0.5"/>'
    )


def topology_graph(m, c):
    """Карта сети: hub-and-spoke (центр + 3 сателлита).

    Отличие от menu_nodes (треугольник link_nodes): звезда с хабом.
    """
    hub, sats = (64, 64), ((36, 44), (92, 44), (64, 94))
    lines = "".join(
        f'<line x1="{hub[0]}" y1="{hub[1]}" x2="{x}" y2="{y}" {st(c, 6)}/>'
        for x, y in sats
    )
    if m == "solid":
        nodes = f'<circle cx="{hub[0]}" cy="{hub[1]}" r="13" {fi(c)}/>'
        nodes += f'<circle cx="{hub[0]}" cy="{hub[1]}" r="5" {fi(INK)}/>'
        for x, y in sats:
            nodes += f'<circle cx="{x}" cy="{y}" r="10" {fi(c)}/>'
            nodes += f'<circle cx="{x}" cy="{y}" r="4" {fi(INK)}/>'
        return lines + nodes
    nodes = f'<circle cx="{hub[0]}" cy="{hub[1]}" r="13" {st(c, 6)}/>'
    nodes += f'<circle cx="{hub[0]}" cy="{hub[1]}" r="4.5" {fi(c)}/>'
    for x, y in sats:
        nodes += f'<circle cx="{x}" cy="{y}" r="10" {st(c, 6)}/>'
        nodes += f'<circle cx="{x}" cy="{y}" r="3.5" {fi(c)}/>'
    return lines + nodes


def video_editing_table(m, c):
    """Видеоредактор: кинолента с play + полоса таймлайна.

    Отличие от menu_recording (кольцо REC): монтажный стол.
    """
    holes = "".join(
        f'<rect x="{x}" y="{y}" width="7" height="7" rx="2" '
        f"{fi(INK) if m == 'solid' else fi(c)}/>"
        for x in (34, 87)
        for y in (40, 52, 64)
    )
    tri = '<path d="M58 46 L78 58 L58 70 Z" '
    if m == "solid":
        body = f'<rect x="28" y="32" width="72" height="52" rx="8" {fi(c)}/>'
        body += holes + tri + fi(INK) + "/>"
        bar = f'<rect x="28" y="92" width="72" height="10" rx="5" {fi(c)}/>'
        bar += (
            f'<circle cx="72" cy="97" r="9" {fi(c)} stroke="{INK}" stroke-width="4"/>'
        )
        return body + bar
    body = f'<rect x="28" y="32" width="72" height="52" rx="8" {st(c)}/>'
    body += holes + tri + fi(c) + "/>"
    bar = f'<rect x="28" y="92" width="72" height="10" rx="5" {st(c, 5)}/>'
    bar += f'<circle cx="72" cy="97" r="8" {fi(c)}/>'
    return body + bar


def bent_arrow(m, c, direction="undo"):
    """Undo/redo: ломаная стрелка снизу вверх (преемник рисованных кнопок preview)."""
    w = 10 if m == "solid" else 7
    if direction == "undo":
        return (
            f'<path d="M84 88 V58 H42" {st(c, w)}/>'
            f'<path d="M54 46 L40 58 L54 70" {st(c, w)}/>'
        )
    return (
        f'<path d="M44 88 V58 H86" {st(c, w)}/>'
        f'<path d="M74 46 L88 58 L74 70" {st(c, w)}/>'
    )


def transport_glyph(m, c, kind):
    """Кнопки воспроизведения, остановки и перемещения по кадрам."""
    if kind == "play":
        return f'<path d="M50 38 L90 64 L50 90 Z" {fi(c)}/>'
    if kind == "pause":
        return (
            f'<rect x="46" y="38" width="13" height="52" rx="6" {fi(c)}/>'
            f'<rect x="69" y="38" width="13" height="52" rx="6" {fi(c)}/>'
        )
    if kind == "stop":
        return f'<rect x="42" y="42" width="44" height="44" rx="3" {fi(c)}/>'
    if kind == "prev":
        return (
            f'<rect x="36" y="42" width="11" height="44" rx="4" {fi(c)}/>'
            f'<path d="M90 42 L58 64 L90 86 Z" {fi(c)}/>'
        )
    if kind == "to_start":
        # В начало: полоса + двойной треугольник влево (|<<).
        return (
            f'<rect x="30" y="42" width="9" height="44" rx="4" {fi(c)}/>'
            f'<path d="M63 42 L41 64 L63 86 Z" {fi(c)}/>'
            f'<path d="M89 42 L67 64 L89 86 Z" {fi(c)}/>'
        )
    # next
    return (
        f'<path d="M38 42 L70 64 L38 86 Z" {fi(c)}/>'
        f'<rect x="81" y="42" width="11" height="44" rx="4" {fi(c)}/>'
    )


def mark_bracket(m, c, which="in"):
    """Метка In/Out: квадратная скобка (преемник рисованных кнопок preview)."""
    w = 10 if m == "solid" else 8
    if which == "in":
        return f'<path d="M58 34 H44 V94 H58" {st(c, w)}/>'
    return f'<path d="M70 34 H84 V94 H70" {st(c, w)}/>'


def scissors(m, c):
    """Ножницы split: два кольца + скрещенные лезвия."""
    w = 8 if m == "solid" else 7
    return (
        f'<circle cx="44" cy="46" r="9" {st(c, w)}/>'
        f'<circle cx="44" cy="82" r="9" {st(c, w)}/>'
        f'<line x1="51" y1="52" x2="92" y2="86" {st(c, 6)}/>'
        f'<line x1="51" y1="76" x2="92" y2="42" {st(c, 6)}/>'
    )


def range_brackets(m, c, mode="cut"):
    """Диапазон I..O: две скобки + крест (cut) или заливка (keep)."""
    w = 8 if m == "solid" else 7
    s = (
        f'<path d="M44 34 H32 V94 H44" {st(c, w)}/>'
        f'<path d="M84 34 H96 V94 H84" {st(c, w)}/>'
    )
    if mode == "cut":
        return s + f'<path d="M56 56 L72 72 M72 56 L56 72" {st("#EF4444", 8)}/>'
    return s + f'<rect x="56" y="52" width="16" height="24" rx="3" {fi(c)}/>'


def speaker_body(m, c):
    spk = "M34 56 H48 L66 40 V88 L48 72 H34 Z"
    if m == "solid":
        return f'<path d="{spk}" {fi(c)}/>'
    return f'<path d="{spk}" {st(c, 6)}/>'


def clear_list_glyph(m, c):
    lines = "".join(
        f'<line x1="30" y1="{y}" x2="68" y2="{y}" {st(c, 6)}/>' for y in (44, 60, 76)
    )
    return lines + f'<path d="M78 84 L98 64 M98 84 L78 64" {st("#EF4444", 8)}/>'
