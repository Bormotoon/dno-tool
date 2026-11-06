"""Параметры титров и геометрия, общие для предпросмотра и экспорта."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.domain.video.models import TextOverlay
    from src.domain.video.render_plan import RenderText


def default_fontfile() -> str:
    """Шрифт по умолчанию под Windows/Linux."""
    if os.name == "nt":
        for cand in (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\verdana.ttf"):
            if Path(cand).exists():
                return cand
        return ""
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    ):
        if Path(cand).exists():
            return cand
    from src.utils.system.media_runtime import media_runtime_root

    bundled = media_runtime_root() / "media-fonts/DejaVuSans.ttf"
    if bundled.is_file():
        return str(bundled)
    return ""


def escape_drawtext(text: str) -> str:
    """Экранировать текст для фильтра drawtext."""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace("\n", " ")
    )


_fontconfig_cache: bool | None = None


def _has_fontconfig() -> bool:
    """Есть ли fontconfig в сборке ffmpeg (для drawtext font='Имя').

    Результат кэшируется: вызов - один ffmpeg -buildconf за сессию.
    """

    return True


def _filter_option(value: str) -> str:
    """Два уровня: значение AVOption, затем лексер filtergraph; shell отсутствует."""
    first = "".join("\\" + char if char in "\\':" else char for char in value)
    return "".join("\\" + char if char in "\\'[],; " else char for char in first)


def _norm_drawtext_color(color: str) -> str:
    """Цвет для drawtext: имена как есть, #rrggbb -> 0xrrggbb."""
    c = (color or "").strip()
    if c.startswith("#"):
        return "0x" + c[1:]
    return c or "white"


def _escape_drawtext_value(value: str) -> str:
    """Экранировать значение параметра drawtext (имя шрифта и т.п.)."""
    return (value or "").replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def _clamp_margin(v: Any, default: int = 20) -> int:
    try:
        return max(0, min(16384, int(v)))
    except (TypeError, ValueError):
        return default


def _clamp_pad(v: Any, default: int = 6) -> int:
    try:
        return max(0, min(30, int(v)))
    except (TypeError, ValueError):
        return default


def overlay_rect(
    corner: str, margin_x: int, margin_y: int, box_pad: int
) -> tuple[str, str]:
    """Якорные выражения origin ТЕКСТА для угла кадра.

    Единый источник правды для позиционирования: экспорт (drawtext)
    и painter превью считают от него. drawtext позиционирует ТЕКСТ,
    а подложка выступает за него на boxborderw - поэтому координаты
    сдвигаем на pad: внешняя граница подложки ложится ровно на
    margin от края якорного угла. Возврат - выражения drawtext
    (w/h/text_w/text_h резолвит ffmpeg).
    """
    m_x = _clamp_margin(margin_x)
    m_y = _clamp_margin(margin_y)
    pad = _clamp_pad(box_pad)
    mxp, myp = m_x + pad, m_y + pad
    positions = {
        "TL": (f"{mxp}", f"{myp}"),
        "TR": (f"w-text_w-{mxp}", f"{myp}"),
        "BL": (f"{mxp}", f"h-text_h-{myp}"),
        "BR": (f"w-text_w-{mxp}", f"h-text_h-{myp}"),
    }
    return positions.get(corner, positions["BR"])


def overlay_origin(
    corner: str,
    margin_x: int,
    margin_y: int,
    box_pad: int,
    text_w: int,
    text_h: int,
    frame_w: int,
    frame_h: int,
) -> tuple[int, int]:
    """Числовой origin текста в пикселях кадра (для painter превью).

    Та же формула, что overlay_rect: внешняя граница подложки ровно
    на margin от края якорного угла, текст внутри со сдвигом на pad.
    """
    m_x = _clamp_margin(margin_x)
    m_y = _clamp_margin(margin_y)
    pad = _clamp_pad(box_pad)
    mxp, myp = m_x + pad, m_y + pad
    if corner == "TL":
        return (mxp, myp)
    if corner == "TR":
        return (frame_w - text_w - mxp, myp)
    if corner == "BL":
        return (mxp, frame_h - text_h - myp)
    return (frame_w - text_w - mxp, frame_h - text_h - myp)


