"""Дисплей трекера: декоративная развёртка и отметки ответивших хостов."""

import hashlib
import math
import random
import time
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QHideEvent,
    QImage,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
    QShowEvent,
)
from PySide6.QtWidgets import QWidget

_SCREEN_SIZE = 320
_CENTER_X = 160.0
_CENTER_Y = 249.0
_RANGE = 214.0
_PERIOD = 0.9
_INK = "#91ff7c"


class MotionTracker(QWidget):
    """Лёгкая анимация в GUI-потоке; сетевых операций и звука здесь нет."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(104, 104)
        self.setAccessibleName("Трекер сканирования")
        self.setToolTip(
            "Трекер сканирования · число найденных хостов\n"
            "Отметки соответствуют ответам хостов. Положение условное, не расстояние."
        )
        self._active = False
        self._started = 0.0
        self._elapsed = 0.0
        self._count = 0
        self._contacts: OrderedDict[str, tuple[float, float, float]] = OrderedDict()
        self._random = random.Random()
        self._section_flashes = [(-math.inf, 1.0) for _ in range(4)]
        self._next_section_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._background = self._make_background()
        self._overlay = self._make_overlay()
        self._section_images = tuple(self._make_section_image(i) for i in range(4))
        self._contact_image = self._make_contact_image()

    def sizeHint(self) -> QSize:
        return QSize(104, 104)

    def start(self) -> None:
        """Новая сессия: сброс предыдущих отметок и запуск развёртки."""
        self.reset()
        self._started = time.monotonic()
        self._active = True
        self._advance_sections(0.0)
        if self.isVisible():
            self._timer.start()
        self.update()

    def stop(self) -> None:
        """Заморозить последний кадр без фонового таймера."""
        if self._active:
            self._elapsed = time.monotonic() - self._started
        self._active = False
        self._timer.stop()
        self.update()

    def reset(self) -> None:
        self.stop()
        self._elapsed = 0.0
        self._count = 0
        self._contacts.clear()
        self._section_flashes = [(-math.inf, 1.0) for _ in range(4)]
        self._next_section_at = 0.0
        self.update()

    def _tick(self) -> None:
        """Менять случайное состояние только по таймеру, а не при перерисовке."""
        if self._active:
            self._advance_sections(time.monotonic() - self._started)
            self.update()

    def _advance_sections(self, elapsed: float) -> None:
        if elapsed < self._next_section_at:
            return
        index = self._random.randrange(4)
        self._section_flashes[index] = (elapsed, self._random.uniform(0.65, 1.3))
        self._next_section_at = elapsed + self._random.uniform(0.48, 1.18)

    def _section_strength(self, index: int, elapsed: float) -> float:
        started, duration = self._section_flashes[index]
        age = elapsed - started
        if age < 0 or age >= duration:
            return 0.0
        return min(1.0, age / 0.065) * (1 - age / duration) ** 0.65

    def set_count(self, count: int) -> None:
        self._count = max(0, count)
        self.update()

    def add_contact(self, host: str) -> None:
        """Стабильная декоративная позиция; память ограничена 48 отметками."""
        if not self._active:
            return
        if host in self._contacts:
            self._contacts.move_to_end(host)
            return
        digest = hashlib.sha256(host.encode("utf-8")).digest()
        angle = math.radians(-35 + digest[0] / 255 * 70)
        radius = _RANGE * (0.20 + digest[1] / 255 * 0.74)
        self._contacts[host] = (
            angle,
            radius,
            time.monotonic() - self._started,
        )
        self._contacts.move_to_end(host)
        while len(self._contacts) > 48:
            self._contacts.popitem(last=False)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._active:
            self._timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    @staticmethod
    def _point(angle: float, radius: float) -> QPointF:
        return QPointF(
            _CENTER_X + math.sin(angle) * radius, _CENTER_Y - math.cos(angle) * radius
        )

    @staticmethod
    def _arc_rect(radius: float) -> QRectF:
        return QRectF(_CENTER_X - radius, _CENTER_Y - radius, radius * 2, radius * 2)

    @staticmethod
    def _screen_clip() -> QPainterPath:
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, _SCREEN_SIZE, _SCREEN_SIZE), 21, 21)
        return clip

    @staticmethod
    def _draw_glowing_path(painter: QPainter, path: QPainterPath, width: float) -> None:
        """Мягкое свечение шкал сохраняется в статическом слое."""
        for extra, alpha in ((6, 8), (3, 16), (1.2, 28), (0, 166)):
            color = QColor(_INK)
            color.setAlpha(alpha)
            painter.setPen(QPen(color, width + extra))
            painter.drawPath(path)

    @staticmethod
    def _make_background() -> QPixmap:
        """Широкий веер, двойная дуга и служебные поля киноэкрана."""
        image = QPixmap(_SCREEN_SIZE * 2, _SCREEN_SIZE * 2)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2, 2)
        painter.setClipPath(MotionTracker._screen_clip())
        background = QRadialGradient(160, 164, 227, 154, 180, 18)
        background.setColorAt(0, QColor("#123c14"))
        background.setColorAt(0.62, QColor("#0a220c"))
        background.setColorAt(1, QColor("#020603"))
        painter.fillRect(QRectF(0, 0, _SCREEN_SIZE, _SCREEN_SIZE), background)
        for radius, width in ((_RANGE, 1.9), (_RANGE - 7, 1.65)):
            path = QPainterPath()
            path.arcMoveTo(MotionTracker._arc_rect(radius), 45)
            path.arcTo(MotionTracker._arc_rect(radius), 45, 90)
            MotionTracker._draw_glowing_path(painter, path, width)
        for fraction in (0.25, 0.50, 0.75):
            pen = QPen(QColor(145, 255, 124, 166), 1.65)
            pen.setDashPattern([6 / 1.65, 5 / 1.65])
            painter.setPen(pen)
            painter.drawArc(
                MotionTracker._arc_rect(_RANGE * fraction), 45 * 16, 90 * 16
            )
        for degrees in (-45, -22.5, 0, 22.5, 45):
            path = QPainterPath(QPointF(_CENTER_X, _CENTER_Y))
            path.lineTo(MotionTracker._point(math.radians(degrees), _RANGE))
            MotionTracker._draw_glowing_path(painter, path, 1.65)
        painter.setPen(QPen(QColor(145, 255, 124, 166), 0.8))
        painter.drawLine(QPointF(_CENTER_X, _CENTER_Y), QPointF(_CENTER_X, 22))
        marker = QPainterPath(QPointF(157, 19))
        marker.lineTo(163, 19)
        marker.lineTo(160, 25)
        marker.closeSubpath()
        painter.fillPath(marker, QColor(145, 255, 124, 166))
        painter.setPen(QPen(QColor(50, 122, 53, 138), 0.7))
        painter.drawRoundedRect(QRectF(22, 24, 276, 277), 17, 17)
        painter.fillRect(QRectF(27, 243, 35, 17), QColor(145, 255, 124, 117))
        painter.fillRect(QRectF(27, 265, 35, 17), QColor(145, 255, 124, 51))
        font = QFont("Consolas")
        font.setPixelSize(17)
        painter.setFont(font)
        painter.setPen(QColor("#031006"))
        painter.drawText(QRectF(27, 242, 31, 18), Qt.AlignmentFlag.AlignRight, "1")
        painter.setPen(QColor(145, 255, 124, 178))
        painter.drawText(QRectF(27, 264, 31, 18), Qt.AlignmentFlag.AlignRight, "2")
        font.setPixelSize(14)
        painter.setFont(font)
        for index, label in enumerate(("ATT", "SUS", "DEC")):
            painter.drawText(QPointF(266, 259 + index * 16), label)
        font.setPixelSize(8)
        painter.setFont(font)
        painter.setPen(QColor(145, 255, 124, 153))
        painter.drawText(QPointF(26, 237), "HOSTS")
        painter.end()
        return image

    @staticmethod
    def _make_overlay() -> QPixmap:
        """Неподвижная CRT-текстура и виньетка поверх всех светящихся элементов."""
        image = QPixmap(_SCREEN_SIZE * 2, _SCREEN_SIZE * 2)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.scale(2, 2)
        painter.setClipPath(MotionTracker._screen_clip())
        noise = random.Random(721032).randbytes(_SCREEN_SIZE * _SCREEN_SIZE)
        texture = QImage(
            noise,
            _SCREEN_SIZE,
            _SCREEN_SIZE,
            _SCREEN_SIZE,
            QImage.Format.Format_Grayscale8,
        )
        painter.setOpacity(0.025)
        painter.drawImage(QPointF(0, 0), texture)
        painter.setOpacity(1)
        painter.setPen(QPen(QColor(0, 0, 0, 41), 0.7))
        for y in range(1, _SCREEN_SIZE, 2):
            painter.drawLine(QPointF(0, y), QPointF(_SCREEN_SIZE, y))
        vignette = QRadialGradient(160, 160, 210, 160, 153, 91)
        vignette.setColorAt(0, QColor(0, 0, 0, 0))
        vignette.setColorAt(1, QColor(0, 0, 0, 133))
        painter.fillRect(QRectF(0, 0, _SCREEN_SIZE, _SCREEN_SIZE), vignette)
        painter.end()
        return image

    @staticmethod
    def _section_path(index: int) -> QPainterPath:
        """Четверть кольца имеет фиксированный угол и небольшой зазор по краям."""
        gap = math.degrees(0.035)
        start = 45 - index * 90 - gap
        span = -90 + gap * 2
        path = QPainterPath()
        path.arcMoveTo(MotionTracker._arc_rect(48), start)
        path.arcTo(MotionTracker._arc_rect(48), start, span)
        path.arcTo(MotionTracker._arc_rect(28), start + span, -span)
        path.closeSubpath()
        return path

    @staticmethod
    def _make_section_image(index: int) -> QPixmap:
        image = QPixmap(240, 240)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2, 2)
        painter.translate(60 - _CENTER_X, 60 - _CENTER_Y)
        path = MotionTracker._section_path(index)
        for width, alpha in ((12, 4), (8, 8), (4, 18)):
            painter.setPen(QPen(QColor(145, 255, 124, alpha), width))
            painter.drawPath(path)
        painter.fillPath(path, QColor(_INK))
        painter.end()
        return image

    @staticmethod
    def _make_contact_image() -> QPixmap:
        image = QPixmap(96, 96)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2, 2)
        halo = QRadialGradient(24, 24, 17.4)
        halo.setColorAt(0, QColor(112, 255, 87, 130))
        halo.setColorAt(0.35, QColor(112, 255, 87, 70))
        halo.setColorAt(1, QColor(80, 255, 65, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QPointF(24, 24), 17.4, 17.4)
        painter.setBrush(QColor(_INK))
        painter.drawEllipse(QPointF(24, 24), 5.8, 5.8)
        painter.end()
        return image

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.scale(self.width() / _SCREEN_SIZE, self.height() / _SCREEN_SIZE)
        painter.setClipPath(self._screen_clip())
        screen = QRectF(0, 0, _SCREEN_SIZE, _SCREEN_SIZE)
        painter.drawPixmap(screen, self._background, QRectF(self._background.rect()))
        elapsed = time.monotonic() - self._started if self._active else self._elapsed
        sweep_radius = (elapsed % _PERIOD) / _PERIOD * _RANGE
        if self._active or self._elapsed > 0:
            # Дуга расходится по дальности; затухающий след остаётся внутри неё.
            phase = (elapsed % _PERIOD) / _PERIOD
            fade = min(1.0, phase * 14, (1 - phase) * 10)
            for tail in range(16, -1, -2):
                radius = sweep_radius - tail
                if radius <= 0:
                    continue
                painter.setPen(
                    QPen(QColor(145, 255, 124, int((1 - tail / 18) * 22 * fade)), 1.6)
                )
                painter.drawArc(self._arc_rect(radius), 45 * 16, 90 * 16)
            painter.setPen(QPen(QColor(186, 255, 154, int(122 * fade)), 1.05))
            painter.drawArc(self._arc_rect(sweep_radius), 45 * 16, 90 * 16)
        # Углы четырёх секций постоянны; случайно меняются только их яркости.
        for index, image in enumerate(self._section_images):
            painter.setOpacity(0.035 + self._section_strength(index, elapsed) * 0.74)
            painter.drawPixmap(
                QRectF(_CENTER_X - 60, _CENTER_Y - 60, 120, 120),
                image,
                QRectF(image.rect()),
            )
        for angle, radius, appeared in self._contacts.values():
            # Новый контакт ждёт первого прохода дуги после получения ответа.
            crossing = radius / _RANGE * _PERIOD
            first_pass = math.floor(appeared / _PERIOD) * _PERIOD + crossing
            if first_pass < appeared:
                first_pass += _PERIOD
            if elapsed < first_pass:
                continue
            age = (elapsed - first_pass) % _PERIOD
            strength = 0.14 + 0.86 * math.exp(-age / _PERIOD * 4.8)
            point = self._point(angle, radius)
            painter.setOpacity(strength)
            painter.drawPixmap(
                QRectF(point.x() - 24, point.y() - 24, 48, 48),
                self._contact_image,
                QRectF(self._contact_image.rect()),
            )
        painter.setOpacity(1)
        painter.drawPixmap(screen, self._overlay, QRectF(self._overlay.rect()))
        self._draw_counter(painter)
        painter.end()

    def _draw_counter(self, painter: QPainter) -> None:
        # Сегментные цифры сохраняют характер дисплея, но показывают число хостов.
        digits = str(self._count).zfill(3)
        painter.save()
        painter.translate(25, 290)
        scale = 0.8 * min(24 / 16, 82 / (len(digits) * 14))
        painter.scale(scale, scale)
        segments = (
            "abcdef",
            "bc",
            "abged",
            "abgcd",
            "fgbc",
            "afgcd",
            "afgecd",
            "abc",
            "abcdefg",
            "abfgcd",
        )
        lines = {
            "a": (2, 0, 9, 0),
            "b": (10, 1, 10, 7),
            "c": (10, 9, 10, 15),
            "d": (2, 16, 9, 16),
            "e": (1, 9, 1, 15),
            "f": (1, 1, 1, 7),
            "g": (2, 8, 9, 8),
        }
        for index, digit in enumerate(digits):
            for segment, coords in lines.items():
                lit = segment in segments[int(digit)]
                x1, y1, x2, y2 = coords
                start = QPointF(index * 14 + x1, y1)
                end = QPointF(index * 14 + x2, y2)
                if lit:
                    painter.setPen(QPen(QColor(145, 255, 124, 65), 3.5))
                    painter.drawLine(start, end)
                painter.setPen(
                    QPen(
                        QColor("#c2ffb0") if lit else QColor("#102c16"),
                        1.6,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.SquareCap,
                    )
                )
                painter.drawLine(start, end)
        painter.restore()
