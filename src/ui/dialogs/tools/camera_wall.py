"""Панель наблюдения ЕЦХД: сетка, выбор камер и признаки задержки."""

from __future__ import annotations

import time
from math import isqrt
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.models.camera import CameraModel
from src.services.media.camera_presentation import camera_fit_size
from src.ui.dialogs.tools.camera_overlay import paint_camera_overlay
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS
from src.utils.text.time_format import format_duration

if TYPE_CHECKING:
    from src.ui.dialogs.tools.camera_video_output import CameraVideoOutput

CAMERA_FOCUS_LAYOUT = 61
CAMERA_LAYOUTS = {
    count: tuple(
        (index // isqrt(count), index % isqrt(count), 1, 1)
        for index in range(count)
    )
    for count in (1, 4, 9, 16)
}
CAMERA_LAYOUTS[6] = tuple((index // 3, index % 3, 1, 1) for index in range(6))
CAMERA_LAYOUTS[CAMERA_FOCUS_LAYOUT] = (
    (1, 1, 2, 2),
    (0, 0, 1, 1), (0, 1, 1, 1), (0, 2, 1, 1),
    (1, 0, 1, 1), (2, 0, 1, 1),
)
CAMERA_VIEW_MODES = (0, 1, 4, 6, CAMERA_FOCUS_LAYOUT, 9, 16)
CAMERA_GRID_MODES = tuple(mode for mode in CAMERA_VIEW_MODES if mode not in (0, 1))


class CameraAddButton(QToolButton):
    """Симметричный плюс без смещения из-за метрик шрифта."""

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().buttonText().color() if self.underMouse() else QColor("#e5eaf0")
        painter.setPen(QPen(color, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = self.width() / 2, self.height() / 2
        painter.drawLine(QPointF(x - 11, y), QPointF(x + 11, y))
        painter.drawLine(QPointF(x, y - 11), QPointF(x, y + 11))


class CameraSurface(QWidget):
    """Рисовать кадр с сохранением пропорций и явной отметкой зависания."""

    activated = Signal()
    choose_requested = Signal()
    resized = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(80, 45)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmap = QPixmap()
        self._source_size = (1, 1)
        self._message = "Камера не выбрана"
        self._stale = False
        self._video: CameraVideoOutput | None = None
        self._native = False
        self.caption = ""
        self.recording_state = ""
        self.recording_elapsed = ""
        self._empty = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.add_button = CameraAddButton(self)
        self.add_button.setFixedSize(52, 52)
        self.add_button.setStyleSheet(
            FLAT_TOOLBUTTON_QSS
            + "QToolButton { color: #e5eaf0; border: 1px solid #536170; border-radius: 8px; }"
            + "QToolButton:hover { color: palette(button-text); }"
        )
        self.add_button.setToolTip("Добавить камеру")
        self.add_button.setAccessibleName("Добавить камеру")
        self.add_button.clicked.connect(self.choose_requested)
        layout.addWidget(self.add_button, alignment=Qt.AlignmentFlag.AlignCenter)

    def set_camera(self, camera: CameraModel | None) -> None:
        self._empty = camera is None
        self.caption = camera.caption if camera else ""
        self.recording_state = ""
        self.recording_elapsed = ""
        self.add_button.setVisible(self._empty)
        self.clear("Подключение…" if camera else "")
        self.setToolTip(self.caption if camera else "Добавить камеру")

    def set_recording(self, state: str, elapsed: str = "") -> None:
        self.recording_state = state
        self.recording_elapsed = elapsed
        self._sync_video_overlay()
        if not self._native:
            self.update()

    def video_output(self, enabled: bool) -> CameraVideoOutput | None:
        """Вернуть видеовыход с подписями поверх кадра на всей площади плитки."""
        self._native = enabled
        if enabled and self._video is None:
            from src.ui.dialogs.tools.camera_video_output import CameraVideoOutput

            self._video = CameraVideoOutput(self)
            self._video.hide()
        if self._video is not None:
            if not enabled:
                self._video.hide()
            self._layout_video()
            self._sync_video_overlay()
        return self._video if enabled else None

    def _layout_video(self) -> None:
        if self._video is None:
            return
        self._video.setGeometry(self.rect())

    def _recording_text(self) -> str:
        if not self.recording_state or self.recording_state == "Ожидание":
            return ""
        return (
            f"{self.recording_state} • {self.recording_elapsed}"
            if self.recording_elapsed else self.recording_state
        )

    def _sync_video_overlay(self) -> None:
        if self._video is None:
            return
        self._video.set_overlay(
            caption=self.caption,
            recordingText=self._recording_text(),
            recordingActive=self.recording_state == "Запись",
            stale=self._stale,
            statusMessage=self._message,
            font=self.font(),
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._layout_video()
        self._sync_video_overlay()
        self.resized.emit()

    def clear(self, message: str) -> None:
        self._pixmap = QPixmap()
        self._message = message
        self._stale = False
        if self._video is not None:
            self._video.hide()
            self._sync_video_overlay()
        self.update()

    def set_frame(self, image: QImage | None, message: str, stale: bool) -> None:
        if image is not None:
            self._pixmap = QPixmap.fromImage(image)
            source = image.text("camera.source_size")
            self._source_size = tuple(map(int, source.split("x"))) if source else (image.width(), image.height())
        self._message = message
        self._stale = stale
        if self._video is not None:
            self._sync_video_overlay()
            self._video.setVisible(self._native and not self._empty)
        if not self._native:
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._native and self._video is not None and self._video.isVisible():
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#10151c"))
        if self._empty:
            return
        if not self._pixmap.isNull():
            dpr = self.devicePixelRatioF()
            width, height = camera_fit_size(
                self._source_size,
                (round(self.width() * dpr), round(self.height() * dpr)),
            )
            target = QRect(0, 0, round(width / dpr), round(height / dpr))
            target.moveCenter(self.rect().center())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setOpacity(0.4 if self._stale else 1.0)
            painter.drawPixmap(target, self._pixmap)
            painter.setOpacity(1.0)
        painter.setFont(self.font())
        paint_camera_overlay(
            painter, self.rect(), self.caption,
            recording_text=self._recording_text(),
            recording=self.recording_state == "Запись",
            message=self._message if self._pixmap.isNull() or self._stale else "",
        )

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._empty:
            self.activated.emit()
        super().mouseDoubleClickEvent(event)


class CameraTile(QFrame):
    choose_requested = Signal(int)
    activated = Signal(str)
    menu_requested = Signal(int, QPoint)

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.iid = ""
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumSize(90, 65)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.surface = CameraSurface(self)
        self.surface.choose_requested.connect(
            lambda: self.choose_requested.emit(self.index)
        )
        self.surface.setToolTip(
            "Двойной щелчок — развернуть камеру / вернуться к сетке"
        )
        self.surface.activated.connect(lambda: self.activated.emit(self.iid))
        self.surface.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.surface.customContextMenuRequested.connect(
            lambda pos: self.menu_requested.emit(
                self.index, self.surface.mapToGlobal(pos)
            )
        )
        layout.addWidget(self.surface, 1)

    def configure(self, cameras: list[CameraModel], iid: str) -> None:
        self.iid = iid
        self.surface.set_camera(
            next((camera for camera in cameras if camera.identifier == iid), None)
        )


class CameraWall(QWidget):
    visible_changed = Signal()
    presets_changed = Signal()
    focus_requested = Signal(str)
    choose_requested = Signal(int)
    menu_requested = Signal(int, QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = 4
        self.count = 4
        self.page = 0
        self.cameras: list[CameraModel] = []
        self._tiles: list[CameraTile] = []
        self._assignments: dict[tuple[int, int], list[str]] = {}
        self._pages: dict[int, int] = {}
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self.visible_changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._grid = QGridLayout()
        self._grid.setSpacing(4)
        layout.addLayout(self._grid, 1)

    def restore_presets(self, presets: object, pages: object = None) -> None:
        """Восстановить независимые раскладки, игнорируя повреждённые записи."""
        self._assignments.clear()
        self._pages.clear()
        if isinstance(presets, dict):
            for mode, cells_layout in CAMERA_LAYOUTS.items():
                count = len(cells_layout)
                saved = presets.get(str(mode), {})
                if not isinstance(saved, dict):
                    continue
                for page, cells in saved.items():
                    if (
                        not isinstance(page, str)
                        or not page.isdecimal()
                        or len(page) > 5
                        or int(page) > 65535
                        or not isinstance(cells, list)
                    ):
                        continue
                    self._assignments[(mode, int(page))] = [
                        value if isinstance(value, str) else ""
                        for value in (cells[:count] + [""] * count)[:count]
                    ]
            seen: dict[int, set[str]] = {}
            for (mode, page), cells in sorted(self._assignments.items()):
                occupied = seen.setdefault(mode, set())
                for index, iid in enumerate(cells):
                    if iid in occupied:
                        cells[index] = ""
                    elif iid:
                        occupied.add(iid)
        if isinstance(pages, dict):
            for mode in CAMERA_LAYOUTS:
                page = pages.get(str(mode), 0)
                if type(page) is int and 0 <= page <= 65535:
                    self._pages[mode] = page

    def presets(self) -> dict[str, dict[str, list[str]]]:
        """Вернуть независимый снимок ячеек без моделей и данных подключения."""
        result: dict[str, dict[str, list[str]]] = {
            str(mode): {} for mode in CAMERA_LAYOUTS
        }
        for (count, page), cells in self._assignments.items():
            result[str(count)][str(page)] = list(cells)
        return result

    def pages(self) -> dict[str, int]:
        return {str(count): page for count, page in self._pages.items()}

    def set_single_camera(self, iid: str) -> None:
        """Выбранная в дереве или развёрнутая камера занимает первый пресет."""
        changed = self._assignments.get((1, 0)) != [iid]
        for (mode, page), cells in self._assignments.items():
            if mode == 1 and page != 0:
                cells[:] = ["" if value == iid else value for value in cells]
        self._assignments[(1, 0)] = [iid]
        self._pages[1] = 0
        if self.mode == 1:
            self.page = 0
            self._rebuild()
        if changed:
            self.presets_changed.emit()

    def configure(
        self, cameras: list[CameraModel], mode: int, anchor: str = ""
    ) -> None:
        self.cameras = cameras
        self.mode = mode
        self.count = len(CAMERA_LAYOUTS[mode])
        self.page = min(self._pages.get(mode, 0), self._page_count(mode) - 1)
        self._pages[mode] = self.page
        if anchor and mode == 1:
            self.set_single_camera(anchor)
            return
        self._rebuild()

    def _rebuild(self) -> None:
        for tile in self._tiles:
            self._grid.removeWidget(tile)
            tile.hide()
            tile.deleteLater()
        self._tiles.clear()
        for i in range(4):
            self._grid.setRowStretch(i, 0)
            self._grid.setColumnStretch(i, 0)
        cells_layout = CAMERA_LAYOUTS[self.mode]
        key = (self.mode, self.page)
        assignments = self._assignments.setdefault(key, [""] * self.count)
        valid = {camera.identifier for camera in self.cameras}
        assignments[:] = [iid if iid in valid else "" for iid in assignments]
        for index, (row, column, row_span, column_span) in enumerate(cells_layout):
            tile = CameraTile(index, self)
            tile.configure(self.cameras, assignments[index])
            tile.choose_requested.connect(self.choose_requested)
            tile.activated.connect(self.focus_requested)
            tile.menu_requested.connect(self.menu_requested)
            tile.surface.resized.connect(self._resize_timer.start)
            self._grid.addWidget(tile, row, column, row_span, column_span)
            self._tiles.append(tile)
        for i in range(max(row + span for row, _, span, _ in cells_layout)):
            self._grid.setRowStretch(i, 1)
        for i in range(max(column + span for _, column, _, span in cells_layout)):
            self._grid.setColumnStretch(i, 1)
        self.visible_changed.emit()
        # Новые плитки получают окончательную геометрию после обработки layout.
        self._resize_timer.start()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._resize_timer.start()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.DevicePixelRatioChange and hasattr(self, "_resize_timer"):
            self._resize_timer.start()
        return super().event(event)

    def preview_sizes(self, maximum: tuple[int, int]) -> dict[str, tuple[int, int]]:
        """Отдельный физический предел каждой плитки, включая смешанную сетку."""
        return {
            tile.iid: (
                min(maximum[0], max(1, round(tile.surface.width() * tile.surface.devicePixelRatioF()))),
                min(maximum[1], max(1, round(tile.surface.height() * tile.surface.devicePixelRatioF()))),
            )
            for tile in self._tiles if tile.iid
        }

    def _page_count(self, mode: int) -> int:
        count = len(CAMERA_LAYOUTS[mode])
        valid = {camera.identifier for camera in self.cameras}
        occupied = max(
            (
                page + 1
                for (saved_mode, page), cells in self._assignments.items()
                if saved_mode == mode and any(iid in valid for iid in cells)
            ),
            default=1,
        )
        return max(occupied, (len(self.cameras) + count - 1) // count, 1)

    def can_move(self, delta: int) -> bool:
        return 0 <= self.page + delta < self._page_count(self.mode)

    def _move(self, delta: int) -> None:
        if not self.can_move(delta):
            return
        self.page += delta
        self._pages[self.mode] = self.page
        self._rebuild()

    def available_cameras(self, index: int) -> list[CameraModel]:
        """Камеры, не занятые в других ячейках и страницах этой раскладки."""
        occupied = {
            iid
            for (mode, page), cells in self._assignments.items()
            if mode == self.mode
            for slot, iid in enumerate(cells)
            if iid and (page != self.page or slot != index)
        }
        return [camera for camera in self.cameras if camera.identifier not in occupied]

    def _choose(self, index: int, iid: str) -> None:
        if not 0 <= index < len(self._tiles):
            return
        if iid and not any(camera.identifier == iid for camera in self.available_cameras(index)):
            return
        tile = self._tiles[index]
        tile.configure(self.cameras, iid)
        self._assignments[(self.mode, self.page)][index] = iid
        self.visible_changed.emit()

    def visible_cameras(self) -> list[CameraModel]:
        ids = {tile.iid for tile in self._tiles if tile.iid}
        return [camera for camera in self.cameras if camera.identifier in ids]

    def video_outputs(self, backend: str) -> dict[str, CameraVideoOutput]:
        """Подготовить видеовыходы только для выбранных видимых камер."""
        outputs = {}
        for tile in self._tiles:
            output = tile.surface.video_output(backend == "qt" and bool(tile.iid))
            if output is not None:
                outputs[tile.iid] = output
        return outputs

    def update_frame(
        self, iid: str, image: QImage | None, received: float, state: str
    ) -> None:
        age = max(0.0, time.monotonic() - received) if received else 0.0
        stale = bool(received and (age > 3 or state != "Прямой эфир"))
        message = f"Нет новых кадров: {format_duration(age, milliseconds=False)}\n{state}" if stale else state
        for tile in self._tiles:
            if tile.iid == iid:
                if not received:
                    tile.surface.clear(state)
                else:
                    tile.surface.set_frame(image, message, stale)
                tile.surface.setToolTip(
                    tile.surface.caption
                    + "\n"
                    + message
                    + "\nДвойной щелчок — развернуть / вернуться\nПравая кнопка — управление камерой"
                )

    def update_recording(
        self, iid: str, state: str, active: bool, elapsed: str = ""
    ) -> None:
        for tile in self._tiles:
            if tile.iid == iid:
                tile.surface.set_recording(state, elapsed)
