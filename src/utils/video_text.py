"""Общая геометрия текста и прозрачный слой для поворота при экспорте."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter


class TextStyle(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def font_family(self) -> str: ...
    @property
    def fontfile(self) -> str: ...
    @property
    def fontsize(self) -> int: ...
    @property
    def color(self) -> str: ...
    @property
    def back_color(self) -> str: ...
    @property
    def back_alpha(self) -> int: ...
    @property
    def box_pad(self) -> int: ...


_font_lock = RLock()
_families: OrderedDict[str, tuple[int, str]] = OrderedDict()


def text_font(family: str, fontfile: str = "") -> QFont:
    """Загрузить тот же файл шрифта в предпросмотре и рабочем потоке экспорта."""
    if not fontfile and not family.strip():
        from src.workers.video_editor.ffmpeg_utils import default_fontfile

        fontfile = default_fontfile() or ""
    if fontfile:
        with _font_lock:
            cached = _families.get(fontfile)
            if cached is None:
                identifier = QFontDatabase.addApplicationFont(fontfile)
                families = QFontDatabase.applicationFontFamilies(identifier)
                if families:
                    cached = identifier, families[0]
                    _families[fontfile] = cached
                    if len(_families) > 32:
                        _, (expired, _) = _families.popitem(last=False)
                        QFontDatabase.removeApplicationFont(expired)
            if cached is not None:
                _families.move_to_end(fontfile)
                return QFont(cached[1])
    return QFont(family.strip())


@dataclass(frozen=True)
class TextGeometry:
    lines: tuple[str, ...]
    width: int
    ink: QRect
    line_spacing: int


def text_geometry(font: QFont, text: str) -> TextGeometry:
    """Размер строки и границы глифов с учётом многострочного текста."""
    lines = tuple(text.split("\n"))
    metrics = QFontMetrics(font)
    ink = metrics.tightBoundingRect(lines[0])
    for number, line in enumerate(lines[1:], 1):
        ink = ink.united(
            metrics.tightBoundingRect(line).translated(
                0, number * metrics.lineSpacing()
            )
        )
    return TextGeometry(
        lines,
        max(metrics.horizontalAdvance(line) for line in lines),
        ink,
        metrics.lineSpacing(),
    )


def text_layer(item: TextStyle) -> tuple[QImage, TextGeometry, int]:
    """Компактный растр текста с подложкой; припуск сохраняет края глифов."""
    from src.workers.video_editor.font_resolver import VideoFontResolver

    selected = item.fontfile or VideoFontResolver.resolve(item.font_family)
    if not Path(selected).is_file():
        raise ValueError("Файл выбранного шрифта недоступен. Перепривяжите шрифт.")
    font = text_font(item.font_family, selected)
    font.setPixelSize(item.fontsize)
    geometry = text_geometry(font, item.text)
    pad = item.box_pad
    bleed = max(2, -geometry.ink.left(), geometry.ink.right() + 1 - geometry.width)
    width = max(1, geometry.width + 2 * (pad + bleed))
    height = max(1, geometry.ink.height() + 2 * (pad + bleed))
    if width * height > 64_000_000:
        raise ValueError("Размер текстового наложения превышает 64 мегапикселя.")
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    if image.isNull():
        raise ValueError("Недостаточно памяти для текстового наложения.")
    painter = QPainter(image)
    try:
        if item.back_alpha > 0:
            background = QColor(item.back_color)
            if not background.isValid():
                background = QColor("black")
            background.setAlpha(round(item.back_alpha * 255 / 100))
            painter.fillRect(
                bleed,
                bleed,
                geometry.width + 2 * pad,
                geometry.ink.height() + 2 * pad,
                background,
            )
        color = QColor(item.color)
        painter.setFont(font)
        painter.setPen(color if color.isValid() else QColor("white"))
        for number, line in enumerate(geometry.lines):
            painter.drawText(
                QPoint(
                    pad + bleed,
                    pad + bleed - geometry.ink.top() + number * geometry.line_spacing,
                ),
                line,
            )
    finally:
        painter.end()
    return image, geometry, bleed
