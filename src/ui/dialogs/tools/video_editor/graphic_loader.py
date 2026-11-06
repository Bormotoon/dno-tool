"""Декодирование картинок вне GUI с ограничением числа ожидающих задач."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import replace

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage

from src.domain.video.models import TextOverlay
from src.utils.system.pool_registry import QtPoolSnapshot, register_pool
from src.utils.video_overlays import cached_graphic, graphic_image, graphic_key


class _Signals(QObject):
    done = Signal(object)


class _RasterTask(QRunnable):
    def __init__(self, key: tuple, item: TextOverlay) -> None:
        super().__init__()
        self.key = key
        self.item = replace(item)
        self.signals = _Signals()

    def run(self) -> None:
        error = ""
        try:
            graphic_image(self.item)
        except Exception as exception:  # noqa: BLE001 — граница фоновой задачи
            error = str(exception)
        self.signals.done.emit((self.key, error))


class GraphicLoader(QObject):
    ready = Signal()
    _pool: QThreadPool | None = None

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        if GraphicLoader._pool is None:
            GraphicLoader._pool = QThreadPool()
            GraphicLoader._pool.setMaxThreadCount(2)
            register_pool(QtPoolSnapshot(GraphicLoader._pool, "Растеризация наложений"))
        self._pending: dict[tuple, _RasterTask] = {}
        self._errors: OrderedDict[tuple, str] = OrderedDict()
        self._maximum = 1024

    def set_count(self, count: int) -> None:
        """Все активные наложения делят бюджет растра предпросмотра 8 МиБ."""
        self._maximum = max(1, min(1024, math.isqrt(2 * 1024 * 1024 // max(1, count))))

    def image(self, item: TextOverlay) -> QImage | None:
        scale = min(1, self._maximum / max(item.width, item.height))
        if scale < 1:
            item = replace(
                item,
                width=max(1, round(item.width * scale)),
                height=max(1, round(item.height * scale)),
                stroke_width=max(1, round(item.stroke_width * scale)),
            )
        key = graphic_key(item)
        image = cached_graphic(key)
        if image is not None:
            return image
        if key in self._errors:
            raise ValueError(self._errors[key])
        if key not in self._pending and len(self._pending) < 8:
            task = _RasterTask(key, item)
            task.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
            self._pending[key] = task
            GraphicLoader._pool.start(task)
        return None

    @Slot(object)
    def _done(self, result: tuple) -> None:
        key, error = result
        self._pending.pop(key, None)
        if error:
            self._errors[key] = error
            while len(self._errors) > 32:
                self._errors.popitem(last=False)
        self.ready.emit()
