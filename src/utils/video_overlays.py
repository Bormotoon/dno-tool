"""Общий растр картинок и фигур для предпросмотра и FFmpeg-экспорта."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import RLock
from typing import Protocol

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QImageReader,
    QPainter,
    QPen,
    QPolygonF,
    QTransform,
)


class GraphicOverlay(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def image_path(self) -> str: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...
    @property
    def opacity(self) -> int: ...
    @property
    def stroke_width(self) -> int: ...
    @property
    def filled(self) -> bool: ...
    @property
    def color(self) -> str: ...
    @property
    def corner(self) -> str: ...
    @property
    def margin_x(self) -> int: ...
    @property
    def margin_y(self) -> int: ...
    @property
    def arrow_direction(self) -> str: ...


_cache: OrderedDict[tuple, QImage] = OrderedDict()
_cache_lock = RLock()
_CACHE_BYTES = 32 * 1024 * 1024


def rotate_layer(image: QImage, angle: int) -> QImage:
    """Повернуть прозрачный слой без обрезки, сохранив его центр."""
    if angle % 360 == 0:
        return image
    return image.transformed(
        QTransform().rotate(angle), Qt.TransformationMode.SmoothTransformation
    )


def image_size(path: str) -> QSize:
    """Проверить формат и площадь до декодирования исходной картинки."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if not reader.canRead() or not size.isValid():
        raise ValueError(
            "Не удалось прочитать картинку. Выберите PNG, JPEG, WebP или BMP."
        )
    if size.width() * size.height() > 64_000_000:
        raise ValueError("Картинка превышает 64 мегапикселя.")
    if reader.transformation().value in (4, 5, 6, 7):
        size.transpose()
    return size


def graphic_origin(item: GraphicOverlay, width: int, height: int) -> tuple[int, int]:
    """Отступ относится к внешней границе картинки или фигуры."""
    return (
        (
            width - item.margin_x - item.width
            if item.corner.endswith("R")
            else item.margin_x
        ),
        (
            height - item.margin_y - item.height
            if item.corner.startswith("B")
            else item.margin_y
        ),
    )


def graphic_key(item: GraphicOverlay) -> tuple:
    """Ключ включает версию файла, чтобы замена картинки обновляла растр."""
    fingerprint: tuple = ()
    if item.kind == "image":
        try:
            stat = Path(item.image_path).stat()
            fingerprint = (item.image_path, stat.st_mtime_ns, stat.st_size)
        except OSError as error:
            raise ValueError(
                "Картинка недоступна. Выберите файл заново в панели наложений."
            ) from error
    return (
        item.kind,
        fingerprint,
        item.width,
        item.height,
        item.opacity,
        item.stroke_width,
        item.filled,
        item.color,
        item.arrow_direction,
    )


def cached_graphic(key: tuple) -> QImage | None:
    """Только чтение кэша; декодирование здесь не выполняется."""
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def graphic_image(item: GraphicOverlay) -> QImage:
    """Создать прозрачный растр элемента с ограниченным общим кэшем."""
    key = graphic_key(item)
    cached = cached_graphic(key)
    if cached is not None:
        return cached
    image = QImage(item.width, item.height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    if image.isNull():
        raise ValueError("Недостаточно памяти для наложения.")
    source = QImage()
    if item.kind == "image":
        image_size(item.image_path)
        reader = QImageReader(item.image_path)
        reader.setAutoTransform(True)
        target_size = QSize(item.width, item.height)
        if reader.transformation().value in (4, 5, 6, 7):
            target_size.transpose()
        reader.setScaledSize(target_size)
        source = reader.read()
        if source.isNull():
            raise ValueError("Не удалось декодировать картинку. Выберите другой файл.")
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(item.opacity / 100)
        if item.kind == "image":
            painter.drawImage(QRectF(0, 0, item.width, item.height), source)
        else:
            color = QColor(item.color)
            if not color.isValid():
                raise ValueError("Укажите корректный цвет фигуры.")
            stroke = min(item.stroke_width, item.width, item.height)
            painter.setPen(
                QPen(
                    color,
                    stroke,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(color if item.filled else Qt.BrushStyle.NoBrush)
            rect = QRectF(
                stroke / 2, stroke / 2, item.width - stroke, item.height - stroke
            )
            if item.kind == "rectangle":
                painter.drawRect(rect)
            elif item.kind == "ellipse":
                painter.drawEllipse(rect)
            elif item.kind == "arrow":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                w, h = item.width, item.height
                if item.arrow_direction == "left":
                    painter.translate(w, h)
                    painter.rotate(180)
                elif item.arrow_direction == "down":
                    painter.translate(w, 0)
                    painter.rotate(90)
                    w, h = h, w
                elif item.arrow_direction == "up":
                    painter.translate(0, h)
                    painter.rotate(-90)
                    w, h = h, w
                shaft = min(stroke, h / 3)
                head = min(w * 0.4, h)
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(0, (h - shaft) / 2),
                            QPointF(w - head, (h - shaft) / 2),
                            QPointF(w - head, 0),
                            QPointF(w, h / 2),
                            QPointF(w - head, h),
                            QPointF(w - head, (h + shaft) / 2),
                            QPointF(0, (h + shaft) / 2),
                        ]
                    )
                )
            else:
                raise ValueError("Неизвестная фигура.")
    finally:
        painter.end()
    with _cache_lock:
        if image.sizeInBytes() <= _CACHE_BYTES:
            _cache[key] = image
            while sum(entry.sizeInBytes() for entry in _cache.values()) > _CACHE_BYTES:
                _cache.popitem(last=False)
    return image
