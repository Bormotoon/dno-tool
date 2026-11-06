"""Общая палитра и отрисовка подписей камеры поверх видео."""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter

OVERLAY_LEFT = "#dc0c1118"
OVERLAY_MIDDLE = "#14495b73"
OVERLAY_RIGHT = "#00495b73"
OVERLAY_FADE_POSITION = 0.5
OVERLAY_END_POSITION = 0.75
OVERLAY_TEXT = "#e5eaf0"
OVERLAY_RECORDING = "#ff6969"
OVERLAY_OUTLINE = "#b0000000"
OVERLAY_MARGIN = 0
OVERLAY_PADDING = 8


def paint_camera_label(
    painter: QPainter, rect: QRect, text: str, *, recording: bool = False
) -> None:
    """Нарисовать компактную подпись с горизонтальной градиентной подложкой."""
    gradient = QLinearGradient(rect.left(), 0, rect.right() + 1, 0)
    gradient.setColorAt(0, QColor(OVERLAY_LEFT))
    gradient.setColorAt(OVERLAY_FADE_POSITION, QColor(OVERLAY_MIDDLE))
    gradient.setColorAt(OVERLAY_END_POSITION, QColor(OVERLAY_RIGHT))
    gradient.setColorAt(1, QColor(OVERLAY_RIGHT))
    painter.fillRect(rect, gradient)
    text_rect = rect.adjusted(OVERLAY_PADDING, 0, -OVERLAY_PADDING, 0)
    alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    elided = painter.fontMetrics().elidedText(
        text, Qt.TextElideMode.ElideRight,
        max(0, rect.width() - OVERLAY_PADDING * 2),
    )
    # Тонкий контур сохраняет читаемость текста над светлыми участками кадра.
    painter.setPen(QColor(OVERLAY_OUTLINE))
    for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
        painter.drawText(text_rect.translated(dx, dy), alignment, elided)
    painter.setPen(QColor(OVERLAY_RECORDING if recording else OVERLAY_TEXT))
    painter.drawText(text_rect, alignment, elided)


def paint_camera_overlay(
    painter: QPainter, rect: QRect, caption: str, *, recording_text: str = "",
    recording: bool = False, message: str = "",
) -> None:
    """Наложить одинаковые подписи на видео Qt и на готовый кадр FFmpeg."""
    if message:
        painter.setPen(QColor(OVERLAY_TEXT))
        painter.drawText(
            rect.adjusted(8, 8, -8, -8),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, message,
        )
    height = painter.fontMetrics().height() + 10
    if caption:
        paint_camera_label(
            painter, QRect(rect.left(), rect.top(), rect.width(), height), caption,
        )
    if recording_text:
        paint_camera_label(
            painter,
            QRect(rect.left(), max(rect.top(), rect.bottom() + 1 - height), rect.width(), height),
            recording_text, recording=recording,
        )