def build_drawtext_filter(
    text: str,
    corner: str,
    fontsize: int,
    color: str,
    margin_x: int,
    margin_y: int,
    fontfile: str = "",
    font_family: str = "",
    back_color: str = "black",
    back_alpha: int = 50,
    box_pad: int = 6,
    textfile: str = "",
) -> str:
    """Фильтр drawtext для угла кадра.

    margin_x/margin_y - отступ от краев якорного угла до ВНЕШНЕЙ границы
    подложки (как блок в превью): X от левого/правого края по стороне
    угла, Y от верхнего/нижнего. drawtext позиционирует ТЕКСТ, а подложка
    выступает за него на boxborderw - поэтому координаты сдвигаем на pad.
    """
    t = escape_drawtext(text or "")
    x, y = overlay_rect(corner, margin_x, margin_y, box_pad)
    try:
        pad = max(0, min(30, int(box_pad)))
    except (TypeError, ValueError):
        pad = 6
    try:
        alpha = max(0, min(100, int(back_alpha))) / 100.0
    except (TypeError, ValueError):
        alpha = 0.5
    parts = [
        f"textfile={_filter_option(textfile)}" if textfile else f"text='{t}'",
        "expansion=none",
        f"x={x}",
        f"y={y}",
        f"fontsize={int(fontsize)}",
        f"fontcolor={_norm_drawtext_color(color)}",
    ]
    if alpha > 0:
        parts.append(
            f"box=1:boxcolor={_norm_drawtext_color(back_color)}@{alpha:.2f}"
            f":boxborderw={pad}"
        )
    else:
        parts.append("box=0")
    ff = (fontfile or "").strip()
    fam = (font_family or "").strip()
    if fam and _has_fontconfig():
        # предпочтение - выбранному семейству (fontconfig резолвит файл сам)
        parts.insert(0, f"font={_filter_option(fam)}")
    elif ff:
        parts.insert(0, f"fontfile={_filter_option(ff.replace(chr(92), '/'))}")
    return "drawtext=" + ":".join(parts)


def build_drawtext_filters(
    overlays: Sequence[TextOverlay | RenderText | dict[str, Any]],
    fontfile: str = "",
    text_directory: Path | None = None,
) -> str:
    """Цепочка drawtext через запятую для всех включенных текстов.

    overlays: список TextOverlay (или их dict).
    """
    from src.domain.video.models import TextOverlay

    chain: list[str] = []
    for index, it in enumerate(overlays):
        ov = TextOverlay.from_dict(it) if isinstance(it, dict) else it
        if not (ov.enabled and ov.kind == "text" and ov.text):
            continue
        from src.workers.video_editor.font_resolver import VideoFontResolver

        selected_file = getattr(ov, "fontfile", "")
        if selected_file:
            if not Path(selected_file).is_file():
                raise ValueError(
                    "Файл выбранного шрифта недоступен. Перепривяжите шрифт."
                )
        else:
            selected_file = (
                VideoFontResolver.resolve(ov.font_family)
                if ov.font_family
                else fontfile or VideoFontResolver.resolve("")
            )
        textfile = ""
        if text_directory is not None:
            path = text_directory / f"overlay-{index}.txt"
            path.write_text(ov.text, encoding="utf-8")
            textfile = path.as_posix()
        chain.append(
            build_drawtext_filter(
                ov.text,
                ov.corner,
                ov.fontsize,
                ov.color,
                getattr(ov, "margin_x", getattr(ov, "margin", 20)),
                getattr(ov, "margin_y", getattr(ov, "margin", 20)),
                selected_file,
                "",
                getattr(ov, "back_color", "black"),
                getattr(ov, "back_alpha", 50),
                getattr(ov, "box_pad", 6),
                textfile,
            )
        )
        start, end = float(getattr(ov, "start", 0)), float(getattr(ov, "end", 0))
        if start or end:
            enabled = f"gte(t,{start:.9f})" + (f"*lt(t,{end:.9f})" if end else "")
            chain[-1] += f":enable='{enabled}'"
    return ",".join(chain)
